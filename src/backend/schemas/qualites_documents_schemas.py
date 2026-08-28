from typing import List, Optional

from pydantic import BaseModel, ConfigDict


class QualiteDocumentBase(BaseModel):
    document_id: int
    page: int
    blur: Optional[float] = None
    skew: Optional[float] = None
    noise_score: Optional[float] = None
    black_pixel_ratio: Optional[float] = None
    entropy: Optional[float] = None
    brightness: Optional[float] = None
    score: Optional[float] = None


class QualiteDocumentCreate(QualiteDocumentBase):
    """Utilisé en interne pour insérer une ligne depuis analyze_document."""
    pass


class QualiteDocumentResponse(QualiteDocumentBase):
    id: int

    model_config = ConfigDict(from_attributes=True)


class QualiteDocumentListResponse(BaseModel):
    """Réponse groupée : toutes les pages d'un document."""
    document_id: int
    pages: List[QualiteDocumentResponse]