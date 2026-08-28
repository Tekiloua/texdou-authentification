from __future__ import annotations
from pydantic import BaseModel

class DeleteThemesRequest(BaseModel):
    ids: list[int]

class ThemeCreate(BaseModel):
    nom: str
    description: str | None = None
    slug: str | None = None
    couleur: str | None = None
    parent_id: int | None = None


class ThemeResponse(BaseModel):
    id: int
    nom: str | None = None
    description: str | None = None
    slug: str | None = None
    couleur: str | None = None
    sous_themes: list[ThemeResponse] = []
    model_config = {"from_attributes": True}