from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.backend.db.database import get_db
from src.backend.models.texte_document import Texte_Document
from src.backend.models.document import Document
from src.backend.schemas.texte_document_schema import DocumentOut, TexteDocumentOut
from src.backend.routes.document_route import UPLOAD_DIR
from src.backend.routes.rag_route import ingest_document, is_document_indexed

router = APIRouter(tags=["Textes Documents"])


@router.get("/textes-documents", response_model=List[TexteDocumentOut])
def get_textes_documents(db: Session = Depends(get_db)):
    """Retourne toutes les associations texte_id / document_id."""
    return db.query(Texte_Document).all()


@router.get("/textes/{texte_id}/documents", response_model=List[DocumentOut])
def get_documents_by_texte(texte_id: int, db: Session = Depends(get_db)):
    """Retourne tous les documents liés à un texte donné."""
    documents = (
        db.query(Document)
        .join(Texte_Document, Texte_Document.document_id == Document.id)
        .filter(Texte_Document.texte_id == texte_id)
        .all()
    )

    if documents is None:
        raise HTTPException(status_code=404, detail="Aucun document trouvé pour ce texte")

    return documents


# ─── Statut RAG des documents d'un texte ────────────────────────────────────
# Un document est considéré "inclus dans le RAG" si un fichier Markdown
# correspondant existe déjà dans rag/markdowns/ (produit par l'indexation,
# que ce soit via /rag/upload ou via cette route). On ne stocke pas ce
# statut en BDD : le dossier markdowns/ fait foi.

class RagStatusOut(BaseModel):
    document_id: int
    inclus: bool


# ─── Statut RAG — bulk (tous les documents en un seul appel) ───────────────
# À privilégier côté frontend pour une liste de textes (ex: TextesTable) :
# évite le pattern N+1 où chaque ligne interroge /rag-status séparément.
@router.get("/documents/rag-status", response_model=List[RagStatusOut])
def get_rag_status_all(db: Session = Depends(get_db)):
    documents = db.query(Document).all()
    return [
        RagStatusOut(document_id=doc.id, inclus=is_document_indexed(doc.nom))
        for doc in documents
    ]


@router.get("/textes/{texte_id}/documents/rag-status", response_model=List[RagStatusOut])
def get_rag_status_by_texte(texte_id: int, db: Session = Depends(get_db)):
    documents = (
        db.query(Document)
        .join(Texte_Document, Texte_Document.document_id == Document.id)
        .filter(Texte_Document.texte_id == texte_id)
        .all()
    )
    return [
        RagStatusOut(document_id=doc.id, inclus=is_document_indexed(doc.nom))
        for doc in documents
    ]


# ─── Inclure un document dans le RAG ────────────────────────────────────────
# Indexe un document déjà uploadé (lié à un texte via /add-texte, stocké dans
# UPLOAD_DIR) en réutilisant le pipeline complet de rag_route.py (extraction
# VLM -> Markdown, chunking sémantique, embedding, stockage Chroma), sans
# dupliquer le fichier dans rag/uploads/.

@router.post("/documents/{document_id}/rag-include")
async def include_document_in_rag(document_id: int, db: Session = Depends(get_db)):
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document introuvable")

    file_path = UPLOAD_DIR / Path(document.nouveau_chemin or document.chemin_fichier).name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Fichier introuvable sur le disque")

    try:
        result = await ingest_document(file_path)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return result