import json
import logging
import traceback
from datetime import datetime
from pathlib import Path
from typing import List

from src.backend.schemas.texte_schemas import TexteResponse
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Cookie,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from pydantic import BaseModel, ValidationError
from src.backend.models import Statut
from src.backend.db.database import db_dependency
from src.backend.models import (
    Categorie,
    Document,
    Historique,
    Liens_Utile,
    Statut, Texte, Texte_Document, Texte_Reference, Texte_Theme, Theme,
)
from src.backend.routes.document_route import (
    UPLOAD_DIR,
    delete_file_from_disk,
    get_unique_path,
    persist_uploaded_file,
)
from src.backend.schemas.texte_reference_schemas import TexteReferenceInput
from src.backend.schemas.lien_utile_schemas import LienUtileInput, LienUtileResponse
from src.backend.schemas.texte_schemas import TexteCreate, TexteUpdate, DeleteTextesPayload
from src.backend.auth.auth import verify_token
from src.backend.models.user import User
from src.backend.models.qualites_documents import QualiteDocument
from src.backend.stats import image_stats, pdf_stats, is_image, is_pdf
from src.backend.routes.rag_route import (
    ingest_texte_content,
    reindex_texte_content,
    cleanup_rag_data,
    cleanup_rag_data_for_texte,
)

logger = logging.getLogger(__name__)


def _persist_qualites(db, document_id: int, file_path):
    """Calcule les statistiques de qualité du fichier (image ou PDF) et
    insère une ligne par page dans la table `qualites_documents`.
    Utilise un savepoint pour que toute erreur ici n'annule pas la
    transaction principale (texte + documents + thèmes restent committés)."""
    try:
        path = Path(file_path)
        if is_pdf(path):
            pages = pdf_stats(path)
        elif is_image(path):
            pages = [image_stats(path)]
        else:
            return  # type de fichier non supporté, on ignore silencieusement

        # Savepoint : si l'insertion des qualités échoue, on ne revient
        # qu'à ce point-ci, sans annuler tout ce qui précède.
        with db.begin_nested():
            for page_data in pages:
                qualite = QualiteDocument(
                    document_id=document_id,
                    page=page_data.get("page", 1),
                    blur=page_data.get("blur"),
                    skew=page_data.get("skew"),
                    noise_score=page_data.get("noise_score"),
                    black_pixel_ratio=page_data.get("black_pixel_ratio"),
                    entropy=page_data.get("entropy"),
                    brightness=page_data.get("brightness"),
                )
                db.add(qualite)
    except Exception:
        logger.warning(
            "Impossible de calculer les qualités pour document_id=%s", document_id
        )
        logger.warning(traceback.format_exc())


def get_current_numero_user(
    db: db_dependency,
    access_token: str | None = Cookie(default=None),
) -> str | None:
    """Résout le `numero` de l'utilisateur courant à partir du cookie
    `access_token`, pour l'enregistrer dans l'historique (FK vers
    users.numero). Retourne None si non authentifié plutôt que de lever une
    erreur : on ne veut pas bloquer une modification de texte juste parce
    qu'on ne peut pas identifier son auteur.
    """
    if not access_token:
        return None
    payload = verify_token(access_token)
    if not payload:
        return None
    user = db.query(User).filter(User.numero == payload.get("sub")).first()
    if not user:
        return None
    return user.numero


def _format_validation_errors(exc: ValidationError) -> str:
    """Convertit une ValidationError pydantic en message texte lisible,
    ex: "categorie_id: field required ; theme_ids.0: Input should be a
    valid integer". On évite de renvoyer exc.errors() brut (liste
    d'objets) : le frontend affiche `detail` tel quel dans le DOM, et
    React plante si ce n'est pas une chaîne de caractères."""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(piece) for piece in err.get("loc", []))
        parts.append(f"{loc}: {err.get('msg')}" if loc else err.get("msg", "valeur invalide"))
    return " ; ".join(parts) if parts else "Données invalides."
 
router = APIRouter()

@router.get("/textes", response_model=list[TexteResponse])
def get_all_textes(db: db_dependency):
    result = (
        db.query(
            Texte,
            Theme.nom.label("theme"),
            Categorie.nom.label("categorie_nom"),
            Statut.nom.label("statut_nom"),
        )
        .join(Categorie, Texte.categorie_id == Categorie.id)
        .join(Statut, Texte.statut_id == Statut.id)
        .outerjoin(Texte_Theme, Texte.id == Texte_Theme.texte_id)
        .outerjoin(Theme, Theme.id == Texte_Theme.theme_id)
        .order_by(Texte.id.asc())
        .all()
    )

    textes_dict = {}

    for texte, theme, categorie_nom, statut_nom in result:
        if texte.id not in textes_dict:
            textes_dict[texte.id] = {
                **texte.__dict__,
                "categorie": categorie_nom,
                "statut": statut_nom,
                "themes": [],
            }
            textes_dict[texte.id].pop("_sa_instance_state", None)

        if theme:
            textes_dict[texte.id]["themes"].append(theme)

    return list(textes_dict.values())


# ─── Liste paginée ("voir plus") ─────────────────────────────────────────────
# GET /textes retourne tout (utilisé ailleurs, ex: fetchTextesDocuments côté
# frontend qui a besoin de l'ensemble). Cette route sert spécifiquement
# TextesSection : 10 textes à la fois, avec le nombre total et un indicateur
# de page suivante, pour éviter de tout charger d'un coup.
class PaginatedTextesResponse(BaseModel):
    items: List[TexteResponse]
    total: int
    has_more: bool


@router.get("/textes/paginated", response_model=PaginatedTextesResponse)
def get_textes_paginated(db: db_dependency, limit: int = 10, offset: int = 0):
    total = db.query(Texte.id).count()

    # Sous-requête pour figer QUELS textes sont sur cette page (id desc = plus
    # récents d'abord), avant de rejoindre thèmes/catégorie/statut — sinon le
    # LIMIT/OFFSET porterait sur les lignes du join (dupliquées par thème),
    # pas sur les textes eux-mêmes.
    texte_ids_page = [
        row[0]
        for row in (
            db.query(Texte.id)
            .order_by(Texte.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
    ]

    if not texte_ids_page:
        return PaginatedTextesResponse(items=[], total=total, has_more=False)

    result = (
        db.query(
            Texte,
            Theme.nom.label("theme"),
            Categorie.nom.label("categorie_nom"),
            Statut.nom.label("statut_nom"),
        )
        .join(Categorie, Texte.categorie_id == Categorie.id)
        .join(Statut, Texte.statut_id == Statut.id)
        .outerjoin(Texte_Theme, Texte.id == Texte_Theme.texte_id)
        .outerjoin(Theme, Theme.id == Texte_Theme.theme_id)
        .filter(Texte.id.in_(texte_ids_page))
        .all()
    )

    textes_dict = {}
    for texte, theme, categorie_nom, statut_nom in result:
        if texte.id not in textes_dict:
            textes_dict[texte.id] = {
                **texte.__dict__,
                "categorie": categorie_nom,
                "statut": statut_nom,
                "themes": [],
            }
            textes_dict[texte.id].pop("_sa_instance_state", None)

        if theme:
            textes_dict[texte.id]["themes"].append(theme)

    # Le join ne garantit pas l'ordre : on réordonne selon texte_ids_page.
    items = [textes_dict[tid] for tid in texte_ids_page if tid in textes_dict]

    return PaginatedTextesResponse(
        items=items,
        total=total,
        has_more=offset + len(texte_ids_page) < total,
    )


@router.get("/textes/{id}", response_model=TexteResponse)
def get_texte_by_id(id: int, db: db_dependency):
    result = (
        db.query(
            Texte,
            Categorie.nom.label("categorie_nom"),
            Statut.nom.label("statut_nom"),
        )
        .outerjoin(Categorie, Texte.categorie_id == Categorie.id)
        .outerjoin(Statut, Texte.statut_id == Statut.id)
        .filter(Texte.id == id)
        .first()
    )

    if not result:
        raise HTTPException(status_code=404, detail="Texte introuvable")

    texte, categorie_nom, statut_nom = result

    data = {
        **texte.__dict__,
        "categorie": categorie_nom,
        "statut": statut_nom,
        "themes": [],
    }
    data.pop("_sa_instance_state", None)

    return data


@router.get("/textes/{id}/liens-utiles", response_model=list[LienUtileResponse])
def get_liens_utiles_by_texte_id(id: int, db: db_dependency):
    """Liste des liens utiles rattachés à un texte donné. Utilisé côté
    frontend pour précharger la section 'Liens utiles' en mode édition
    (la route /textes/{id} ne les inclut pas)."""
    return (
        db.query(Liens_Utile)
        .filter(Liens_Utile.texte_id == id)
        .order_by(Liens_Utile.id.asc())
        .all()
    )


@router.post("/add-texte", response_model=TexteResponse, status_code=201)
def add_texte(
    db: db_dependency,
    background_tasks: BackgroundTasks,
    texte: str = Form(
        ...,
        description="Champs du texte, sérialisés en JSON (mêmes clés que TexteCreate).",
    ),
    references: str = Form(
        "[]",
        description="Liste de références liées, sérialisée en JSON (voir TexteReferenceInput).",
    ),
    liens_utiles: str = Form(
        "[]",
        description="Liste de liens utiles liés, sérialisée en JSON (voir LienUtileInput).",
    ),
    files: List[UploadFile] = File(
        default_factory=list,
        description="Fichiers (images et/ou PDF) à rattacher au texte.",
    ),
):
    # ── Décodage des champs JSON imbriqués dans le formulaire ────────────────
    # /add-texte passe en multipart/form-data (pour transporter les fichiers
    # en même temps), donc le payload "texte" ne peut plus arriver comme
    # body JSON classique : il est envoyé en tant que champ Form contenant
    # une chaîne JSON, qu'on valide nous-mêmes contre TexteCreate.
    #
    # Important : en cas d'erreur de validation, on renvoie une CHAÎNE de
    # caractères lisible dans `detail` (et pas exc.errors(), qui est une
    # liste d'objets {type, loc, msg, input} — le frontend affiche `detail`
    # tel quel dans le DOM, et React plante si ce n'est pas une string).
    try:
        payload = TexteCreate(**json.loads(texte))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Champ 'texte' invalide (JSON) : {exc}")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_format_validation_errors(exc))

    try:
        references_data = json.loads(references)
        references_payload = [TexteReferenceInput(**r) for r in references_data]
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Champ 'references' invalide (JSON) : {exc}")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_format_validation_errors(exc))

    try:
        liens_utiles_data = json.loads(liens_utiles)
        liens_utiles_payload = [LienUtileInput(**l) for l in liens_utiles_data]
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Champ 'liens_utiles' invalide (JSON) : {exc}")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_format_validation_errors(exc))

    # Vérification que la catégorie et le statut existent
    categorie = db.query(Categorie).filter(Categorie.id == payload.categorie_id).first()
    if not categorie:
        raise HTTPException(status_code=404, detail="Catégorie introuvable")

    statut = db.query(Statut).filter(Statut.id == payload.statut_id).first()
    if not statut:
        raise HTTPException(status_code=404, detail="Statut introuvable")

    # Création du texte (on exclut theme_ids qui n'est pas une colonne de Texte)
    # Important : on ne commit PLUS ici. Tout (texte + thèmes + fichiers +
    # références + liens utiles) doit être validé dans UNE SEULE transaction,
    # sinon un échec plus loin (ex: insertion d'un lien utile) ne peut plus
    # annuler le texte déjà committé — c'était le bug précédent : le texte
    # était enregistré même quand les liens utiles échouaient silencieusement.
    data = payload.dict(exclude={"theme_ids"})
    nouveau_texte = Texte(**data)

    try:
        db.add(nouveau_texte)
        db.flush()  # obtient nouveau_texte.id sans clôturer la transaction

        # ── Liaison des thèmes (table d'association Texte_Theme) ────────────
        themes_noms = []
        if payload.theme_ids:
            for theme_id in payload.theme_ids:
                theme = db.query(Theme).filter(Theme.id == theme_id).first()
                if theme:
                    db.add(Texte_Theme(texte_id=nouveau_texte.id, theme_id=theme_id))
                    themes_noms.append(theme.nom)

        # ── Fichiers : sauvegarde disque (avec dédoublonnage) + ligne
        # `documents` + association `textes_documents` ──────────────────────
        documents_crees = []
        for file in files:
            metadata = persist_uploaded_file(file)
            document = Document(**metadata)
            db.add(document)
            db.flush()  # pour obtenir document.id sans clôturer la transaction
            db.add(Texte_Document(texte_id=nouveau_texte.id, document_id=document.id))
            # Calcul et persistance des statistiques de qualité du fichier
            _persist_qualites(db, document.id, UPLOAD_DIR / document.nom)
            documents_crees.append(document)

        # ── Références liées (table textes_reference) ───────────────────────
        for reference in references_payload:
            db.add(Texte_Reference(texte_id=nouveau_texte.id, **reference.dict()))

        # ── Liens utiles (table liens_utiles) ────────────────────────────────
        for lien in liens_utiles_payload:
            db.add(Liens_Utile(texte_id=nouveau_texte.id, **lien.dict()))

        db.commit()
    except Exception:
        db.rollback()
        logger.error("Échec lors de la création du texte (ou de ses thèmes/fichiers/références/liens utiles)")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail="Le texte n'a pas pu être enregistré avec ses thèmes/fichiers/références/liens utiles.",
        )

    db.refresh(nouveau_texte)

    # ── Chunking RAG du contenu texte (éditeur Lexical) ──────────────────
    # Uniquement à la création (pas sur update_texte) : découpe le contenu
    # HTML en chunks sémantiques, les embedde et les indexe dans Chroma,
    # tous marqués inclus=1 par défaut. Exécuté en tâche de fond pour ne
    # pas retarder la réponse au frontend (appels LLM potentiellement
    # longs) ; échec silencieux (best-effort, voir ingest_texte_content).
    if nouveau_texte.contenu_html:
        # Pas besoin de créer la queue de progression explicitement ici :
        # _emit_ingest_progress (rag_route.py) la crée elle-même au premier
        # event émis si elle n'existe pas encore, et stream_ingest_progress
        # s'y abonne dès sa connexion — la queue bufferise donc déjà les
        # tout premiers events même si le frontend se connecte après le
        # démarrage de la tâche de fond ci-dessous.
        background_tasks.add_task(
            ingest_texte_content,
            nouveau_texte.id,
            nouveau_texte.contenu_html,
            [d.nom for d in documents_crees],
        )

    result_data = {
        **nouveau_texte.__dict__,
        "categorie": categorie.nom,
        "statut": statut.nom,
        "themes": themes_noms,
    }
    result_data.pop("_sa_instance_state", None)

    return result_data


@router.put("/textes/{id}", response_model=TexteResponse)
def update_texte(
    id: int,
    db: db_dependency,
    background_tasks: BackgroundTasks,
    texte: str = Form(
        ...,
        description="Champs à modifier, sérialisés en JSON (mêmes clés que TexteUpdate, mise à jour partielle).",
    ),
    # None = on ne touche pas aux références existantes (utile pour les
    # updates ponctuelles comme publish/rag qui ne renvoient pas ce champ).
    # "[]" = on veut explicitement les vider.
    references: str | None = Form(
        None,
        description="Liste souhaitée de références liées, sérialisée en JSON. Omis = inchangé.",
    ),
    # Même logique que `references` : None = inchangé, "[]" = vidé
    # explicitement. Contrairement aux références (diff fin par
    # texte_lie_id), la liste envoyée ici REMPLACE entièrement les liens
    # utiles existants, faute de clé stable côté client pour faire un diff.
    liens_utiles: str | None = Form(
        None,
        description="Liste souhaitée de liens utiles liés, sérialisée en JSON. Omis = inchangé.",
    ),
    # Liste COMPLÈTE des fichiers désormais associés au texte (existants
    # conservés + nouveaux), à ne prendre en compte QUE si `files_provided`
    # est vrai (voir plus bas). Un champ `File` multipart ne permet pas de
    # distinguer nativement "absent" de "envoyé vide", d'où ce flag explicite.
    files: List[UploadFile] = File(
        default_factory=list,
        description="Fichiers désormais associés au texte (existants + nouveaux).",
    ),
    # False (défaut) = on ne touche PAS aux documents déjà liés, même si
    # `files` est vide (même logique que `references`/`liens_utiles` omis).
    # True = `files` représente l'état complet voulu, et tout document lié
    # absent de cette liste est retiré. Permet aux updates ponctuelles
    # (ex. toggle publish/rag) de ne jamais supprimer les documents liés
    # par accident faute d'avoir renvoyé la liste complète des fichiers.
    files_provided: bool = Form(
        False,
        description="True si `files` reflète l'état complet voulu des documents liés. False = documents liés inchangés.",
    ),
    # `rag` et `publish` sont des toggles d'état pilotés exclusivement par
    # leurs routes dédiées côté frontend (updateTexteRag / updateTextePublish).
    # Le formulaire d'édition générique du texte NE DOIT JAMAIS pouvoir les
    # modifier, même par accident (ex : state local du formulaire réinitialisé
    # à une valeur par défaut et renvoyé sans que l'utilisateur y touche) — un
    # tel envoi silencieux écraserait l'état RAG/publication réel du texte
    # sans que rien ne change côté ChromaDB, rendant la case décochée
    # impossible à réactiver de façon fiable depuis la table. Par défaut
    # (False), ces deux champs sont donc ignorés même s'ils sont présents
    # dans `texte`. Seules updateTexteRag/updateTextePublish envoient True.
    status_fields_provided: bool = Form(
        False,
        description="True pour autoriser la modification de `rag`/`publish` via ce endpoint. Réservé aux toggles dédiés.",
    ),
    numero_user: str | None = Depends(get_current_numero_user),
):
    texte_obj = db.query(Texte).filter(Texte.id == id).first()
    if not texte_obj:
        raise HTTPException(status_code=404, detail="Texte introuvable")

    # Snapshot avant modification : sert à détecter un changement de statut
    # et à alimenter l'historique plus bas. `contenu_html_avant` sert à
    # détecter un changement de CONTENU (déclenche une ré-indexation RAG
    # complète, voir plus bas après le commit).
    statut_id_avant = texte_obj.statut_id
    titre_avant = texte_obj.titre
    contenu_html_avant = texte_obj.contenu_html

    try:
        payload = TexteUpdate(**json.loads(texte))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Champ 'texte' invalide (JSON) : {exc}")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_format_validation_errors(exc))

    references_payload = None
    if references is not None:
        try:
            references_data = json.loads(references)
            references_payload = [TexteReferenceInput(**r) for r in references_data]
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail=f"Champ 'references' invalide (JSON) : {exc}")
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_format_validation_errors(exc))

    liens_utiles_payload = None
    if liens_utiles is not None:
        try:
            liens_utiles_data = json.loads(liens_utiles)
            liens_utiles_payload = [LienUtileInput(**l) for l in liens_utiles_data]
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail=f"Champ 'liens_utiles' invalide (JSON) : {exc}")
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_format_validation_errors(exc))

    # Ne met à jour que les champs réellement envoyés par le client
    # (permet un PUT partiel, ex. juste le titre).
    data = payload.dict(exclude_unset=True, exclude={"theme_ids"})

    # Protection contre l'écrasement accidentel de rag/publish par le
    # formulaire d'édition générique (voir doc du paramètre plus haut) :
    # ces deux champs ne sont appliqués que si explicitement autorisés.
    if not status_fields_provided:
        data.pop("rag", None)
        data.pop("publish", None)

    if "categorie_id" in data:
        categorie = db.query(Categorie).filter(Categorie.id == data["categorie_id"]).first()
        if not categorie:
            raise HTTPException(status_code=404, detail="Catégorie introuvable")

    if "statut_id" in data:
        statut = db.query(Statut).filter(Statut.id == data["statut_id"]).first()
        if not statut:
            raise HTTPException(status_code=404, detail="Statut introuvable")

    for field, value in data.items():
        setattr(texte_obj, field, value)

    # ── Historique : on trace uniquement les changements de statut ──────────
    # (création d'une ligne dans `historiques` si statut_id fait partie des
    # champs envoyés ET qu'il diffère réellement de l'ancien statut).
    nouveau_statut_id = data.get("statut_id")
    if nouveau_statut_id is not None and nouveau_statut_id != statut_id_avant:
        ancien_statut_obj = (
            db.query(Statut).filter(Statut.id == statut_id_avant).first()
            if statut_id_avant is not None
            else None
        )
        # `statut` a été chargé plus haut (bloc de vérification categorie/statut)
        # puisque "statut_id" est dans `data`.
        db.add(
            Historique(
                texte_id=texte_obj.id,
                texte_titre=data.get("titre", titre_avant),
                ancien_statut=ancien_statut_obj.nom if ancien_statut_obj else None,
                nouveau_statut=statut.nom,
                numero_user=numero_user,
            )
        )

    # Ré-association des thèmes uniquement si theme_ids a été fourni
    # (None ou absent = on laisse les thèmes existants inchangés).
    if payload.theme_ids is not None:
        db.query(Texte_Theme).filter(Texte_Theme.texte_id == texte_obj.id).delete()
        for theme_id in payload.theme_ids:
            theme = db.query(Theme).filter(Theme.id == theme_id).first()
            if theme:
                db.add(Texte_Theme(texte_id=texte_obj.id, theme_id=theme_id))

    # ── Références liées (table textes_reference) ───────────────────────────
    # On ne touche à rien si le champ n'a pas été fourni. Sinon, on compare
    # l'existant (par texte_lie_id) à la liste souhaitée : on ne supprime
    # que celles qui ont disparu et n'ajoute que celles qui sont nouvelles,
    # plutôt que de tout recréer à chaque sauvegarde.
    if references_payload is not None:
        references_existantes = (
            db.query(Texte_Reference).filter(Texte_Reference.texte_id == id).all()
        )
        existantes_par_lie_id = {
            ref.texte_lie_id: ref
            for ref in references_existantes
            if ref.texte_lie_id is not None
        }
        lie_ids_souhaites = {
            ref.texte_lie_id for ref in references_payload if ref.texte_lie_id is not None
        }

        for lie_id, ref in existantes_par_lie_id.items():
            if lie_id not in lie_ids_souhaites:
                db.delete(ref)

        for ref in references_payload:
            if ref.texte_lie_id is not None and ref.texte_lie_id not in existantes_par_lie_id:
                db.add(Texte_Reference(texte_id=id, **ref.dict()))

    # ── Liens utiles (table liens_utiles) ────────────────────────────────────
    # Pas de clé stable côté client pour ces objets (contrairement aux
    # références, qui s'appuient sur texte_lie_id, une vraie FK) : on
    # remplace donc entièrement la liste existante par la liste souhaitée
    # dès que le champ est fourni. None = on ne touche à rien.
    if liens_utiles_payload is not None:
        db.query(Liens_Utile).filter(Liens_Utile.texte_id == id).delete(
            synchronize_session=False
        )
        for lien in liens_utiles_payload:
            db.add(Liens_Utile(texte_id=id, **lien.dict()))

    # ── Documents liés (tables textes_documents / documents) ─────────────────
    # Rien n'est touché ici si `files_provided` n'est pas explicitement à
    # True (cas des updates ponctuelles comme publish/rag) : on ne veut
    # jamais détacher/supprimer des documents faute d'avoir reçu la liste
    # complète des fichiers.
    #
    # Quand `files_provided` est True, `files` représente l'état complet
    # voulu pour les documents du texte :
    #   - un fichier du payload dont le nom ET la taille correspondent à un
    #     document déjà lié = fichier inchangé → on ne fait rien ;
    #   - un fichier du payload qui ne correspond à aucun document déjà lié
    #     = nouveau fichier → on le sauvegarde sur disque et on le lie ;
    #   - un document déjà lié qui ne correspond à aucun fichier du payload
    #     = retiré côté client → on le détache (et on le supprime plus bas
    #     s'il devient orphelin).
    if files_provided:
        documents_lies_actuels = (
            db.query(Document)
            .join(Texte_Document, Texte_Document.document_id == Document.id)
            .filter(Texte_Document.texte_id == id)
            .all()
        )
        # Candidats à la suppression : réduit au fil des correspondances trouvées.
        documents_non_retrouves = {document.id: document for document in documents_lies_actuels}

        for file in files:
            content = file.file.read()
            taille = len(content)
            nom = file.filename

            match_id = next(
                (
                    doc_id
                    for doc_id, document in documents_non_retrouves.items()
                    if document.nom == nom and document.taille_octets == taille
                ),
                None,
            )

            if match_id is not None:
                # Même nom + même taille : fichier inchangé, on le retire des
                # candidats à la suppression et on ne recrée rien.
                documents_non_retrouves.pop(match_id)
                continue

            # Nouveau fichier : sauvegarde disque (avec dédoublonnage) + nouvelle
            # ligne `documents`, liée au texte.
            dest_path = get_unique_path(UPLOAD_DIR, nom)
            dest_path.write_bytes(content)
            relative_path = f"uploads/{dest_path.name}"

            document = Document(
                nom=dest_path.name,
                chemin_fichier=relative_path,
                nouveau_chemin=relative_path,
                mime_type=file.content_type,
                taille_octets=taille,
                date_upload=datetime.now(),
            )
            db.add(document)
            db.flush()  # pour obtenir document.id sans clôturer la transaction
            db.add(Texte_Document(texte_id=id, document_id=document.id))
            # Calcul et persistance des statistiques de qualité du fichier
            _persist_qualites(db, document.id, dest_path)

        # Documents restés sans correspondance dans le payload = retirés côté
        # client : on détache l'association ; ils seront supprimés plus bas s'ils
        # deviennent orphelins.
        document_ids_retires = set(documents_non_retrouves.keys())
        if document_ids_retires:
            db.query(Texte_Document).filter(
                Texte_Document.texte_id == id,
                Texte_Document.document_id.in_(document_ids_retires),
            ).delete(synchronize_session=False)

        db.commit()

        # Nettoyage des documents détachés ci-dessus devenus orphelins (plus
        # liés à aucun autre texte) : supprime le fichier physique + la ligne.
        if document_ids_retires:
            documents_encore_utilises = {
                row[0]
                for row in (
                    db.query(Texte_Document.document_id)
                    .filter(Texte_Document.document_id.in_(document_ids_retires))
                    .distinct()
                    .all()
                )
            }
            document_ids_orphelins = document_ids_retires - documents_encore_utilises

            if document_ids_orphelins:
                documents_orphelins = (
                    db.query(Document).filter(Document.id.in_(document_ids_orphelins)).all()
                )
                for document in documents_orphelins:
                    delete_file_from_disk(document.chemin_fichier)

                # Les lignes `qualites_documents` référencent `documents` via une
                # FK sans cascade : il faut les supprimer avant de pouvoir
                # supprimer les documents eux-mêmes, sinon PostgreSQL lève
                # ForeignKeyViolation (qualites_documents_document_id_fkey).
                db.query(QualiteDocument).filter(
                    QualiteDocument.document_id.in_(document_ids_orphelins)
                ).delete(synchronize_session=False)

                db.query(Document).filter(Document.id.in_(document_ids_orphelins)).delete(
                    synchronize_session=False
                )
                db.commit()
    else:
        db.commit()

    db.refresh(texte_obj)

    # ── Ré-indexation RAG si le contenu a changé ─────────────────────────
    # Ne se déclenche QUE si contenu_html a réellement changé (comparaison
    # avec le snapshot pris avant application des modifications) — inutile
    # de tout re-chunker/re-embedder si seuls le titre, la catégorie ou un
    # document joint ont été modifiés. `payload.contenu_html` peut être
    # absent du JSON reçu (update partiel) : dans ce cas `contenu_html_avant`
    # reste la valeur actuelle après refresh, donc aucun changement détecté
    # à tort.
    contenu_html_change = (
        "contenu_html" in data
        and texte_obj.contenu_html != contenu_html_avant
    )
    if contenu_html_change:
        print(f"[texte:{texte_obj.id}] contenu modifié — ré-indexation RAG complète programmée")
        noms_documents_lies = [
            d.nom
            for d in (
                db.query(Document)
                .join(Texte_Document, Texte_Document.document_id == Document.id)
                .filter(Texte_Document.texte_id == texte_obj.id)
                .all()
            )
            if d.nom
        ]
        background_tasks.add_task(
            reindex_texte_content,
            texte_obj.id,
            texte_obj.contenu_html,
            noms_documents_lies,
        )

    categorie = db.query(Categorie).filter(Categorie.id == texte_obj.categorie_id).first()
    statut = db.query(Statut).filter(Statut.id == texte_obj.statut_id).first()
    themes_noms = (
        db.query(Theme.nom)
        .join(Texte_Theme, Texte_Theme.theme_id == Theme.id)
        .filter(Texte_Theme.texte_id == texte_obj.id)
        .all()
    )

    result_data = {
        **texte_obj.__dict__,
        "categorie": categorie.nom if categorie else None,
        "statut": statut.nom if statut else None,
        "themes": [t[0] for t in themes_noms],
    }
    result_data.pop("_sa_instance_state", None)

    return result_data


@router.delete("/textes", status_code=200)
def delete_textes(payload: DeleteTextesPayload, db: db_dependency):
    if not payload.ids:
        raise HTTPException(status_code=400, detail="Aucun identifiant fourni")

    texte_ids = payload.ids

    # ── Récupération AVANT suppression des documents liés (pour pouvoir
    # nettoyer leurs qualités et leurs chunks Chroma une fois les
    # associations textes_documents supprimées plus bas). ────────────────
    documents_lies = (
        db.query(Document.id, Document.nom)
        .join(Texte_Document, Texte_Document.document_id == Document.id)
        .filter(Texte_Document.texte_id.in_(texte_ids))
        .distinct()
        .all()
    )
    document_ids_lies = [d.id for d in documents_lies]
    document_noms_lies = [d.nom for d in documents_lies if d.nom]

    # ── Qualités des documents liés à ces textes ─────────────────────────
    # Supprimées ici (indépendamment du fait que le document lui-même
    # devienne orphelin ou non), comme demandé : la suppression d'un texte
    # entraîne la suppression des lignes qualites_documents de ses documents.
    if document_ids_lies:
        db.query(QualiteDocument).filter(
            QualiteDocument.document_id.in_(document_ids_lies)
        ).delete(synchronize_session=False)

    # Pas de cascade garantie côté base de données : on nettoie explicitement
    # toutes les tables d'association avant de supprimer les textes eux-mêmes.
    db.query(Texte_Document).filter(Texte_Document.texte_id.in_(texte_ids)).delete(
        synchronize_session=False
    )
    db.query(Texte_Theme).filter(Texte_Theme.texte_id.in_(texte_ids)).delete(
        synchronize_session=False
    )
    # Un lien utile appartient à un seul texte (texte_id, FK simple) : il
    # perd son sens dès que ce texte est supprimé, donc on le supprime avec
    # lui (contrairement aux références, qui peuvent pointer VERS un texte
    # supprimé sans lui appartenir).
    db.query(Liens_Utile).filter(Liens_Utile.texte_id.in_(texte_ids)).delete(
        synchronize_session=False
    )
    # Une référence appartient à un texte (texte_id) OU pointe vers un texte
    # (texte_lie_id) : dans les deux cas, elle perd son sens si l'une des
    # deux extrémités est supprimée.
    db.query(Texte_Reference).filter(
        Texte_Reference.texte_id.in_(texte_ids)
        | Texte_Reference.texte_lie_id.in_(texte_ids)
    ).delete(synchronize_session=False)

    deleted = (
        db.query(Texte)
        .filter(Texte.id.in_(texte_ids))
        .delete(synchronize_session=False)
    )
    db.commit()

    # ── Nettoyage Chroma ──────────────────────────────────────────────────
    # 1. Chunks issus du contenu propre de chaque texte (éditeur Lexical,
    #    voir ingest_texte_content / cleanup_rag_data_for_texte).
    # 2. Chunks issus des documents PDF/image liés (voir cleanup_rag_data).
    # Best-effort : chaque fonction est déjà tolérante aux erreurs et ne
    # doit jamais faire échouer la suppression déjà committée ci-dessus.
    for texte_id in texte_ids:
        cleanup_rag_data_for_texte(texte_id)

    for nom in document_noms_lies:
        cleanup_rag_data(nom)

    # NB : la suppression des documents eux-mêmes (lignes `documents` +
    # fichiers physiques) quand ils deviennent orphelins (plus liés à
    # aucun texte) n'est pas effectuée automatiquement ici. Elle reste
    # gérée côté frontend via un appel explicite à la route
    # DELETE /documents/orphelins/{id} (voir document_route.py), qui
    # supprime aussi les qualités et les chunks Chroma restants pour ce
    # document (par sécurité, en plus du nettoyage déjà fait ci-dessus).
    return {"deleted": deleted}

@router.get("/textes-publics", response_model=list[TexteResponse])
def get_textes_publics(db: db_dependency):
    result = (
        db.query(
            Texte,
            Theme.nom.label("theme"),
            Categorie.nom.label("categorie_nom"),
            Statut.nom.label("statut_nom"),
        )
        .join(Categorie, Texte.categorie_id == Categorie.id)
        .join(Statut, Texte.statut_id == Statut.id)
        .outerjoin(Texte_Theme, Texte.id == Texte_Theme.texte_id)
        .outerjoin(Theme, Theme.id == Texte_Theme.theme_id)
        .filter(Texte.publish == 1)
        .order_by(Texte.id.asc())
        .all()
    )

    textes_dict = {}

    for texte, theme, categorie_nom, statut_nom in result:
        if texte.id not in textes_dict:
            textes_dict[texte.id] = {
                **texte.__dict__,
                "categorie": categorie_nom,
                "statut": statut_nom,
                "themes": [],
            }
            textes_dict[texte.id].pop("_sa_instance_state", None)

        if theme:
            textes_dict[texte.id]["themes"].append(theme)

    return list(textes_dict.values())

@router.get("/textes-publics/{id}", response_model=TexteResponse)
def get_texte_public_by_id(id: int, db: db_dependency):
    result = (
        db.query(
            Texte,
            Categorie.nom.label("categorie_nom"),
            Statut.nom.label("statut_nom"),
        )
        .outerjoin(Categorie, Texte.categorie_id == Categorie.id)
        .outerjoin(Statut, Texte.statut_id == Statut.id)
        .filter(Texte.id == id, Texte.publish == 1)
        .first()
    )

    if not result:
        raise HTTPException(status_code=404, detail="Texte introuvable")

    texte, categorie_nom, statut_nom = result

    themes_noms = (
        db.query(Theme.nom)
        .join(Texte_Theme, Texte_Theme.theme_id == Theme.id)
        .filter(Texte_Theme.texte_id == texte.id)
        .all()
    )

    data = {
        **texte.__dict__,
        "categorie": categorie_nom,
        "statut": statut_nom,
        "themes": [t[0] for t in themes_noms],
    }
    data.pop("_sa_instance_state", None)
    return data   # ← cette ligne manquait