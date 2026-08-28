from datetime import date
from typing import Optional, List

from pydantic import BaseModel, ConfigDict

class TexteCreate(BaseModel):
    # wp_id: Optional[int] = 0
    titre: str
    numero: Optional[str] = None
    date_mise_en_vigueur: Optional[date] = None
    signataire_nom: Optional[str] = None
    signataire_titre: Optional[str] = None
    resume: Optional[str] = None
    mots_cles: Optional[str] = None
    contenu_html: Optional[str] = None
    categorie_id: int
    statut_id: int
    # note_presentation_id: Optional[int] = None
    publish: Optional[int] = 0
    theme_ids: Optional[List[int]] = []  # pour lier les thèmes en même temps

class TexteUpdate(BaseModel):
    # Tous les champs sont optionnels : seuls ceux fournis seront modifiés
    # (mise à jour partielle, cf. `exclude_unset` côté route).
    titre: Optional[str] = None
    numero: Optional[str] = None
    date_mise_en_vigueur: Optional[date] = None
    signataire_nom: Optional[str] = None
    signataire_titre: Optional[str] = None
    resume: Optional[str] = None
    mots_cles: Optional[str] = None
    contenu_html: Optional[str] = None
    categorie_id: Optional[int] = None
    statut_id: Optional[int] = None
    publish: Optional[int] = None
    theme_ids: Optional[List[int]] = None  # None = ne pas toucher aux thèmes


class DeleteTextesPayload(BaseModel):
    ids: List[int]


class TexteResponse(BaseModel):
    id: int
    # wp_id: Optional[int] = None
    titre: str
    numero: Optional[str] = None
    date_mise_en_vigueur: Optional[date] = None
    signataire_nom: Optional[str] = None
    signataire_titre: Optional[str] = None
    resume: Optional[str] = None
    mots_cles: Optional[str] = None
    contenu_html: Optional[str] = None
    categorie: Optional[str] = None
    statut: Optional[str] = None
    categorie_id: Optional[int] = None
    statut_id: Optional[int] = None
    # note_presentation_id: Optional[int] = None
    publish: int
    themes: List[str] = []
    model_config = ConfigDict(from_attributes=True)