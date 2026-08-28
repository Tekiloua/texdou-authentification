from __future__ import annotations
from pydantic import BaseModel
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.backend.schemas.message_schemas import MessageResponse


class ConversationCreate(BaseModel):
    titre: str | None = None


class ConversationUpdate(BaseModel):
    titre: str | None = None


class ConversationResponse(BaseModel):
    id: int
    titre: str | None = None
    user_id: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConversationDetailResponse(ConversationResponse):
    """Conversation avec ses messages inclus."""
    messages: list[MessageResponse] = []

    model_config = {"from_attributes": True}


class DeleteConversationsPayload(BaseModel):
    ids: list[int]


# Import réel (pas seulement TYPE_CHECKING) requis par Pydantic v2 pour résoudre
# l'annotation différée `list[MessageResponse]` ci-dessus (from __future__ import
# annotations transforme les annotations en chaînes de caractères évaluées plus
# tard). Sans ça, le modèle reste "not fully defined" et FastAPI plante à la
# sérialisation de la réponse.
from src.backend.schemas.message_schemas import MessageResponse  # noqa: E402

ConversationDetailResponse.model_rebuild()