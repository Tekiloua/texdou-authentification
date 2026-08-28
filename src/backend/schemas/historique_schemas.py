from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, ConfigDict


class HistoriqueResponse(BaseModel):
    id: int
    texte_id: Optional[int] = None
    texte_titre: Optional[str] = None
    ancien_statut: Optional[str] = None
    nouveau_statut: Optional[str] = None
    numero_user: Optional[str] = None
    date: datetime

    model_config = ConfigDict(from_attributes=True)


class HistoriqueListResponse(BaseModel):
    items: List[HistoriqueResponse]