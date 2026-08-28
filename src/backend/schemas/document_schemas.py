from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict


class DocumentBase(BaseModel):
    nom: Optional[str] = None
    chemin_fichier: Optional[str] = None
    nouveau_chemin: Optional[str] = None
    mime_type: Optional[str] = None
    taille_octets: Optional[int] = None
    # La colonne `date_upload` en base est un DateTime (pas une Date pure) :
    # certaines lignes historiques ont une heure non nulle. Utiliser `date`
    # ici fait échouer la validation Pydantic ("Datetimes provided to dates
    # should have zero time") dès qu'une ligne a une heure ≠ 00:00:00.
    date_upload: Optional[datetime] = None


class DocumentResponse(DocumentBase):
    id: int

    model_config = ConfigDict(from_attributes=True)


class DeleteDocumentsPayload(BaseModel):
    ids: List[int]