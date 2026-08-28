from fastapi import APIRouter, HTTPException
from src.backend.db.database import db_dependency
from src.backend.models import Texte_Reference
from src.backend.schemas.texte_reference_schemas import (
    TexteReferenceCreate,
    TexteReferenceResponse,
    TexteReferenceUpdate,
)

router = APIRouter()


# ── GET /textes/{texte_id}/references ────────────────────────────────────────
# Retourne toutes les références liées à un texte donné.
@router.get(
    "/textes/{texte_id}/references",
    response_model=list[TexteReferenceResponse],
)
def get_references_by_texte(texte_id: int, db: db_dependency):
    references = (
        db.query(Texte_Reference)
        .filter(Texte_Reference.texte_id == texte_id)
        .order_by(Texte_Reference.id.asc())
        .all()
    )
    return references


# ── GET /textes-references ────────────────────────────────────────────────────
# Retourne toutes les références (toutes textes confondus).
@router.get("/textes-references", response_model=list[TexteReferenceResponse])
def get_all_textes_references(db: db_dependency):
    return (
        db.query(Texte_Reference)
        .order_by(Texte_Reference.id.asc())
        .all()
    )


# ── GET /textes-references/{id} ───────────────────────────────────────────────
@router.get("/textes-references/{id}", response_model=TexteReferenceResponse)
def get_texte_reference_by_id(id: int, db: db_dependency):
    reference = (
        db.query(Texte_Reference)
        .filter(Texte_Reference.id == id)
        .first()
    )
    if not reference:
        raise HTTPException(status_code=404, detail="Référence introuvable")
    return reference


# ── POST /textes-references ───────────────────────────────────────────────────
@router.post(
    "/textes-references",
    response_model=TexteReferenceResponse,
    status_code=201,
)
def add_texte_reference(payload: TexteReferenceCreate, db: db_dependency):
    nouvelle_reference = Texte_Reference(**payload.dict())
    db.add(nouvelle_reference)
    db.commit()
    db.refresh(nouvelle_reference)
    return nouvelle_reference


# ── PUT /textes-references/{id} ───────────────────────────────────────────────
@router.put("/textes-references/{id}", response_model=TexteReferenceResponse)
def update_texte_reference(
    id: int, payload: TexteReferenceUpdate, db: db_dependency
):
    reference = (
        db.query(Texte_Reference).filter(Texte_Reference.id == id).first()
    )
    if not reference:
        raise HTTPException(status_code=404, detail="Référence introuvable")

    for field, value in payload.dict(exclude_unset=True).items():
        setattr(reference, field, value)

    db.commit()
    db.refresh(reference)
    return reference


# ── DELETE /textes-references/{id} ────────────────────────────────────────────
@router.delete("/textes-references/{id}", status_code=200)
def delete_texte_reference(id: int, db: db_dependency):
    reference = (
        db.query(Texte_Reference).filter(Texte_Reference.id == id).first()
    )
    if not reference:
        raise HTTPException(status_code=404, detail="Référence introuvable")

    db.delete(reference)
    db.commit()
    return {"deleted": 1}