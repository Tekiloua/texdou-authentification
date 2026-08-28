from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import APIRouter, File, HTTPException, UploadFile

from src.backend.db.database import db_dependency
from src.backend.models import Document, Texte_Document
from src.backend.models.qualites_documents import QualiteDocument
from src.backend.schemas.document_schemas import (
    DeleteDocumentsPayload,
    DocumentResponse,
)
from src.backend.routes.rag_route import cleanup_rag_data

router = APIRouter(tags=["Documents"])

# ─── Dossier de stockage des fichiers uploadés ──────────────────────────────
# Même emplacement que celui utilisé historiquement par /upload-file dans
# routes.py, pour que les documents créés ici et les fichiers déjà présents
# restent dans le même dossier.
BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ─── Dédoublonnage de nom de fichier ─────────────────────────────────────────
def get_unique_path(directory: Path, filename: str) -> Path:
    """Retourne un Path garanti inexistant dans `directory` pour `filename`.

    Si "rapport.pdf" existe déjà, essaie "rapport (1).pdf", puis
    "rapport (2).pdf", etc., jusqu'à trouver un nom libre.
    """
    candidate = directory / filename
    if not candidate.exists():
        return candidate

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1
    while True:
        candidate = directory / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


# ─── Sauvegarde d'un fichier uploadé (sans écriture en base) ────────────────
# Renvoie les métadonnées prêtes à être passées à Document(**metadata) ; ne
# fait pas db.add()/db.commit() elle-même, pour que l'appelant contrôle la
# transaction (ex: add_texte veut committer texte + documents ensemble).
def persist_uploaded_file(file: UploadFile) -> dict:
    dest_path = get_unique_path(UPLOAD_DIR, file.filename)
    content = file.file.read()
    dest_path.write_bytes(content)

    # Chemin relatif au dossier uploads, pratique pour reconstruire une URL
    # de téléchargement côté frontend sans exposer le chemin absolu serveur.
    relative_path = f"uploads/{dest_path.name}"

    return {
        "nom": dest_path.name,
        "chemin_fichier": relative_path,
        "nouveau_chemin": relative_path,
        "mime_type": file.content_type,
        "taille_octets": len(content),
        "date_upload": datetime.now(),
    }


# ─── GET /documents ───────────────────────────────────────────────────────────
@router.get("/documents", response_model=List[DocumentResponse])
def get_all_documents(db: db_dependency):
    return db.query(Document).all()


# ─── GET /documents/orphelins ─────────────────────────────────────────────────
# Liste les documents présents dans le dossier "uploads" (table `documents`)
# qui ne sont liés à aucun texte (aucune ligne dans `textes_documents`).
# Placée avant GET /documents/{id} par convention, mais l'ordre n'a pas
# d'incidence ici : {id} est typé int, "orphelins" ne peut donc pas matcher
# ce paramètre.
@router.get("/documents/orphelins", response_model=List[DocumentResponse])
def get_documents_orphelins(db: db_dependency):
    document_ids_utilises = db.query(Texte_Document.document_id).distinct()
    return (
        db.query(Document)
        .filter(~Document.id.in_(document_ids_utilises))
        .order_by(Document.date_upload.desc())
        .all()
    )


# ─── GET /documents/{id} ──────────────────────────────────────────────────────
@router.get("/documents/{id}", response_model=DocumentResponse)
def get_document_by_id(id: int, db: db_dependency):
    result = db.query(Document).filter(Document.id == id).first()
    if not result:
        raise HTTPException(status_code=404, detail="Document introuvable")
    return result


# ─── POST /documents/upload ───────────────────────────────────────────────────
# Upload autonome (hors création de texte) : un ou plusieurs fichiers,
# chacun devient une ligne `documents`. Utile par ex. pour un futur écran de
# gestion de documents indépendant d'un texte.
@router.post("/documents/upload", response_model=List[DocumentResponse], status_code=201)
def upload_documents(db: db_dependency, files: List[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="Aucun fichier fourni")

    documents = []
    for file in files:
        metadata = persist_uploaded_file(file)
        document = Document(**metadata)
        db.add(document)
        documents.append(document)

    db.commit()
    for document in documents:
        db.refresh(document)

    return documents


# ─── DELETE /documents/{id} ───────────────────────────────────────────────────
@router.delete("/documents/{id}", status_code=200)
def delete_document(id: int, db: db_dependency):
    document = db.query(Document).filter(Document.id == id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document introuvable")

    db.query(Texte_Document).filter(Texte_Document.document_id == id).delete(
        synchronize_session=False
    )
    db.query(QualiteDocument).filter(QualiteDocument.document_id == id).delete(
        synchronize_session=False
    )
    delete_file_from_disk(document.chemin_fichier)
    cleanup_rag_data(document.nom)

    db.delete(document)
    db.commit()
    return {"deleted": 1}


# ─── DELETE /documents (suppression multiple) ─────────────────────────────────
@router.delete("/documents", status_code=200)
def delete_documents(payload: DeleteDocumentsPayload, db: db_dependency):
    if not payload.ids:
        raise HTTPException(status_code=400, detail="Aucun identifiant fourni")

    documents = db.query(Document).filter(Document.id.in_(payload.ids)).all()
    for document in documents:
        delete_file_from_disk(document.chemin_fichier)
        cleanup_rag_data(document.nom)

    db.query(Texte_Document).filter(Texte_Document.document_id.in_(payload.ids)).delete(
        synchronize_session=False
    )
    db.query(QualiteDocument).filter(QualiteDocument.document_id.in_(payload.ids)).delete(
        synchronize_session=False
    )
    deleted = (
        db.query(Document)
        .filter(Document.id.in_(payload.ids))
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"deleted": deleted}


# ─── DELETE /documents/orphelins/{id} ─────────────────────────────────────────
# Supprime un document UNIQUEMENT s'il n'est plus lié à aucun texte
# (table Texte_Document). Utilisée par le frontend juste après la
# suppression d'un ou plusieurs textes, pour nettoyer les documents devenus
# orphelins (auparavant fait automatiquement côté delete_textes).
@router.delete("/documents/orphelins/{id}", status_code=200)
def delete_document_orphelin(id: int, db: db_dependency):
    document = db.query(Document).filter(Document.id == id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document introuvable")

    encore_utilise = (
        db.query(Texte_Document)
        .filter(Texte_Document.document_id == id)
        .first()
        is not None
    )
    if encore_utilise:
        raise HTTPException(
            status_code=409,
            detail="Ce document est encore lié à un texte, suppression refusée.",
        )

    delete_file_from_disk(document.chemin_fichier)
    cleanup_rag_data(document.nom)
    db.query(QualiteDocument).filter(QualiteDocument.document_id == id).delete(
        synchronize_session=False
    )
    db.delete(document)
    db.commit()
    return {"deleted": 1}


def delete_file_from_disk(relative_path: str | None) -> None:
    """Best-effort : si le fichier physique n'existe plus, on ignore plutôt
    que de faire échouer la suppression de la ligne en base."""
    if not relative_path:
        return
    file_path = BASE_DIR / relative_path
    try:
        file_path.unlink(missing_ok=True)
    except OSError:
        pass