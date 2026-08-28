from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class DocumentOut(BaseModel):
    id: int
    nom: Optional[str] = None
    chemin_fichier: Optional[str] = None
    nouveau_chemin: Optional[str] = None
    mime_type: Optional[str] = None
    taille_octets: Optional[int] = None
    date_upload: Optional[datetime] = None

    class Config:
        from_attributes = True  # (orm_mode sur pydantic v1)


class TexteDocumentOut(BaseModel):
    texte_id: int
    document_id: int

    class Config:
        from_attributes = True