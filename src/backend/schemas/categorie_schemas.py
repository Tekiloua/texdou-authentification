from __future__ import annotations
from pydantic import BaseModel

class DeleteCategoriesRequest(BaseModel):
    ids: list[int]

class CategorieCreate(BaseModel):
    nom: str
    description: str | None = None
    slug: str | None = None
    couleur: str | None = None
    parent_id: int | None = None

class CategorieResponse(BaseModel):
    id: int
    nom: str | None = None
    description: str | None = None
    slug: str | None = None
    couleur: str | None = None
    sous_categories: list[CategorieResponse] = []

    model_config = {"from_attributes": True}