"""
rag_route.py
────────────
Route FastAPI pour uploader un ou plusieurs PDF/images, extraire leur texte
(en Markdown, avec préservation des tableaux) via un modèle vision OpenRouter,
découper ce texte en chunks sémantiques via un LLM, embedder ces chunks,
et les stocker dans ChromaDB (une seule collection, un id par chunk).

Deux pipelines selon la taille du document :

  - Document <= LARGE_DOC_PAGE_THRESHOLD pages :
        pipeline "classique" (tout le document en un seul passage).

  - Document > LARGE_DOC_PAGE_THRESHOLD pages :
        pipeline "par lots" de BATCH_PAGE_SIZE pages (1 par défaut, donc
        page par page) :
            1. VLM sur les pages du lot (en parallèle, limité par sémaphore)
               -> sauvegarde un markdown distinct par page :
               "nom-fichier-page0001.md", "-page0002.md", ... (ou
               "-page0001-0005.md" si BATCH_PAGE_SIZE > 1)
            2. Chunking sémantique du markdown de CE lot uniquement
            3. Embedding des chunks du lot (par sous-lots)
            4. Upsert Chroma du lot (par sous-lots)
            5. Passe au lot suivant

Chaque étape utilise un retry avec backoff exponentiel pour absorber les
erreurs transitoires (429, 5xx, timeout) d'OpenRouter, afin d'éviter qu'un
échec ponctuel sur un lot ne fasse échouer tout le document.
"""

import asyncio
import base64
import difflib
import html as html_lib
import json
import os
import random
import re
from pathlib import Path

import chromadb
import fitz  # PyMuPDF
import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.backend.routes.user_route import get_current_user
from src.backend.models.user import User

load_dotenv()

router = APIRouter(prefix="/rag", tags=["rag"])

# ─── Streaming de progression de l'indexation RAG (chunking/embedding) ────
# Le chunking sémantique + l'embedding + l'upsert Chroma d'un texte
# s'exécutent en tâche de fond (BackgroundTasks, voir texte_route.py) après
# que la réponse HTTP de création/mise à jour du texte est déjà repartie
# côté client. Pour que le terminal de publication du frontend puisse
# afficher la PROGRESSION RÉELLE de cette tâche (et pas une simulation), on
# publie chaque étape dans une file asyncio dédiée au texte_id, qu'un
# endpoint SSE (GET /rag/ingest-progress/{texte_id}) se contente de
# retransmettre au fur et à mesure.
_ingest_progress_queues: dict[int, "asyncio.Queue[dict]"] = {}


def _emit_ingest_progress(texte_id: int, event: dict) -> None:
    """Publie un event de progression pour ce texte. Crée la file si elle
    n'existe pas encore — la tâche de fond (BackgroundTasks) peut démarrer
    avant que le frontend n'ait eu le temps d'ouvrir le flux SSE ; dans ce
    cas, la file bufferise les premiers events plutôt que de les perdre,
    et le endpoint SSE s'y abonne dès sa connexion."""
    queue = _ingest_progress_queues.get(texte_id)
    if queue is None:
        queue = asyncio.Queue()
        _ingest_progress_queues[texte_id] = queue
    queue.put_nowait(event)


def _unique_path_in(directory: Path, filename: str) -> Path:
    """Équivalent local de get_unique_path (document_route.py) : renvoie un
    chemin qui n'existe pas encore dans `directory`, en suffixant
    "(1)", "(2)", ... au nom si besoin.

    Dupliqué ICI plutôt qu'importé de document_route.py pour éviter un
    import circulaire : document_route.py importe déjà `cleanup_rag_data`
    depuis ce module (rag_route.py), donc rag_route.py ne peut pas importer
    en retour depuis document_route.py sans créer une boucle
    (ImportError: "partially initialized module").

    Si la logique de dédoublonnage de document_route.get_unique_path change
    un jour, penser à répercuter le changement ici aussi.
    """
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem, suffix = Path(filename).stem, Path(filename).suffix
    i = 1
    while True:
        candidate = directory / f"{stem}({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1

# ─── Configuration ────────────────────────────────────────────────────────

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"

VISION_MODEL = os.getenv("OPENROUTER_VISION_MODEL", "google/gemini-3.1-flash-lite")
# Modèle dédié au chunking, indépendant du VLM. Qwen est un modèle
# "thinking" : il peut consommer tout son budget de tokens en raisonnement
# interne avant même de commencer à écrire le JSON de sortie, ce qui produit
# des réponses content=None (finish_reason="length") ou du JSON tronqué.
# Les protections existantes (_try_repair_truncated_json, fallback
# _fallback_chunk_by_paragraphs sur content=None) couvrent déjà ce cas.
CHUNKING_MODEL = os.getenv("OPENROUTER_CHUNKING_MODEL", "google/gemini-3.1-flash-lite")
EMBEDDING_MODEL = os.getenv("OPENROUTER_EMBEDDING_MODEL", "perplexity/pplx-embed-v1-4b")

# On sort du dossier actuel (src/backend/routes/ par ex.) pour aller dans
# {racine du projet}/rag/, avec ses trois sous-dossiers dédiés.
RAG_DIR = Path(os.getenv("RAG_DIR", Path(__file__).resolve().parent.parent / "rag"))
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", RAG_DIR / "uploads"))
MARKDOWN_CACHE_DIR = Path(os.getenv("MARKDOWN_CACHE_DIR", RAG_DIR / "markdowns"))
CHUNKS_DIR = Path(os.getenv("CHUNKS_DIR", RAG_DIR / "chunks"))
CHROMA_DIR = Path(os.getenv("CHROMA_DIR", RAG_DIR / "chromadb"))

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
MARKDOWN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
CHROMA_DIR.mkdir(parents=True, exist_ok=True)

_HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
}

_chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
_collection = _chroma_client.get_or_create_collection(name="documents")

ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}

# Balises générées par l'éditeur Lexical qui doivent produire un saut de
# ligne / paragraphe dans le texte brut, avant de retirer le reste du HTML.
_BLOCK_TAGS_RE = re.compile(
    r"</(p|div|h[1-6]|li|tr|blockquote|br)\s*>|<br\s*/?>", re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_plain_text(html: str) -> str:
    """
    Conversion HTML -> texte brut pour le chunking du contenu d'un texte
    juridique saisi dans l'éditeur Lexical (document-section.tsx).

    Contrairement au pipeline RAG classique (PDF/image), il n'y a pas
    besoin de VLM ici : le contenu est déjà du texte structuré, on le
    "désérialise" juste en texte simple avant de le passer à
    _chunk_with_llm, qui attend du Markdown/texte brut.
    """
    if not html:
        return ""
    with_breaks = _BLOCK_TAGS_RE.sub("\n", html)
    text = _TAG_RE.sub("", with_breaks)
    text = html_lib.unescape(text)
    # Compacte les lignes vides multiples laissées par les balises de bloc
    lines = [line.strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned


def _backfill_missing_inclus_field() -> None:
    """
    Migration one-shot au démarrage : les chunks indexés AVANT l'ajout du
    champ "inclus" n'ont pas cette clé en metadata. rag_retriever.py filtre
    désormais avec where={"inclus": 1}, ce qui les exclurait silencieusement
    de toute recherche RAG. On les marque donc inclus=1 (comportement
    inchangé pour l'utilisateur) au premier chargement du module.
    """
    try:
        existing = _collection.get(include=["metadatas"])
    except Exception as e:
        print(f"[rag] backfill 'inclus' impossible (collection illisible) : {e}")
        return

    ids = existing.get("ids", [])
    metadatas = existing.get("metadatas") or []
    to_update_ids, to_update_metas = [], []
    for chunk_id, meta in zip(ids, metadatas):
        meta = meta or {}
        if "inclus" not in meta:
            new_meta = dict(meta)
            new_meta["inclus"] = 1
            to_update_ids.append(chunk_id)
            to_update_metas.append(new_meta)

    if to_update_ids:
        _collection.update(ids=to_update_ids, metadatas=to_update_metas)
        print(f"[rag] backfill 'inclus' : {len(to_update_ids)} chunk(s) mis à inclus=1")


_backfill_missing_inclus_field()

# ─── Config pipeline "gros document" / retries / concurrence ─────────────

LARGE_DOC_PAGE_THRESHOLD = int(os.getenv("RAG_LARGE_DOC_THRESHOLD", "20"))
BATCH_PAGE_SIZE = int(os.getenv("RAG_BATCH_PAGE_SIZE", "1"))
MAX_CONCURRENT_VLM = int(os.getenv("RAG_MAX_CONCURRENT_VLM", "4"))
MAX_RETRIES = int(os.getenv("RAG_MAX_RETRIES", "4"))
EMBED_BATCH_SIZE = int(os.getenv("RAG_EMBED_BATCH_SIZE", "50"))
CHROMA_UPSERT_BATCH_SIZE = int(os.getenv("RAG_CHROMA_BATCH_SIZE", "300"))
CHUNKING_MAX_TOKENS = int(os.getenv("RAG_CHUNKING_MAX_TOKENS", "8000"))

_vlm_semaphore = asyncio.Semaphore(MAX_CONCURRENT_VLM)


# ─── Prompts ──────────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """Extrait tout le texte visible dans cette page/image, sans reformulation ni résumé.

Règles importantes :
- Si tu rencontres un tableau, retranscris-le en syntaxe Markdown (| colonne1 | colonne2 |...)
  en conservant l'alignement lignes/colonnes exact, sans mélanger les données de colonnes différentes
- Conserve les titres, sous-titres, numéros de section tels quels (utilise la syntaxe Markdown # ## ###)
- Ne saute aucune donnée chiffrée, même si elle semble redondante
- Ne commente pas, ne résume pas, ne reformule pas

Retourne uniquement le texte extrait en Markdown, sans préambule ni commentaire."""

CHUNKING_PROMPT = """Tu vas découper un texte Markdown en chunks pour un système de recherche sémantique (RAG).

Règles strictes :
- Ne reformule JAMAIS le texte, retourne-le EXACTEMENT tel quel, juste segmenté
- Chaque chunk doit être une unité de sens autonome (idée complète, section, paragraphe cohérent)
- RÈGLE ABSOLUE SUR LES TABLEAUX : un tableau Markdown (lignes avec |) doit rester ENTIER dans un
  seul chunk, jamais réparti sur plusieurs chunks, même si ça dépasse la taille cible. Le titre ou
  la légende juste avant le tableau doit être inclus dans le même chunk.
- EXCEPTION 1 : si un tableau dépasse 30 lignes, découpe-le par groupes de 10-15 lignes en répétant
  l'en-tête des colonnes ET le titre du tableau dans chaque chunk
- EXCEPTION 2 : si le texte fourni commence par la suite d'un tableau entamé avant (reconnaissable à
  des lignes "| ... | ... |" dès le début du texte, sans ligne d'en-tête ni titre avant elles), ce
  tableau continue probablement depuis une page précédente. Dans ce cas, répète quand même une ligne
  d'en-tête de colonnes plausible (déduite du contenu des lignes) en première ligne du chunk contenant
  cette suite de tableau, pour que le chunk reste compréhensible isolément
- Pour le texte hors tableau : taille cible 200 à 600 mots par chunk
- Ne coupe jamais en plein milieu d'une phrase
- IMPORTANT SUR LE FORMAT DE SORTIE : chaque chunk est une chaîne JSON valide. Échappe correctement
  tous les guillemets doubles (\\") et retours à la ligne (\\n) à l'intérieur des chunks. Ne renvoie
  jamais une chaîne JSON non terminée.

Réponds UNIQUEMENT avec un JSON strict, sans texte avant/après, sans balises markdown ```:
{{"chunks": ["...", "...", ...]}}

Texte à découper :
{texte}
"""


# ─── Retry générique avec backoff exponentiel ────────────────────────────

async def _call_with_retry(coro_fn, *args, **kwargs):
    """
    Exécute coro_fn(*args, **kwargs) avec retry/backoff exponentiel + jitter
    sur les erreurs transitoires (429, 5xx, timeout, erreurs réseau).
    """
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            return await coro_fn(*args, **kwargs)
        except RuntimeError as e:
            msg = str(e)
            transient = any(
                code in msg for code in ("429", "500", "502", "503", "504", "CONTENT_NULL")
            )
            if not transient or attempt == MAX_RETRIES - 1:
                raise
            last_exc = e
        except httpx.TransportError as e:
            # Couvre ConnectError, ReadError, WriteError, ReadTimeout, etc.
            # (coupures réseau/proxy/VPN pendant la connexion ou la lecture)
            last_exc = e
            if attempt == MAX_RETRIES - 1:
                raise RuntimeError(f"Échec après {MAX_RETRIES} tentatives : {e}") from e

        wait = (2 ** attempt) + random.uniform(0, 1)
        print(f"[retry] tentative {attempt + 1}/{MAX_RETRIES} échouée, nouvel essai dans {wait:.1f}s...")
        await asyncio.sleep(wait)

    raise RuntimeError(f"Échec après {MAX_RETRIES} tentatives : {last_exc}")


# ─── Appels OpenRouter ────────────────────────────────────────────────────

async def _extract_page_text(image_b64: str, mime: str) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY manquant.")

    payload = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": EXTRACTION_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                ],
            }
        ],
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(OPENROUTER_CHAT_URL, json=payload, headers=_HEADERS)
    if resp.status_code != 200:
        raise RuntimeError(f"Erreur extraction ({VISION_MODEL}) {resp.status_code}: {resp.text}")

    data = resp.json()
    message = data["choices"][0].get("message", {}) or {}
    content = message.get("content")
    if content is None:
        finish_reason = data["choices"][0].get("finish_reason")
        raise RuntimeError(
            f"[CONTENT_NULL] Réponse VLM vide (content=None, finish_reason={finish_reason!r}) : "
            f"{json.dumps(data)[:500]}"
        )
    return content


async def _chunk_with_llm(texte: str) -> list[str]:
    if not texte.strip():
        return []

    payload = {
        "model": CHUNKING_MODEL,
        "messages": [{"role": "user", "content": CHUNKING_PROMPT.format(texte=texte)}],
        # Le JSON de sortie peut être volumineux (tableaux répétés, en-têtes
        # dupliqués) ; un max_tokens trop bas tronque la réponse en plein
        # milieu d'une chaîne et invalide le JSON. On force une valeur large.
        "max_tokens": CHUNKING_MAX_TOKENS,
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(OPENROUTER_CHAT_URL, json=payload, headers=_HEADERS)
    if resp.status_code != 200:
        raise RuntimeError(f"Erreur chunking ({CHUNKING_MODEL}) {resp.status_code}: {resp.text}")

    data = resp.json()
    message = data["choices"][0].get("message", {}) or {}
    content = message.get("content")
    if content is None:
        finish_reason = data["choices"][0].get("finish_reason")
        print(
            f"[chunking] AVERTISSEMENT : content=None reçu du modèle "
            f"(finish_reason={finish_reason!r}), fallback en découpage par paragraphes"
        )
        _log_invalid_chunking_response(
            json.dumps(data)[:2000], finish_reason, ValueError("content=None")
        )
        return _fallback_chunk_by_paragraphs(texte)

    raw = content.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

    finish_reason = data["choices"][0].get("finish_reason")
    if finish_reason == "length":
        print(
            "[chunking] AVERTISSEMENT : réponse tronquée par max_tokens "
            f"(finish_reason='length', {len(raw)} caractères reçus) — tentative de réparation..."
        )

    chunks = _parse_chunking_response(raw, finish_reason)
    if chunks is None:
        # JSON définitivement irrécupérable : fallback sur découpage naïf,
        # sans faire planter le lot en cours.
        return _fallback_chunk_by_paragraphs(texte)

    if not _verify_chunks_fidelity(texte, chunks):
        # Fallback : découpage naïf par paragraphes si le LLM a trop dévié du texte source
        print("[chunking] fidélité insuffisante, fallback en découpage par paragraphes")
        chunks = _fallback_chunk_by_paragraphs(texte)

    return chunks


def _parse_chunking_response(raw: str, finish_reason: str | None) -> list[str] | None:
    """
    Parse la réponse JSON du chunker, avec tentative de réparation si le
    JSON est tronqué (ex: coupé en plein milieu d'une chaîne par max_tokens).
    Retourne None si totalement irrécupérable (l'appelant doit alors
    basculer sur un fallback de découpage naïf).
    """
    try:
        parsed = json.loads(raw)
        return parsed["chunks"]
    except (json.JSONDecodeError, KeyError) as e:
        repaired = _try_repair_truncated_json(raw)
        if repaired is not None:
            print(
                f"[chunking] JSON tronqué réparé avec succès ({len(repaired)} chunks récupérés "
                "sur la réponse partielle)"
            )
            return repaired

        _log_invalid_chunking_response(raw, finish_reason, e)
        return None


def _try_repair_truncated_json(raw: str) -> list[str] | None:
    """
    Tente de récupérer les chunks déjà complets d'un JSON tronqué en plein
    milieu, du type : {"chunks": ["chunk1 complet", "chunk2 complet", "chunk3 tro
    (coupé sans guillemet fermant ni accolade finale).

    Stratégie : extraire toutes les chaînes JSON complètes (entre guillemets,
    en respectant les échappements \\") qui apparaissent dans le tableau
    "chunks", en ignorant la dernière chaîne si elle est incomplète.
    Retourne None si aucune chaîne complète n'a pu être extraite.
    """
    marker = '"chunks"'
    idx = raw.find(marker)
    if idx == -1:
        return None

    # On se positionne juste après "chunks": [
    array_start = raw.find("[", idx)
    if array_start == -1:
        return None

    fragment = raw[array_start + 1:]

    # Regex qui capture des chaînes JSON complètes : "..." en gérant les
    # guillemets échappés (\") et les backslashes échappés (\\) à l'intérieur.
    string_pattern = re.compile(r'"((?:[^"\\]|\\.)*)"')
    matches = list(string_pattern.finditer(fragment))
    if not matches:
        return None

    chunks = []
    for m in matches:
        try:
            # json.loads sur la chaîne isolée pour dé-échapper proprement (\n, \", etc.)
            chunks.append(json.loads(f'"{m.group(1)}"'))
        except json.JSONDecodeError:
            # Chaîne malgré tout mal formée : on l'ignore plutôt que de planter
            continue

    return chunks if chunks else None


def _log_invalid_chunking_response(raw: str, finish_reason: str | None, error: Exception) -> None:
    """Écrit la réponse brute complète dans un fichier de debug (diagnostic)."""
    debug_path = CHUNKS_DIR / "_debug_invalid_json.log"
    try:
        with debug_path.open("a", encoding="utf-8") as f:
            f.write(f"--- finish_reason={finish_reason} len={len(raw)} error={error} ---\n{raw}\n\n")
    except OSError:
        pass
    print(
        f"[chunking] JSON invalide et irréparable ({error}), fallback en découpage par "
        f"paragraphes. Détail complet écrit dans {debug_path.name}"
    )


def _verify_chunks_fidelity(original: str, chunks: list[str], threshold: float = 0.92) -> bool:
    normalize = lambda t: " ".join(t.split())
    reconstructed = normalize(" ".join(chunks))
    ratio = difflib.SequenceMatcher(None, normalize(original), reconstructed).ratio()
    return ratio >= threshold


def _fallback_chunk_by_paragraphs(text: str, target_size: int = 600) -> list[str]:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, current = [], ""
    for p in paragraphs:
        if len(current) + len(p) > target_size and current:
            chunks.append(current.strip())
            current = p
        else:
            current += "\n\n" + p
    if current:
        chunks.append(current.strip())
    return chunks


async def _embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    payload = {"model": EMBEDDING_MODEL, "input": texts}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(OPENROUTER_EMBEDDINGS_URL, json=payload, headers=_HEADERS)
    if resp.status_code != 200:
        raise RuntimeError(f"Erreur embedding ({EMBEDDING_MODEL}) {resp.status_code}: {resp.text}")
    data = resp.json()["data"]
    ordered = sorted(data, key=lambda item: item["index"])
    return [item["embedding"] for item in ordered]


async def _embed_texts_batched(
    texts: list[str],
    on_batch_done: "callable | None" = None,
) -> list[list[float]]:
    if not texts:
        return []
    all_vectors = []
    total_batches = (len(texts) + EMBED_BATCH_SIZE - 1) // EMBED_BATCH_SIZE
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i:i + EMBED_BATCH_SIZE]
        batch_num = i // EMBED_BATCH_SIZE + 1
        print(f"[embedding] envoi du sous-lot {batch_num} ({len(batch)} chunks)...")
        vectors = await _call_with_retry(_embed_texts, batch)
        all_vectors.extend(vectors)
        if on_batch_done:
            on_batch_done(batch_num, total_batches, len(all_vectors), len(texts))
    return all_vectors


def _upsert_chroma_batched(
    ids, embeddings, documents, metadatas,
    on_batch_done: "callable | None" = None,
):
    total = len(ids)
    for i in range(0, total, CHROMA_UPSERT_BATCH_SIZE):
        sl = slice(i, i + CHROMA_UPSERT_BATCH_SIZE)
        _collection.upsert(
            ids=ids[sl], embeddings=embeddings[sl],
            documents=documents[sl], metadatas=metadatas[sl],
        )
        if on_batch_done:
            on_batch_done(min(i + CHROMA_UPSERT_BATCH_SIZE, total), total)


# ─── Extraction VLM parallélisée avec retry ──────────────────────────────

async def _extract_page_text_safe(image_b64: str, mime: str, page_index: int) -> tuple[int, str]:
    async with _vlm_semaphore:
        print(f"[vlm] extraction page {page_index + 1} en cours...")
        text = await _call_with_retry(_extract_page_text, image_b64, mime)
        print(f"[vlm] extraction page {page_index + 1} terminée")
    return page_index, text


async def _extract_pages_parallel(pages_data: list[tuple[int, str, str]]) -> dict[int, str]:
    """
    pages_data: liste de (page_index, image_b64, mime).
    Retourne {page_index: texte_extrait}.
    """
    tasks = [
        _extract_page_text_safe(image_b64, mime, page_index)
        for page_index, image_b64, mime in pages_data
    ]
    results = await asyncio.gather(*tasks)
    return dict(results)


# ─── Extraction PDF / image (pipeline classique, petits documents) ──────

async def _extract_markdown(path: Path) -> str:
    """Extrait le texte Markdown complet d'un fichier PDF ou image."""
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        doc = fitz.open(str(path))
        total_pages = len(doc)
        pages_md = [None] * total_pages

        pages_data = []
        for page_index in range(total_pages):
            page = doc.load_page(page_index)
            pix = page.get_pixmap(dpi=200)
            image_b64 = base64.b64encode(pix.tobytes("png")).decode()
            pages_data.append((page_index, image_b64, "image/png"))
        doc.close()

        extracted = await _extract_pages_parallel(pages_data)
        for page_index, text in extracted.items():
            pages_md[page_index] = f"<!-- page {page_index + 1} -->\n{text}"

        return "\n\n".join(pages_md)

    elif suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        mime = "image/png" if suffix == ".png" else "image/jpeg"
        image_b64 = base64.b64encode(path.read_bytes()).decode()
        print("[vlm] extraction image unique en cours...")
        text = await _call_with_retry(_extract_page_text, image_b64, mime)
        print("[vlm] extraction image unique terminée")
        return text

    raise ValueError(f"Type de fichier non supporté : {suffix}")


def _safe_stem(path: Path) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in path.stem)


# ─── Pipeline "gros document" : traitement par lots de N pages ──────────

async def _ingest_large_pdf(path: Path) -> dict:
    stem = _safe_stem(path)
    doc = fitz.open(str(path))
    total_pages = len(doc)
    total_chunks_indexed = 0
    batch_reports = []

    print(f"[document] '{path.name}' : {total_pages} pages détectées, traitement par lots de {BATCH_PAGE_SIZE}")

    try:
        for batch_start in range(0, total_pages, BATCH_PAGE_SIZE):
            batch_end = min(batch_start + BATCH_PAGE_SIZE, total_pages)
            batch_num = (batch_start // BATCH_PAGE_SIZE) + 1
            batch_pages = list(range(batch_start, batch_end))

            page_start, page_end = batch_start + 1, batch_end
            # Label de fichier basé sur les pages plutôt que sur un numéro de
            # lot arbitraire : "page0007" si un seul page (cas par défaut,
            # BATCH_PAGE_SIZE=1), "page0007-0011" si le lot couvre plusieurs
            # pages (BATCH_PAGE_SIZE > 1).
            page_label = (
                f"page{page_start:04d}"
                if page_start == page_end
                else f"page{page_start:04d}-{page_end:04d}"
            )

            print(f"[lot {batch_num}] pages {page_start} à {page_end} / {total_pages}")

            # 1. Rendu image de chaque page du lot
            pages_data = []
            for page_index in batch_pages:
                page = doc.load_page(page_index)
                pix = page.get_pixmap(dpi=200)
                image_b64 = base64.b64encode(pix.tobytes("png")).decode()
                pages_data.append((page_index, image_b64, "image/png"))

            # 2. VLM en parallèle (limité par sémaphore) + retry
            extracted = await _extract_pages_parallel(pages_data)
            batch_markdown = "\n\n".join(
                f"<!-- page {idx + 1} -->\n{extracted[idx]}" for idx in batch_pages
            )

            # 3. Sauvegarde du markdown du lot : un fichier propre et distinct
            # par page avec BATCH_PAGE_SIZE=1 (nom-fichier-page0001.md,
            # -page0002.md, ...), ou par plage de pages sinon.
            md_path = MARKDOWN_CACHE_DIR / f"{stem}-{page_label}.md"
            md_path.write_text(batch_markdown, encoding="utf-8")
            print(f"[lot {batch_num}] markdown sauvegardé -> {md_path.name}")

            # 4. Chunking sémantique sur CE lot seulement
            print(f"[lot {batch_num}] chunking sémantique en cours...")
            chunks = await _call_with_retry(_chunk_with_llm, batch_markdown)
            print(f"[lot {batch_num}] chunking terminé ({len(chunks)} chunks)")

            if not chunks:
                batch_reports.append({"batch": batch_num, "pages": page_label, "chunks_indexed": 0})
                for page_index in batch_pages:
                    print(f"page {page_index + 1} / {total_pages} ✔️")
                continue

            chunks_path = CHUNKS_DIR / f"{stem}-{page_label}.json"
            chunks_path.write_text(
                json.dumps(
                    {"source": path.name, "batch": batch_num, "pages": page_label, "chunks": chunks},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )

            # 5. Embedding des chunks du lot (par sous-lots)
            print(f"[lot {batch_num}] embedding en cours ({len(chunks)} chunks)...")
            vectors = await _embed_texts_batched(chunks)

            # 6. Upsert Chroma du lot (par sous-lots)
            ids = [f"{stem}-{page_label}-chunk{i + 1}" for i in range(len(chunks))]
            metadatas = [
                {
                    "source": path.name,
                    "batch": batch_num,
                    "pages": page_label,
                    "chunk_index": i + 1,
                    "markdown_path": str(md_path),
                    # Inclus par défaut dans les recherches RAG. Peut être mis à 0
                    # via POST /rag/toggle-inclusion (décocher la case RAG d'un
                    # texte dans le backoffice) sans supprimer le chunk.
                    "inclus": 1,
                }
                for i in range(len(chunks))
            ]
            _upsert_chroma_batched(ids, vectors, chunks, metadatas)

            total_chunks_indexed += len(chunks)
            batch_reports.append({"batch": batch_num, "pages": page_label, "chunks_indexed": len(chunks)})

            # Affichage demandé : uniquement une fois l'embedding terminé pour le lot
            for page_index in batch_pages:
                print(f"page {page_index + 1} / {total_pages} ✔️")

    finally:
        doc.close()

    print(f"[document] '{path.name}' : traitement terminé, {total_chunks_indexed} chunks indexés au total")

    return {
        "file": path.name,
        "mode": "large_pdf_batched",
        "total_pages": total_pages,
        "batches": batch_reports,
        "chunks_indexed": total_chunks_indexed,
    }


# ─── Ingestion d'un fichier (pipeline classique, petit document) ────────

async def _ingest_file_classic(path: Path) -> dict:
    stem = _safe_stem(path)

    # 1-3 — Extraction VLM + cache Markdown
    print(f"[document] '{path.name}' : extraction du texte en cours...")
    markdown_text = await _extract_markdown(path)
    md_path = MARKDOWN_CACHE_DIR / f"{stem}.md"
    md_path.write_text(markdown_text, encoding="utf-8")
    print(f"[document] '{path.name}' : extraction terminée -> {md_path.name}")

    # 4-5 — Découpage sémantique via LLM (avec vérification de fidélité)
    print(f"[document] '{path.name}' : chunking sémantique en cours...")
    chunks = await _call_with_retry(_chunk_with_llm, markdown_text)
    print(f"[document] '{path.name}' : chunking terminé ({len(chunks)} chunks)")

    if not chunks:
        return {"file": path.name, "chunks_indexed": 0, "markdown_path": str(md_path)}

    chunks_path = CHUNKS_DIR / f"{stem}.json"
    chunks_path.write_text(
        json.dumps({"source": path.name, "chunks": chunks}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 6 — Embedding des chunks
    print(f"[document] '{path.name}' : embedding en cours ({len(chunks)} chunks)...")
    vectors = await _embed_texts_batched(chunks)

    # 7 — Stockage dans Chroma
    ids = [f"{stem}-chunk{i + 1}" for i in range(len(chunks))]
    metadatas = [
        {
            "source": path.name,
            "chunk_index": i + 1,
            "markdown_path": str(md_path),
            # Inclus par défaut dans les recherches RAG (voir commentaire
            # équivalent dans _ingest_large_pdf).
            "inclus": 1,
        }
        for i in range(len(chunks))
    ]
    _upsert_chroma_batched(ids, vectors, chunks, metadatas)

    # Affichage demandé : uniquement une fois l'embedding terminé
    total_pages = 1
    try:
        if path.suffix.lower() == ".pdf":
            doc = fitz.open(str(path))
            total_pages = len(doc)
            doc.close()
    except Exception:
        pass

    for page_index in range(total_pages):
        print(f"page {page_index + 1} / {total_pages} ✔️")

    print(f"[document] '{path.name}' : traitement terminé, {len(chunks)} chunks indexés")

    return {
        "file": path.name,
        "chunks_indexed": len(chunks),
        "markdown_path": str(md_path),
        "chunks_path": str(chunks_path),
    }


async def _ingest_file(path: Path) -> dict:
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        doc = fitz.open(str(path))
        nb_pages = len(doc)
        doc.close()

        if nb_pages > LARGE_DOC_PAGE_THRESHOLD:
            return await _ingest_large_pdf(path)

    return await _ingest_file_classic(path)


# ─── Route FastAPI ─────────────────────────────────────────────────────────

@router.post("/upload")
async def upload_and_index(
    files: list[UploadFile],
    current_user: User = Depends(get_current_user),
):
    """
    Upload un ou plusieurs PDF/images, extrait leur texte (Markdown),
    les découpe en chunks sémantiques, et les indexe dans Chroma.
    """
    results = []
    errors = []

    for upload in files:
        suffix = Path(upload.filename).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            errors.append({"file": upload.filename, "error": f"Extension non supportée : {suffix}"})
            continue

        # Dédoublonnage AVANT écriture : si un fichier du même nom existe
        # déjà dans UPLOADS_DIR, get_unique_path renvoie un chemin du type
        # "naruto(1).pdf" plutôt que d'écraser silencieusement l'existant.
        # Le nom réellement utilisé pour écrire sur disque (dest_path.name)
        # est ensuite le SEUL utilisé pour la metadata "source" dans Chroma
        # (voir _ingest_file -> _ingest_file_classic/_ingest_large_pdf, qui
        # utilisent path.name) : upload.filename (potentiellement le nom
        # d'origine, avant dédoublonnage) n'est plus utilisé que pour les
        # messages d'erreur, jamais pour l'indexation elle-même.
        dest_path = _unique_path_in(UPLOADS_DIR, upload.filename)
        content = await upload.read()
        dest_path.write_bytes(content)

        try:
            result = await _ingest_file(dest_path)
            results.append(result)
        except RuntimeError as e:
            print(f"[erreur] échec de l'indexation de '{dest_path.name}' : {e}")
            errors.append({"file": upload.filename, "error": str(e)})

    if not results and errors:
        raise HTTPException(status_code=502, detail={"errors": errors})

    return {"indexed": results, "errors": errors}


# ─── Réutilisable en dehors de l'upload direct ─────────────────────────────
# Point d'entrée public pour indexer un fichier déjà présent sur le disque
# (ex: un document déjà lié à un texte via texte_route.py, stocké dans
# UPLOAD_DIR de document_route.py, pas dans rag/uploads/). Utilisé par
# POST /documents/{document_id}/rag-include dans texte_document_route.py.
async def ingest_document(path: Path) -> dict:
    try:
        return await _ingest_file(path)
    except RuntimeError as e:
        print(f"[erreur] échec de l'indexation de '{path.name}' : {e}")
        raise HTTPException(
            status_code=502,
            detail=f"Échec de l'indexation RAG pour '{path.name}' : {e}",
        ) from e


# ─── Ingestion du contenu texte d'un texte juridique (éditeur Lexical) ────
# Contrairement à ingest_document (fichiers PDF/image, pipeline VLM), le
# contenu ici arrive déjà en HTML/texte structuré depuis document-section.tsx
# (champ contenu_html envoyé à POST /add-texte) : on saute directement au
# chunking sémantique, sans extraction VLM.

def _texte_source_name(texte_id: int) -> str:
    """Nom de 'source' utilisé en metadata Chroma en dernier recours, quand
    aucun document PDF/image n'est associé au texte (ex: texte rédigé
    entièrement à la main, sans import de fichier)."""
    return f"texte-{texte_id}"


async def ingest_texte_content(
    texte_id: int,
    html_content: str | None,
    document_names: list[str] | None = None,
) -> dict:
    """
    Découpe en chunks sémantiques le contenu HTML d'un texte juridique
    (rédigé dans l'éditeur Lexical), les embedde et les indexe dans Chroma.

    `document_names` : noms des fichiers PDF/image uploadés et liés à ce
    texte (Document.nom). Quand fourni, la metadata "source" de TOUS les
    chunks reprend le nom du PREMIER fichier de la liste (document_names[0]),
    même si plusieurs fichiers (PDF et/ou images) sont liés au texte — le
    contenu Lexical fusionne déjà tous les fichiers en un seul texte, donc
    une seule source cohérente est utilisée plutôt qu'une répartition entre
    fichiers. Si la liste est vide/absente, fallback sur "texte-{id}".

    Appelé après la création d'un texte (POST /add-texte) : tous les chunks
    obtenus sont marqués inclus=1 par défaut dans la base vectorielle,
    exactement comme pour un document uploadé (voir _ingest_file_classic).

    Best-effort : ne lève jamais d'exception (appelé en tâche de fond,
    ne doit jamais faire échouer la création du texte elle-même).
    """
    plain_text = _html_to_plain_text(html_content or "")
    if not plain_text.strip():
        _emit_ingest_progress(texte_id, {"type": "done", "chunks_indexed": 0})
        return {"texte_id": texte_id, "chunks_indexed": 0}

    document_names = [n for n in (document_names or []) if n]

    try:
        print(f"[texte:{texte_id}] chunking sémantique du contenu en cours...")
        _emit_ingest_progress(texte_id, {"type": "chunking_start"})
        chunks = await _call_with_retry(_chunk_with_llm, plain_text)
        print(f"[texte:{texte_id}] chunking terminé ({len(chunks)} chunks)")
        _emit_ingest_progress(
            texte_id, {"type": "chunking_done", "chunks_total": len(chunks)}
        )

        if not chunks:
            _emit_ingest_progress(texte_id, {"type": "done", "chunks_indexed": 0})
            return {"texte_id": texte_id, "chunks_indexed": 0}

        print(f"[texte:{texte_id}] embedding en cours ({len(chunks)} chunks)...")

        def _on_embed_batch(batch_num: int, total_batches: int, done: int, total: int):
            _emit_ingest_progress(
                texte_id,
                {
                    "type": "embedding_progress",
                    "batch": batch_num,
                    "total_batches": total_batches,
                    "chunks_embedded": done,
                    "chunks_total": total,
                },
            )

        vectors = await _embed_texts_batched(chunks, on_batch_done=_on_embed_batch)

        # ── Résolution de la source par chunk ────────────────────────────
        # Un id Chroma dérivé UNIQUEMENT du nom de fichier entrerait en
        # collision avec les chunks du même fichier indexés via
        # /rag/upload (même stem -> mêmes ids -> upsert qui écrase). On
        # préfixe donc toujours par "texte{id}-", même quand la source
        # affichée est un nom de fichier.
        def _source_for(i: int) -> str:
            # Tous les chunks partagent la même source : le premier fichier
            # lié au texte (document_names[0]) s'il y en a un, sinon
            # "texte-{id}". `i` n'est plus utilisé pour varier la source
            # (voir docstring ci-dessus).
            if not document_names:
                return _texte_source_name(texte_id)
            return document_names[0]

        ids = [
            f"texte{texte_id}-{_safe_stem(Path(_source_for(i)))}-chunk{i + 1}"
            for i in range(len(chunks))
        ]
        metadatas = [
            {
                "source": _source_for(i),
                "texte_id": texte_id,
                "chunk_index": i + 1,
                # Inclus par défaut dans les recherches RAG, comme pour les
                # documents uploadés (voir POST /rag/toggle-inclusion pour
                # décocher explicitement).
                "inclus": 1,
            }
            for i in range(len(chunks))
        ]
        def _on_upsert_batch(done: int, total: int):
            _emit_ingest_progress(
                texte_id,
                {"type": "upsert_progress", "chunks_upserted": done, "chunks_total": total},
            )

        _upsert_chroma_batched(ids, vectors, chunks, metadatas, on_batch_done=_on_upsert_batch)

        print(f"[texte:{texte_id}] indexation terminée, {len(chunks)} chunks indexés")
        _emit_ingest_progress(
            texte_id, {"type": "done", "chunks_indexed": len(chunks)}
        )

        return {
            "texte_id": texte_id,
            "chunks_indexed": len(chunks),
        }
    except Exception as e:
        # Best-effort : on log et on n'interrompt jamais l'appelant (tâche
        # de fond déclenchée après la création réussie du texte).
        print(f"[texte:{texte_id}] échec de l'indexation RAG du contenu : {e}")
        _emit_ingest_progress(texte_id, {"type": "error", "message": str(e)})
        return {"texte_id": texte_id, "chunks_indexed": 0, "error": str(e)}


async def reindex_texte_content(
    texte_id: int,
    html_content: str | None,
    document_names: list[str] | None = None,
) -> dict:
    """
    Ré-indexation COMPLÈTE du contenu d'un texte lors de sa modification :
    supprime d'abord tous les chunks Chroma déjà associés à ce texte_id
    (cleanup_rag_data_for_texte), puis relance exactement le même pipeline
    que pour une création (ingest_texte_content) — nouveau chunking
    sémantique, nouveaux embeddings, nouveaux ids Chroma.

    Appelé en tâche de fond depuis PUT /textes/{id} UNIQUEMENT si le
    contenu HTML a réellement changé (voir update_texte) — inutile de
    tout ré-embedder si seul le titre ou la catégorie a été modifié.

    On ne tente pas de faire un diff chunk-par-chunk pour ne remplacer que
    ce qui a changé : un ré-chunking sémantique peut redécouper le texte
    entier différemment (limites de chunks déplacées), donc un id de chunk
    "chunk3" d'avant ne correspond plus forcément au même passage après
    modification. Supprimer-puis-réinsérer entièrement est la seule
    approche qui garantit qu'aucun chunk obsolète (contenu qui n'existe
    plus dans la nouvelle version du texte) ne reste indexé par erreur.

    Best-effort comme ingest_texte_content : ne lève jamais d'exception.
    """
    print(f"[texte:{texte_id}] modification détectée — suppression des chunks existants avant ré-indexation")
    cleanup_rag_data_for_texte(texte_id)
    return await ingest_texte_content(texte_id, html_content, document_names)


@router.get("/ingest-progress/{texte_id}", summary="Flux SSE de progression de l'indexation RAG d'un texte")
async def stream_ingest_progress(texte_id: int):
    """
    Retransmet en direct (Server-Sent Events) la progression du chunking
    sémantique, de l'embedding et de l'upsert Chroma pour un texte donné.

    À appeler côté frontend juste après le succès de POST /textes ou
    PUT /textes/{id} (l'id du texte est connu à ce moment) : ces routes
    déclenchent `ingest_texte_content` en tâche de fond (BackgroundTasks),
    et ce endpoint ne fait qu'écouter les events que cette tâche publie via
    `_emit_ingest_progress` pendant son exécution.

    Le flux se termine de lui-même dès qu'un event "done" ou "error" est
    reçu. Si la tâche de fond ne démarre jamais (ex: contenu vide), le
    premier event ("done", chunks_indexed=0) est envoyé immédiatement par
    `ingest_texte_content` — le flux ne reste donc jamais ouvert
    indéfiniment sans raison.
    """
    queue = _ingest_progress_queues.get(texte_id)
    if queue is None:
        queue = asyncio.Queue()
        _ingest_progress_queues[texte_id] = queue

    async def event_generator():
        try:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
        finally:
            # Ne retire la queue que si c'est toujours la nôtre : si un
            # nouveau flux a été ouvert entre-temps pour le même texte_id
            # (ex: reconnexion), on ne veut pas lui couper l'herbe sous le
            # pied en supprimant SA queue par erreur.
            if _ingest_progress_queues.get(texte_id) is queue:
                _ingest_progress_queues.pop(texte_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def cleanup_rag_data_for_texte(texte_id: int) -> dict:
    """Supprime les chunks Chroma issus du CONTENU d'un texte lui-même
    (indexés par ingest_texte_content), à appeler lors de la suppression
    d'un texte si on veut aussi nettoyer son propre contenu indexé (à ne
    pas confondre avec ses documents joints, cf. cleanup_rag_data(nom_fichier)).

    Filtre sur la metadata "texte_id" plutôt que sur "source" : depuis que
    la source peut être un nom de fichier partagé avec le document
    lui-même (voir ingest_texte_content), un filtre par nom de fichier
    risquerait de supprimer aussi les chunks issus de /rag/upload pour ce
    même fichier."""
    try:
        existing = _collection.get(where={"texte_id": texte_id})
        ids_to_delete = existing.get("ids", [])
        if ids_to_delete:
            _collection.delete(where={"texte_id": texte_id})
        print(f"[cleanup] texte {texte_id} : {len(ids_to_delete)} vecteur(s) Chroma supprimés")
        return {"texte_id": texte_id, "chroma_vectors_deleted": len(ids_to_delete)}
    except Exception as e:
        print(f"[cleanup] texte {texte_id} : erreur -> {e}")
        return {"texte_id": texte_id, "chroma_vectors_deleted": 0, "error": str(e)}


# ─── Inclusion / exclusion des chunks d'un document dans la recherche RAG ─
# Bascule le champ metadata "inclus" (0 ou 1) sur TOUS les chunks Chroma
# rattachés à un nom de fichier donné, sans les supprimer. rag_retriever.py
# filtre ensuite sur where={"inclus": 1} lors de la recherche sémantique :
# un chunk à inclus=0 reste stocké (et peut être réactivé plus tard) mais
# n'est jamais utilisé pour répondre aux questions du chatbot.

def set_inclus_for_source(nom_fichier: str, inclus: int) -> int:
    """
    Met à jour le champ metadata "inclus" (0 ou 1) de tous les chunks dont
    metadata["source"] == nom_fichier. Retourne le nombre de chunks modifiés.
    """
    existing = _collection.get(where={"source": nom_fichier})
    ids = existing.get("ids", [])
    if not ids:
        return 0

    metadatas = existing.get("metadatas") or [{} for _ in ids]
    updated_metadatas = []
    for meta in metadatas:
        meta = dict(meta or {})
        meta["inclus"] = inclus
        updated_metadatas.append(meta)

    _collection.update(ids=ids, metadatas=updated_metadatas)
    return len(ids)


def set_inclus_for_sources(noms_fichiers: list[str], inclus: int) -> dict[str, int]:
    """
    Applique set_inclus_for_source à une liste de fichiers (ex: tous les
    documents liés à un texte). Retourne {nom_fichier: nb_chunks_modifiés}.
    """
    return {nom: set_inclus_for_source(nom, inclus) for nom in noms_fichiers}


def get_inclus_status_for_sources(noms_fichiers: list[str]) -> dict[str, bool]:
    """
    Pour chaque nom de fichier, indique si SES chunks sont actuellement
    inclus dans le RAG. Un document est considéré "inclus" seulement s'il
    existe AU MOINS un chunk Chroma dont metadata["source"] correspond
    exactement à ce nom de fichier ET dont metadata["inclus"] == 1.

    Strict par construction : un document sans chunk correspondant en base
    (pas encore indexé, ou nom de source différent) est considéré NON
    inclus (False), pour éviter de précocher à tort des documents qui
    n'ont pas de contrepartie dans ChromaDB.
    """
    status: dict[str, bool] = {}
    for nom in noms_fichiers:
        existing = _collection.get(where={"source": nom}, include=["metadatas"])
        metadatas = existing.get("metadatas") or []
        if not metadatas:
            status[nom] = False
            continue
        status[nom] = any((m or {}).get("inclus", 1) == 1 for m in metadatas)
    return status


class RagInclusionTogglePayload(BaseModel):
    sources: list[str]
    inclus: int  # 0 ou 1


@router.post("/toggle-inclusion")
async def toggle_rag_inclusion(
    payload: RagInclusionTogglePayload,
    current_user: User = Depends(get_current_user),
):
    """
    Coche/décoche l'inclusion RAG de tous les chunks appartenant aux
    fichiers listés dans `sources` (noms tels que stockés dans
    metadata["source"], ex: les documents liés à un texte). N'affecte
    jamais les chunks des AUTRES documents.
    """
    if payload.inclus not in (0, 1):
        raise HTTPException(status_code=422, detail="inclus doit valoir 0 ou 1.")

    updated = set_inclus_for_sources(payload.sources, payload.inclus)
    total = sum(updated.values())
    return {"inclus": payload.inclus, "chunks_updated": total, "details": updated}


@router.get("/inclusion-status")
async def get_rag_inclusion_status(
    sources: str,
    current_user: User = Depends(get_current_user),
):
    """
    GET /rag/inclusion-status?sources=fichier1.pdf,fichier2.pdf
    Retourne l'état d'inclusion (True/False) par nom de fichier.
    """
    noms = [s for s in sources.split(",") if s.strip()]
    return get_inclus_status_for_sources(noms)


def is_document_indexed(nom_fichier: str) -> bool:
    """
    True si un fichier de ce nom a déjà un .md en cache (donc déjà indexé).
    Gère les deux pipelines : fichier classique ("stem.md") ou par lots
    ("stem-001.md", "stem-002.md", ...).
    """
    stem = _safe_stem(Path(nom_fichier))
    if (MARKDOWN_CACHE_DIR / f"{stem}.md").exists():
        return True
    return any(MARKDOWN_CACHE_DIR.glob(f"{stem}-*.md"))


# ─── Nettoyage complet des données RAG liées à un document ───────────────

def cleanup_rag_data(nom_fichier: str) -> dict:
    """
    Supprime TOUT ce qui a été généré par l'ingestion RAG d'un document :
      - les markdowns en cache (pipeline classique "stem.md" ET pipeline
        par lots "stem-001.md", "stem-002.md", ...)
      - les fichiers de chunks JSON (idem, "stem.json" et "stem-001.json", ...)
      - les entrées Chroma dont le metadata "source" correspond au fichier

    `nom_fichier` doit être le nom de fichier tel que stocké dans la
    metadata "source" lors de l'ingestion (path.name, ex: "rapport.pdf").

    Best-effort : chaque suppression est isolée, une erreur sur l'une
    n'empêche pas les autres (on ne veut pas bloquer la suppression du
    document en base pour un souci de nettoyage de cache).
    """
    stem = _safe_stem(Path(nom_fichier))
    removed_markdowns = []
    removed_chunks = []
    chroma_deleted = 0
    errors = []

    # 1. Markdowns : "stem.md" (classique) + "stem-NNN.md" (par lots)
    md_candidates = list(MARKDOWN_CACHE_DIR.glob(f"{stem}.md")) + list(
        MARKDOWN_CACHE_DIR.glob(f"{stem}-*.md")
    )
    for md_path in md_candidates:
        try:
            md_path.unlink(missing_ok=True)
            removed_markdowns.append(md_path.name)
        except OSError as e:
            errors.append(f"markdown {md_path.name}: {e}")

    # 2. Chunks JSON : "stem.json" (classique) + "stem-NNN.json" (par lots)
    chunk_candidates = list(CHUNKS_DIR.glob(f"{stem}.json")) + list(
        CHUNKS_DIR.glob(f"{stem}-*.json")
    )
    for chunk_path in chunk_candidates:
        try:
            chunk_path.unlink(missing_ok=True)
            removed_chunks.append(chunk_path.name)
        except OSError as e:
            errors.append(f"chunks {chunk_path.name}: {e}")

    # 3. Entrées Chroma : toutes celles dont metadata["source"] == nom_fichier
    try:
        existing = _collection.get(where={"source": nom_fichier})
        ids_to_delete = existing.get("ids", [])
        if ids_to_delete:
            _collection.delete(where={"source": nom_fichier})
            chroma_deleted = len(ids_to_delete)
    except Exception as e:
        errors.append(f"chroma: {e}")

    print(
        f"[cleanup] '{nom_fichier}' : {len(removed_markdowns)} markdown(s), "
        f"{len(removed_chunks)} fichier(s) de chunks, {chroma_deleted} vecteur(s) Chroma supprimés"
    )
    if errors:
        print(f"[cleanup] '{nom_fichier}' : erreurs rencontrées -> {errors}")

    return {
        "file": nom_fichier,
        "markdowns_removed": removed_markdowns,
        "chunks_removed": removed_chunks,
        "chroma_vectors_deleted": chroma_deleted,
        "errors": errors,
    }