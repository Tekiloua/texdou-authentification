from __future__ import annotations
from pydantic import BaseModel

class DeleteStatutsRequest(BaseModel):
    ids: list[int]

class StatutCreate(BaseModel):
    nom: str
    description: str | None = None
    slug: str | None = None
    couleur: str | None = None
    parent_id: int | None = None


class StatutResponse(BaseModel):
    id: int
    nom: str | None = None
    description: str | None = None
    slug: str | None = None
    couleur: str | None = None
    sous_statuts: list[StatutResponse] = []
    model_config = {"from_attributes": True}