from http.client import HTTPException

from src.backend.schemas.statut_schemas import DeleteStatutsRequest, StatutCreate, StatutResponse
from fastapi import APIRouter, Depends
from src.backend.models import Statut
from src.backend.db.database import db_dependency, get_db, Session

router = APIRouter()

@router.get("/statuts", response_model=list[StatutResponse])
def get_all_statuts(db: db_dependency):
    statuts = db.query(Statut).all()

    nodes: dict[int, StatutResponse] = {
        statut.id: StatutResponse(
            id=statut.id,
            nom=statut.nom,
            description=statut.description,
            slug=statut.slug,
            couleur=statut.couleur,
            sous_statuts=[],
        )
        for statut in statuts
    }

    roots: list[StatutResponse] = []

    for statut in statuts:
        if statut.parent_id is None:
            roots.append(nodes[statut.id])
        else:
            parent = nodes.get(statut.parent_id)
            if parent:
                parent.sous_statuts.append(nodes[statut.id])

    return roots

@router.post("/add-statut")
def add_statut(statut: StatutCreate, db: Session = Depends(get_db)):
    nouvelle_statut = Statut(
        nom=statut.nom,
        description=statut.description,
        slug=statut.slug,
        couleur=statut.couleur,
        parent_id=statut.parent_id,
    )
    db.add(nouvelle_statut)
    db.commit()
    db.refresh(nouvelle_statut)
    return nouvelle_statut

@router.delete("/delete-statuts")
def delete_statuts(payload: DeleteStatutsRequest, db: db_dependency):
    statuts = db.query(Statut).filter(Statut.id.in_(payload.ids)).all()

    if not statuts:
        raise HTTPException(status_code=404, detail="Aucun statut trouvé.")

    noms = [s.nom for s in statuts]

    for statut in statuts:
        db.delete(statut)

    db.commit()

    return {
        "message": f"{len(noms)} statut(s) supprimé(s) : {', '.join(noms)}."
    }