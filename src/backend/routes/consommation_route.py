from fastapi import APIRouter, Depends
from sqlalchemy import func
from src.backend.db.database import db_dependency
from src.backend.models.consommation import Consommation
from src.backend.schemas.consommation_schemas import (
    ConsommationResponse,
    ConsommationTotal,
)
from src.backend.routes.user_route import get_current_user
from src.backend.models.user import User

router = APIRouter(prefix="/consommations", tags=["consommations"])


# ─── LISTER SES PROPRES CONSOMMATIONS (historique détaillé) ────────────────

@router.get("", response_model=list[ConsommationResponse])
def get_my_consommations(
    db: db_dependency,
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(Consommation)
        .filter(Consommation.numero == current_user.numero)
        .order_by(Consommation.created_at.desc())
        .all()
    )


# ─── TOTAL AGRÉGÉ DE SA CONSOMMATION ─────────────────────────────────────────

@router.get("/total", response_model=ConsommationTotal)
def get_my_total_consommation(
    db: db_dependency,
    current_user: User = Depends(get_current_user),
):
    result = (
        db.query(
            func.coalesce(func.sum(Consommation.input), 0).label("total_input"),
            func.coalesce(func.sum(Consommation.output), 0).label("total_output"),
        )
        .filter(Consommation.numero == current_user.numero)
        .first()
    )

    total_input = result.total_input or 0
    total_output = result.total_output or 0

    return ConsommationTotal(
        numero=current_user.numero,
        total_input=total_input,
        total_output=total_output,
        total_tokens=total_input + total_output,
    )