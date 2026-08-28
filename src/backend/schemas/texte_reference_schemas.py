from pydantic import BaseModel
from datetime import date
from typing import Optional


class TexteReferenceBase(BaseModel):
    texte_id: int
    titre: Optional[str] = None
    numero: Optional[str] = None
    date_mise_en_vigueur: Optional[date] = None
    categorie: Optional[str] = None
    statut: Optional[str] = None
    lien_url: Optional[str] = None
    texte_lie_id: Optional[int] = None


class TexteReferenceCreate(TexteReferenceBase):
    pass


# Utilisé uniquement par POST /add-texte : mêmes champs qu'une référence,
# sans texte_id (le texte n'existe pas encore au moment où le client
# construit la requête — texte_id est renseigné côté serveur juste après
# la création du texte).
class TexteReferenceInput(BaseModel):
    titre: Optional[str] = None
    numero: Optional[str] = None
    date_mise_en_vigueur: Optional[date] = None
    categorie: Optional[str] = None
    statut: Optional[str] = None
    lien_url: Optional[str] = None
    texte_lie_id: Optional[int] = None


class TexteReferenceUpdate(BaseModel):
    titre: Optional[str] = None
    numero: Optional[str] = None
    date_mise_en_vigueur: Optional[date] = None
    categorie: Optional[str] = None
    statut: Optional[str] = None
    lien_url: Optional[str] = None
    texte_lie_id: Optional[int] = None


class TexteReferenceResponse(TexteReferenceBase):
    id: int

    class Config:
        from_attributes = True