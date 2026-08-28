from __future__ import annotations
from pydantic import BaseModel
from datetime import datetime


class ConsommationCreate(BaseModel):
    """Payload interne utilisé pour créer un enregistrement de consommation."""
    input: int
    output: int
    numero: str


class ConsommationResponse(BaseModel):
    id: int
    input: int
    output: int
    numero: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ConsommationTotal(BaseModel):
    """Agrégat total de consommation pour un numero donné."""
    numero: str
    total_input: int
    total_output: int
    total_tokens: int