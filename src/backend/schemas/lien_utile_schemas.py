from typing import Optional
from pydantic import BaseModel, ConfigDict


class LienUtileInput(BaseModel):
    """Payload d'un lien utile envoyé en même temps que la création/màj
    d'un texte (pas de texte_id : il est déduit côté serveur)."""
    titre: Optional[str] = None
    url: Optional[str] = None
    entite: Optional[str] = None


class LienUtileResponse(BaseModel):
    id: int
    texte_id: int
    titre: Optional[str] = None
    url: Optional[str] = None
    entite: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)