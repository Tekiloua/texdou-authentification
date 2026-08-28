from http.client import HTTPException

from src.backend.schemas.theme_schemas import DeleteThemesRequest, ThemeCreate, ThemeResponse
from fastapi import APIRouter
from src.backend.models import Theme
from src.backend.db.database import db_dependency
from src.backend.models import Theme

router = APIRouter()

@router.get("/themes", response_model=list[ThemeResponse])
def get_all_themes(db: db_dependency):
    themes = db.query(Theme).all()

    nodes: dict[int, ThemeResponse] = {
        theme.id: ThemeResponse(
            id=theme.id,
            nom=theme.nom,
            description=theme.description,
            slug=theme.slug,
            couleur=theme.couleur,
            sous_themes=[],
        )
        for theme in themes
    }

    roots: list[ThemeResponse] = []

    for theme in themes:
        if theme.parent_id is None:
            roots.append(nodes[theme.id])
        else:
            parent = nodes.get(theme.parent_id)
            if parent:
                parent.sous_themes.append(nodes[theme.id])

    return roots

@router.post("/add-theme")
def add_theme(theme: ThemeCreate, db: db_dependency):
    nouveau_theme = Theme(
        nom=theme.nom,
        description=theme.description,
        slug=theme.slug,
        couleur=theme.couleur,
        parent_id=theme.parent_id,
    )
    db.add(nouveau_theme)
    db.commit()
    db.refresh(nouveau_theme)
    return nouveau_theme

@router.delete("/delete-themes")
def delete_themes(payload: DeleteThemesRequest, db: db_dependency):
    themes = db.query(Theme).filter(Theme.id.in_(payload.ids)).all()

    if not themes:
        raise HTTPException(status_code=404, detail="Aucun thème trouvé.")

    noms = [t.nom for t in themes]

    for theme in themes:
        db.delete(theme)

    db.commit()

    return {
        "message": f"{len(noms)} thème(s) supprimé(s) : {', '.join(noms)}."
    }

@router.put("/update-theme/{theme_id}")
def update_theme(theme_id: int, theme: ThemeCreate, db: db_dependency):
    db_theme = db.query(Theme).filter(Theme.id == theme_id).first()

    if not db_theme:
        raise HTTPException(status_code=404, detail="Thème introuvable.")

    db_theme.nom = theme.nom
    db_theme.slug = theme.slug
    db_theme.description = theme.description
    db_theme.couleur = theme.couleur
    db_theme.parent_id = theme.parent_id

    db.commit()
    db.refresh(db_theme)
    return db_theme