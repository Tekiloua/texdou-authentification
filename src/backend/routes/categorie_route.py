from src.backend.schemas.categorie_schemas import CategorieCreate, CategorieResponse, DeleteCategoriesRequest
from fastapi import APIRouter, Depends, HTTPException
from src.backend.models import Categorie
from src.backend.db.database import db_dependency, get_db, Session

router = APIRouter()

@router.get("/categories", response_model=list[CategorieResponse])
def get_all_categories(db: db_dependency):
    categories = db.query(Categorie).all()

    nodes: dict[int, CategorieResponse] = {
        categorie.id: CategorieResponse(
            id=categorie.id,
            nom=categorie.nom,
            description=categorie.description,
            slug=categorie.slug,
            couleur=categorie.couleur,
            sous_categories=[],
        )
        for categorie in categories
    }

    roots: list[CategorieResponse] = []

    for categorie in categories:
        if categorie.parent_id is None:
            roots.append(nodes[categorie.id])
        else:
            parent = nodes.get(categorie.parent_id)
            if parent:
                parent.sous_categories.append(nodes[categorie.id])

    return roots

@router.post("/add-categorie")
def add_categorie(categorie: CategorieCreate, db: Session = Depends(get_db)):
    existante = db.query(Categorie).filter(Categorie.nom == categorie.nom).first()
    if existante:
        raise HTTPException(
            status_code=409,
            detail=f"Une catégorie avec le nom « {categorie.nom} » existe déjà."
        )

    nouvelle_categorie = Categorie(
        nom=categorie.nom,
        description=categorie.description,
        slug=categorie.slug,
        couleur=categorie.couleur,
        parent_id=categorie.parent_id,
    )
    db.add(nouvelle_categorie)
    db.commit()
    db.refresh(nouvelle_categorie)
    return nouvelle_categorie


@router.delete("/delete-categories")
def delete_categories(payload: DeleteCategoriesRequest, db: db_dependency):
    categories = db.query(Categorie).filter(Categorie.id.in_(payload.ids)).all()

    if not categories:
        raise HTTPException(status_code=404, detail="Aucune catégorie trouvée.")

    noms = [c.nom for c in categories]

    for categorie in categories:
        db.delete(categorie)

    db.commit()

    return {
        "message": f"{len(noms)} catégorie(s) supprimée(s) : {', '.join(noms)}."
    }