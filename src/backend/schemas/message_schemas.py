from __future__ import annotations
from pydantic import BaseModel
from datetime import datetime
from src.backend.models.message import RoleEnum


class MessageCreate(BaseModel):
    """Payload envoyé par le frontend : la question de l'utilisateur, plus la
    sélection courante de la "base de connaissance" (KnowledgeSheet /
    useKnowledgeBaseStore), sous forme d'ids de Texte.

    - texte_ids is None : pas de restriction, comportement legacy (recherche
      dans toute la collection ChromaDB).
    - texte_ids == []   : base de connaissance vide côté utilisateur, aucun
      contexte ne doit être récupéré.
    - texte_ids == [1, 2, ...] : restreint la recherche RAG aux documents
      liés à ces textes (cf. message_route.py -> _resolve_sources).
    """
    contenu: str
    texte_ids: list[int] | None = None


class MessageResponse(BaseModel):
    id: int
    conversation_id: int
    role: RoleEnum
    contenu: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatResponse(BaseModel):
    """Retourné après un envoi de message : le message user + la réponse assistant."""
    user_message: MessageResponse
    assistant_message: MessageResponse