from fastapi import APIRouter

from src.backend.db.database import db_dependency
from src.backend.models import Historique
from src.backend.schemas.historique_schemas import HistoriqueResponse

router = APIRouter()


@router.get("/historiques", response_model=list[HistoriqueResponse])
def get_all_historiques(db: db_dependency):
    """Retourne le journal des changements de statut, du plus récent
    au plus ancien."""
    return (
        db.query(Historique)
        .order_by(Historique.date.desc(), Historique.id.desc())
        .all()
    )