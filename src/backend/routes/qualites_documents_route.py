"""
qualites_documents_route.py
───────────────────────────
Contient :
  • analyze_document(file_path)  → fonction pure appelée dans add_texte /
    upload_documents pour peupler la table `qualites_documents` juste après
    l'écriture physique du fichier.
  • Les routes FastAPI (GET) pour consulter les qualités enregistrées.

Les suppressions sont gérées dans document_route.py (cascade manuelle lors
du DELETE /documents/{id} et DELETE /documents).
"""

import asyncio
import base64
import io
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import List

import cv2
import httpx
import numpy as np
from fastapi import APIRouter, File as FastAPIFile, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pdf2image import convert_from_path
from pdf2image.pdf2image import pdfinfo_from_path
from pydantic import BaseModel

from src.backend.db.database import db_dependency
from src.backend.models.qualites_documents import QualiteDocument
from src.backend.schemas.qualites_documents_schemas import (
    QualiteDocumentListResponse,
    QualiteDocumentResponse,
)
# Réutilisation telle quelle de la logique VLM + config OpenRouter de
# rag_route.py (même modèle d'extraction, même client HTTP, même clé) —
# on ne veut pas de deuxième implémentation de l'appel OpenRouter à
# maintenir en parallèle.
from src.backend.routes.rag_route import (
    OPENROUTER_API_KEY,
    OPENROUTER_CHAT_URL,
    _HEADERS,
    _call_with_retry,
    _extract_page_text,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Qualités documents"])

# ─── Résolution cible pour le resize (repris de stats.py) ────────────────────
MAX_RESOLUTION = 9_497_600
DPI_VALUE = 300

EXTENSIONS_IMAGES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}


# =============================================================================
# Fonctions de métriques (extraites de stats.py, sans dépendances BD)
# =============================================================================

def _blur_score(gray: np.ndarray) -> float:
    """Variance du Laplacien — plus c'est élevé, plus l'image est nette."""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _skew(image_bgr: np.ndarray) -> float:
    """Inclinaison estimée du document (en degrés)."""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 5))
    dilated = cv2.dilate(thresh, kernel, iterations=1)
    contours, _ = cv2.findContours(dilated, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    angles = []
    for contour in contours:
        if cv2.contourArea(contour) < 500:
            continue
        angle = cv2.minAreaRect(contour)[-1]
        if angle < -45:
            angle = 90 + angle
        if -45 <= angle <= 45:
            angles.append(angle)

    return round(float(np.median(angles)), 2) if angles else 0.0


def _noise_score(gray: np.ndarray) -> float:
    """Écart-type du résidu après flou gaussien."""
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    return round(float(np.std(gray.astype(np.float32) - blurred.astype(np.float32))), 3)


def _black_pixel_ratio(gray: np.ndarray) -> float:
    """Ratio de pixels sombres (< 50) sur le total."""
    return round(float(np.sum(gray < 50) / gray.size), 3)


def _entropy(gray: np.ndarray) -> float:
    """Entropie de Shannon sur l'histogramme d'intensité."""
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256])
    hist = hist / hist.sum()
    hist = hist[hist > 0]
    return round(float(-np.sum(hist * np.log2(hist))), 3)


def _brightness(gray: np.ndarray) -> float:
    """Luminosité moyenne ramenée à 0-100."""
    return round(float(np.mean(gray) / 255 * 100), 1)


def _global_score(
    blur: float,
    skew: float,
    noise_score: float,
    black_pixel_ratio: float,
    entropy: float,
    brightness: float,
) -> float:
    """
    Score global de qualité sur 100 (heuristique simple, ajustable).

    Logique :
    - blur          : variance Laplacien — on cible ≥ 100 (document net)
    - skew          : idéalement 0° — on pénalise au-delà de 5°
    - noise_score   : idéalement bas (< 5) — on pénalise au-delà
    - black_pixel_ratio : idéalement < 0.05 (pas trop sombre)
    - entropy       : idéalement entre 4 et 7
    - brightness    : idéalement entre 50 et 90
    """
    score = 100.0

    # Netteté (40 pts)
    blur_score = min(blur / 100.0, 1.0) * 40
    score = blur_score

    # Inclinaison (20 pts) — pénalité linéaire jusqu'à 10°
    skew_penalty = min(abs(skew) / 10.0, 1.0) * 20
    score += (20 - skew_penalty)

    # Bruit (15 pts) — pénalité si noise_score > 5
    noise_penalty = min(max(noise_score - 5, 0) / 15.0, 1.0) * 15
    score += (15 - noise_penalty)

    # Ratio pixels noirs (10 pts) — idéalement < 0.05
    black_penalty = min(black_pixel_ratio / 0.20, 1.0) * 10
    score += (10 - black_penalty)

    # Entropie (10 pts) — idéalement 4 ≤ e ≤ 7
    entropy_score = 10 if 4 <= entropy <= 7 else max(0.0, 10 - abs(entropy - 5.5) * 2)
    score += entropy_score

    # Luminosité (5 pts) — idéalement 50-90
    brightness_score = 5 if 50 <= brightness <= 90 else max(0.0, 5 - abs(brightness - 70) / 20)
    score += brightness_score

    return round(min(max(score, 0.0), 100.0), 2)


# =============================================================================
# Fonction principale : analyze_document
# =============================================================================

def _metrics_from_gray(gray: np.ndarray, image_bgr: np.ndarray) -> dict:
    """Calcule toutes les métriques pour une image en niveaux de gris."""
    b = _blur_score(gray)
    s = _skew(image_bgr)
    n = _noise_score(gray)
    bp = _black_pixel_ratio(gray)
    e = _entropy(gray)
    br = _brightness(gray)
    sc = _global_score(b, s, n, bp, e, br)
    return {
        "blur": round(b, 3),
        "skew": s,
        "noise_score": n,
        "black_pixel_ratio": bp,
        "entropy": e,
        "brightness": br,
        "score": sc,
    }


def analyze_document(file_path: Path) -> List[dict]:
    """
    Analyse un fichier PDF ou image et retourne une liste de dicts,
    un par page, prêts à être insérés dans `qualites_documents`.

    Chaque dict contient :
        page, blur, skew, noise_score, black_pixel_ratio, entropy,
        brightness, score

    Appelée SANS session BD — c'est l'appelant (route) qui gère
    la transaction (flush/commit).

    Retourne [] si le fichier ne peut pas être lu.
    """
    suffix = file_path.suffix.lower()
    results: List[dict] = []

    try:
        if suffix == ".pdf":
            info = pdfinfo_from_path(file_path)
            total_pages = info["Pages"]
            pages = convert_from_path(file_path, dpi=DPI_VALUE)

            for index, pil_page in enumerate(pages):
                image_bgr = cv2.cvtColor(np.array(pil_page), cv2.COLOR_RGB2BGR)
                gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
                metrics = _metrics_from_gray(gray, image_bgr)
                metrics["page"] = index + 1
                results.append(metrics)

        elif suffix in EXTENSIONS_IMAGES:
            image_bgr = cv2.imdecode(
                np.fromfile(file_path, dtype=np.uint8), cv2.IMREAD_COLOR
            )
            if image_bgr is None:
                logger.warning("analyze_document : impossible de lire %s", file_path.name)
                return []
            gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
            metrics = _metrics_from_gray(gray, image_bgr)
            metrics["page"] = 1
            results.append(metrics)

        else:
            logger.info(
                "analyze_document : extension non prise en charge (%s), ignoré.", suffix
            )

    except Exception:
        logger.exception("analyze_document : erreur lors de l'analyse de %s", file_path.name)

    return results


# =============================================================================
# Analyse en direct (SSE) — utilisée AVANT l'enregistrement du texte, pendant
# l'import des fichiers dans l'éditeur. Ne touche pas la base de données :
# c'est uniquement pour donner un retour visuel immédiat à l'utilisateur
# (mini console de progression). L'écriture réelle dans `qualites_documents`
# continue de se faire via `analyze_document`, appelée au moment de la
# publication du texte (add_texte / upload_documents).
#
# Les appels cv2 / pdf2image sont bloquants : on les passe systématiquement
# par `asyncio.to_thread` pour ne pas geler la boucle asyncio le temps du
# calcul — sinon aucun event SSE ne partirait avant la fin totale du
# traitement, ce qui viderait le flux de son intérêt (progression page par
# page en direct côté front).
# =============================================================================

# Limite le nombre de pages traitées par le VLM en parallèle (un seul
# fichier à la fois passe par ce flux côté front — voir le singleton
# AnalysisStore côté useDocumentAnalysis.ts — mais plusieurs pages d'un
# même PDF peuvent être envoyées en même temps si on ne les sérialise pas).
_VLM_STREAM_MAX_CONCURRENT = 3
_vlm_stream_semaphore = asyncio.Semaphore(_VLM_STREAM_MAX_CONCURRENT)


def _pil_to_base64_png(pil_page) -> str:
    buf = io.BytesIO()
    pil_page.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


async def _extract_text_safe(pil_page, filename: str, page_num: int) -> str | None:
    """
    Extraction VLM d'une page, best-effort : une erreur ici ne doit pas
    faire échouer tout le flux SSE (la page garde ses métriques de qualité,
    juste sans texte extrait — l'utilisateur peut toujours choisir de ne
    pas insérer cette page dans l'éditeur).
    """
    image_b64 = await asyncio.to_thread(_pil_to_base64_png, pil_page)
    async with _vlm_stream_semaphore:
        try:
            return await _call_with_retry(_extract_page_text, image_b64, "image/png")
        except Exception:
            logger.exception(
                "analyze_stream : extraction VLM échouée pour %s page %s",
                filename, page_num,
            )
            return None


def _sse(event: dict) -> str:
    """Formate un dict en event SSE (`data: ...\n\n`)."""
    return f"data: {json.dumps(event)}\n\n"


# ─── Résumé + mots-clés (déclenchés une fois tout le texte extrait) ──────
# Même modèle que le chunking RAG par défaut, dédié séparément ici pour
# pouvoir l'ajuster indépendamment (prompts différents, pas de contrainte
# de fidélité au texte source comme pour le chunking).
SUMMARY_KEYWORDS_MODEL = os.getenv(
    "OPENROUTER_SUMMARY_MODEL", "google/gemini-3.1-flash-lite"
)

SUMMARY_PROMPT = """Tu vas résumer un texte juridique.

Règles :
- Le résumé doit faire environ 1/5 de la longueur du texte d'origine (ni plus, ni beaucoup moins)
- Reste factuel et neutre, ne reformule pas le sens des dispositions
- Conserve les numéros d'articles, dates et montants s'ils sont essentiels au sens
- Rédige en phrases complètes, sans puces ni titres

Réponds UNIQUEMENT avec le résumé, sans préambule ni commentaire.

Texte :
{texte}
"""

KEYWORDS_PROMPT = """Tu vas extraire des mots-clés d'un texte juridique, pour améliorer sa \
recherche dans un registre documentaire.

Règles :
- Extrait EXACTEMENT {nombre} mots-clés ou courtes expressions (2-3 mots maximum chacun)
- Privilégie les termes qui identifient le sujet, le type d'acte, le domaine concerné
- Pas de doublons ni de synonymes proches

Réponds UNIQUEMENT avec un JSON strict, sans texte avant/après, sans balises markdown ```:
{{"mots_cles": ["...", "..."]}}

Texte :
{texte}
"""


async def _call_llm_text(prompt: str) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY manquant.")

    payload = {
        "model": SUMMARY_KEYWORDS_MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(OPENROUTER_CHAT_URL, json=payload, headers=_HEADERS)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Erreur LLM ({SUMMARY_KEYWORDS_MODEL}) {resp.status_code}: {resp.text}"
        )

    data = resp.json()
    content = (data["choices"][0].get("message") or {}).get("content")
    if content is None:
        raise RuntimeError("Réponse LLM vide (content=None).")
    return content.strip()


async def _summarize_text(texte: str) -> str | None:
    """Best-effort : ne doit jamais faire échouer le flux SSE en cas d'erreur."""
    try:
        return await _call_with_retry(_call_llm_text, SUMMARY_PROMPT.format(texte=texte))
    except Exception:
        logger.exception("analyze_stream : échec du résumé")
        return None


async def _extract_keywords(texte: str, nombre: int = 5) -> list[str] | None:
    """Best-effort : ne doit jamais faire échouer le flux SSE en cas d'erreur."""
    try:
        raw = await _call_with_retry(
            _call_llm_text, KEYWORDS_PROMPT.format(texte=texte, nombre=nombre)
        )
    except Exception:
        logger.exception("analyze_stream : échec de l'extraction des mots-clés")
        return None

    cleaned = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(cleaned)
        mots = parsed.get("mots_cles", [])
        result = [m.strip() for m in mots if isinstance(m, str) and m.strip()]
        return result[:nombre] if result else None
    except (json.JSONDecodeError, AttributeError):
        # Le modèle n'a pas respecté le JSON : fallback sur un découpage
        # naïf par virgules plutôt que de perdre le résultat.
        fallback = [m.strip() for m in cleaned.split(",") if m.strip()]
        return fallback[:nombre] if fallback else None


async def _analyze_stream(file_path: Path, filename: str):
    """
    Générateur asynchrone : analyse `file_path` page par page et yield un
    event SSE à chaque étape (page_start / page_done / file_done / error).
    """
    suffix = file_path.suffix.lower()
    # Texte de chaque page, dans l'ordre — sert à construire le texte complet
    # une fois l'extraction terminée, pour en tirer un résumé et des
    # mots-clés (voir plus bas, après le if/elif/else).
    extracted_texts: list[str] = []

    try:
        if suffix == ".pdf":
            info = await asyncio.to_thread(pdfinfo_from_path, file_path)
            total_pages = info["Pages"]

            for page_num in range(1, total_pages + 1):
                yield _sse({
                    "type": "page_start",
                    "filename": filename,
                    "page": page_num,
                    "total_pages": total_pages,
                })

                pages = await asyncio.to_thread(
                    convert_from_path,
                    file_path,
                    dpi=DPI_VALUE,
                    first_page=page_num,
                    last_page=page_num,
                )
                pil_page = pages[0]
                # Force le chargement complet des pixels AVANT de partager
                # cet objet entre deux threads concurrents (_compute et
                # _extract_text_safe ci-dessous) : un Image PIL peut être
                # chargé paresseusement, et deux threads accédant en même
                # temps à un chargement encore en cours provoquent une
                # erreur "image file is truncated" (lecture concurrente non
                # thread-safe du buffer sous-jacent).
                await asyncio.to_thread(pil_page.load)

                def _compute(pil_page=pil_page) -> dict:
                    image_bgr = cv2.cvtColor(np.array(pil_page), cv2.COLOR_RGB2BGR)
                    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
                    return _metrics_from_gray(gray, image_bgr)

                # Métriques (cv2, local) et extraction VLM (réseau) lancées
                # de front : la seconde est nettement plus lente, inutile
                # de les sérialiser.
                metrics, text = await asyncio.gather(
                    asyncio.to_thread(_compute),
                    _extract_text_safe(pil_page, filename, page_num),
                )
                if text:
                    extracted_texts.append(text)
                metrics["page"] = page_num
                yield _sse({
                    "type": "page_done",
                    "filename": filename,
                    "page": page_num,
                    "total_pages": total_pages,
                    "text": text,
                    **metrics,
                })

        elif suffix in EXTENSIONS_IMAGES:
            yield _sse({
                "type": "page_start",
                "filename": filename,
                "page": 1,
                "total_pages": 1,
            })

            def _compute_image():
                image_bgr = cv2.imdecode(
                    np.fromfile(file_path, dtype=np.uint8), cv2.IMREAD_COLOR
                )
                if image_bgr is None:
                    return None
                gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
                return _metrics_from_gray(gray, image_bgr)

            def _load_pil():
                from PIL import Image
                return Image.open(file_path).convert("RGB")

            metrics, pil_page = await asyncio.gather(
                asyncio.to_thread(_compute_image),
                asyncio.to_thread(_load_pil),
            )
            if metrics is None:
                yield _sse({
                    "type": "error",
                    "filename": filename,
                    "message": "Image illisible.",
                })
                return

            text = await _extract_text_safe(pil_page, filename, 1)
            if text:
                extracted_texts.append(text)

            metrics["page"] = 1
            yield _sse({
                "type": "page_done",
                "filename": filename,
                "page": 1,
                "total_pages": 1,
                "text": text,
                **metrics,
            })

        else:
            yield _sse({
                "type": "error",
                "filename": filename,
                "message": f"Format non pris en charge ({suffix}).",
            })
            return

        # Le résumé et les mots-clés ne sont plus générés automatiquement ici
        # : un même contenu peut être scindé en plusieurs fichiers (ex: PDF
        # coupé en deux), donc un résumé par fichier n'aurait pas de sens.
        # Voir POST /qualites-documents/generate-summary-keywords, déclenchée
        # manuellement une fois tous les fichiers importés (bouton côté
        # front), sur le texte combiné de tous les fichiers.
        yield _sse({"type": "file_done", "filename": filename})

    except Exception as exc:
        logger.exception("analyze_stream : erreur lors de l'analyse de %s", filename)
        yield _sse({
            "type": "error",
            "filename": filename,
            "message": str(exc),
        })


@router.post(
    "/qualites-documents/analyze-stream",
    summary="Analyse en direct (SSE) d'un fichier avant enregistrement du texte",
)
async def analyze_stream(file: UploadFile = FastAPIFile(...)):
    """
    Reçoit un fichier (image ou PDF), l'écrit temporairement sur disque, et
    streame sa progression d'analyse page par page via Server-Sent Events.
    Le fichier temporaire est supprimé une fois le flux terminé, quel que
    soit le résultat (succès ou erreur).
    """
    suffix = Path(file.filename).suffix
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    content = await file.read()
    tmp.write(content)
    tmp.close()
    tmp_path = Path(tmp.name)

    async def event_generator():
        try:
            async for event in _analyze_stream(tmp_path, file.filename):
                yield event
        finally:
            tmp_path.unlink(missing_ok=True)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            # Évite que nginx (ou un autre reverse-proxy) ne bufferise le
            # flux et ne le livre d'un coup à la fin du traitement complet.
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )


# ─── POST /qualites-documents/generate-summary-keywords ─────────────────────
# Déclenchée manuellement côté front (bouton "Générer mots-clés et résumé"),
# une fois que l'utilisateur a fini d'importer tous ses fichiers — plutôt
# qu'automatiquement à la fin de chaque fichier individuel : un même contenu
# peut être scindé en plusieurs PDF (ex: un texte coupé en deux), et on veut
# un résumé/mots-clés sur l'ensemble, pas un par fichier.
class GenerateSummaryKeywordsPayload(BaseModel):
    # Texte déjà extrait (VLM) de chaque fichier terminé, côté front —
    # un élément par fichier, dans l'ordre d'import. On les rejoint ici en
    # un seul texte avant résumé/mots-clés.
    texts: List[str]
    # Nombre de mots-clés souhaité, calculé côté front (≈ total_mots / 20).
    # Optionnel pour rester compatible avec d'anciens appels : défaut à 5.
    keywords_count: int | None = None


class GenerateSummaryKeywordsResponse(BaseModel):
    summary: str | None = None
    keywords: List[str] | None = None


@router.post(
    "/qualites-documents/generate-summary-keywords",
    response_model=GenerateSummaryKeywordsResponse,
    summary="Génère un résumé et des mots-clés combinés à partir des textes déjà extraits de plusieurs fichiers",
)
async def generate_summary_keywords(payload: GenerateSummaryKeywordsPayload):
    full_text = "\n\n".join(t.strip() for t in payload.texts if t and t.strip())
    if not full_text:
        raise HTTPException(status_code=400, detail="Aucun texte exploitable fourni.")

    nombre = payload.keywords_count if payload.keywords_count and payload.keywords_count > 0 else 5

    summary, keywords = await asyncio.gather(
        _summarize_text(full_text),
        _extract_keywords(full_text, nombre=nombre),
    )
    return GenerateSummaryKeywordsResponse(summary=summary, keywords=keywords)


# ─── POST /qualites-documents/generate-metadata-suggestions ─────────────────
# Déclenchée par le même bouton "Générer suggestions" côté front
# (mots-cles-resume-section.tsx), en parallèle de generate-summary-keywords.
# Sert à préremplir informations-complementaires-section.tsx et
# titre-classification-section.tsx à partir du texte déjà extrait.
#
# IMPORTANT : la forme de la réponse doit correspondre EXACTEMENT à
# l'interface MetadataSuggestion de useDocumentAnalysis.ts côté front — un
# JSON PLAT (pas enveloppé dans "suggestions"), avec ces noms de champs
# précis (categorie_nom/statut_nom/theme_noms, pas categorie/statut/theme).
class GenerateMetadataSuggestionsPayload(BaseModel):
    texts: List[str]


class MetadataSuggestionResponse(BaseModel):
    titre: str | None = None
    numero: str | None = None
    date_mise_en_vigueur: str | None = None
    nom_signataire: str | None = None
    titre_signataire: str | None = None
    categorie_nom: str | None = None
    statut_nom: str | None = None
    theme_noms: List[str] | None = None


METADATA_PROMPT = """Tu vas analyser un texte juridique pour préremplir la fiche \
descriptive d'un document dans un registre documentaire.

Règles générales :
- Réponds UNIQUEMENT avec un JSON strict, sans texte avant/après, sans balises markdown ```
- Ne mets une valeur à null QUE si l'information est vraiment absente du texte. Pour "titre", \
"categorie_nom", "statut_nom" et "theme_noms", fournis TOUJOURS ta meilleure estimation à \
partir du contenu et de la structure du texte (type d'acte, vocabulaire employé, objet des \
dispositions) plutôt que de répondre null par excès de prudence — une estimation raisonnable \
est plus utile qu'un champ vide, l'utilisateur peut la corriger ou l'ignorer.

Champs à extraire :
- "titre" : titre court et factuel du document (ex : type d'acte + objet), pas de ponctuation \
finale. Se déduit quasiment toujours du texte (ex : "Arrêté portant nomination de...", \
"Contrat de bail commercial entre..."). Ne le laisse à null que si le texte est trop \
fragmentaire pour même deviner le type de document.
- "numero" : numéro/référence officielle de l'acte si présent (ex : "2024-DC-042")
- "date_mise_en_vigueur" : date de signature ou d'entrée en vigueur, au format AAAA-MM-JJ si déductible
- "nom_signataire" : nom de la personne qui signe l'acte
- "titre_signataire" : fonction/titre officiel du signataire (ex : "Directeur Général des Douanes")
- "categorie_nom" : type de document en un ou deux mots courants (ex : "Loi", "Décret", "Arrêté", \
"Contrat", "Correspondance", "Circulaire", "Note de service") — déduis-le du format et du \
vocabulaire du texte même si aucune liste de catégories ne t'est fournie
- "statut_nom" : statut apparent du document si déductible (ex : "Projet", "En vigueur", \
"Abrogé", "Signé")
- "theme_noms" : liste de 1 à 3 thèmes/domaines principaux en un ou deux mots chacun (ex : \
["Fiscalité", "Foncier"], ["Douanes", "Import-export"]) — déduis-les du sujet traité par le \
texte ; tableau vide uniquement si le texte est vraiment trop générique pour identifier un domaine

Format de réponse (JSON strict) :
{{"titre": "...", "numero": "...", "date_mise_en_vigueur": "...", "nom_signataire": "...", \
"titre_signataire": "...", "categorie_nom": "...", "statut_nom": "...", "theme_noms": ["..."]}}

Texte :
{texte}
"""


async def _suggest_metadata(texte: str) -> MetadataSuggestionResponse:
    """Best-effort : en cas d'échec, retourne des champs vides plutôt que de
    faire échouer toute la requête (le front n'affiche alors simplement pas
    les bannières de suggestion)."""
    try:
        raw = await _call_with_retry(_call_llm_text, METADATA_PROMPT.format(texte=texte))
    except Exception:
        logger.exception("generate-metadata-suggestions : échec de l'appel LLM")
        return MetadataSuggestionResponse()

    cleaned = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("generate-metadata-suggestions : réponse LLM non-JSON, ignorée")
        return MetadataSuggestionResponse()

    def _clean_str(value) -> str | None:
        if isinstance(value, str) and value.strip() and value.strip().lower() != "null":
            return value.strip()
        return None

    theme_noms_raw = parsed.get("theme_noms")
    theme_noms = None
    if isinstance(theme_noms_raw, list):
        cleaned_themes = [t.strip() for t in theme_noms_raw if isinstance(t, str) and t.strip()]
        theme_noms = cleaned_themes or None

    return MetadataSuggestionResponse(
        titre=_clean_str(parsed.get("titre")),
        numero=_clean_str(parsed.get("numero")),
        date_mise_en_vigueur=_clean_str(parsed.get("date_mise_en_vigueur")),
        nom_signataire=_clean_str(parsed.get("nom_signataire")),
        titre_signataire=_clean_str(parsed.get("titre_signataire")),
        categorie_nom=_clean_str(parsed.get("categorie_nom")),
        statut_nom=_clean_str(parsed.get("statut_nom")),
        theme_noms=theme_noms,
    )


@router.post(
    "/qualites-documents/generate-metadata-suggestions",
    response_model=MetadataSuggestionResponse,
    summary="Génère des suggestions de préremplissage (titre, classification, informations complémentaires)",
)
async def generate_metadata_suggestions(payload: GenerateMetadataSuggestionsPayload):
    full_text = "\n\n".join(t.strip() for t in payload.texts if t and t.strip())
    if not full_text:
        raise HTTPException(status_code=400, detail="Aucun texte exploitable fourni.")

    # Réponse renvoyée telle quelle (JSON plat) — le front fait
    # `await res.json() as MetadataSuggestion` directement, sans déballer
    # de clé englobante.
    return await _suggest_metadata(full_text)


# =============================================================================
# Routes FastAPI (lecture seule — les écritures sont faites dans document_route)
# =============================================================================

@router.get(
    "/qualites-documents/document/{document_id}",
    response_model=QualiteDocumentListResponse,
    summary="Qualités par document",
)
def get_qualites_by_document(document_id: int, db: db_dependency):
    """Retourne les métriques de qualité de toutes les pages d'un document."""
    rows = (
        db.query(QualiteDocument)
        .filter(QualiteDocument.document_id == document_id)
        .order_by(QualiteDocument.page.asc())
        .all()
    )
    if not rows:
        raise HTTPException(
            status_code=404,
            detail="Aucune qualité enregistrée pour ce document.",
        )
    return QualiteDocumentListResponse(document_id=document_id, pages=rows)


@router.get(
    "/qualites-documents/{id}",
    response_model=QualiteDocumentResponse,
    summary="Qualité d'une ligne",
)
def get_qualite_by_id(id: int, db: db_dependency):
    """Retourne une ligne de qualité par son identifiant."""
    row = db.query(QualiteDocument).filter(QualiteDocument.id == id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Entrée introuvable.")
    return row


@router.get(
    "/qualites-documents",
    response_model=List[QualiteDocumentResponse],
    summary="Toutes les qualités",
)
def get_all_qualites(db: db_dependency):
    """Liste complète des métriques de qualité (toutes pages, tous documents)."""
    return (
        db.query(QualiteDocument)
        .order_by(QualiteDocument.document_id.asc(), QualiteDocument.page.asc())
        .all()
    )