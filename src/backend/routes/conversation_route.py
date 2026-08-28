from fastapi import APIRouter, HTTPException
from src.backend.db.database import db_dependency
from src.backend.models.conversation import Conversation
from src.backend.models.message import Message
from src.backend.schemas.conversation_schemas import (
    ConversationCreate,
    ConversationUpdate,
    ConversationResponse,
    ConversationDetailResponse,
    DeleteConversationsPayload,
)
from src.backend.routes.user_route import get_current_user
from src.backend.models.user import User
from fastapi import Depends

router = APIRouter(prefix="/conversations", tags=["conversations"])


# ─── LISTE DES CONVERSATIONS DE L'UTILISATEUR CONNECTÉ ───────────────────────

@router.get("", response_model=list[ConversationResponse])
def get_my_conversations(
    db: db_dependency,
    current_user: User = Depends(get_current_user),
):
    return (
        db.query(Conversation)
        .filter(Conversation.user_id == current_user.id)
        .order_by(Conversation.updated_at.desc())
        .all()
    )


# ─── DÉTAIL D'UNE CONVERSATION (avec messages) ───────────────────────────────

@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
def get_conversation(
    conversation_id: int,
    db: db_dependency,
    current_user: User = Depends(get_current_user),
):
    conv = (
        db.query(Conversation)
        .filter(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id,
        )
        .first()
    )
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    return conv


# ─── CRÉER UNE CONVERSATION ───────────────────────────────────────────────────

@router.post("", response_model=ConversationResponse, status_code=201)
def create_conversation(
    payload: ConversationCreate,
    db: db_dependency,
    current_user: User = Depends(get_current_user),
):
    conv = Conversation(
        titre=payload.titre,
        user_id=current_user.id,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


# ─── RENOMMER UNE CONVERSATION ────────────────────────────────────────────────

@router.put("/{conversation_id}", response_model=ConversationResponse)
def update_conversation(
    conversation_id: int,
    payload: ConversationUpdate,
    db: db_dependency,
    current_user: User = Depends(get_current_user),
):
    conv = (
        db.query(Conversation)
        .filter(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id,
        )
        .first()
    )
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation introuvable")

    if payload.titre is not None:
        conv.titre = payload.titre

    db.commit()
    db.refresh(conv)
    return conv


# ─── SUPPRIMER UNE CONVERSATION ──────────────────────────────────────────────

@router.delete("/{conversation_id}", status_code=200)
def delete_conversation(
    conversation_id: int,
    db: db_dependency,
    current_user: User = Depends(get_current_user),
):
    conv = (
        db.query(Conversation)
        .filter(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id,
        )
        .first()
    )
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation introuvable")

    db.delete(conv)  # cascade supprime aussi les messages (cf. model)
    db.commit()
    return {"deleted": 1}


# ─── SUPPRESSION EN MASSE ─────────────────────────────────────────────────────

@router.delete("", status_code=200)
def delete_conversations(
    payload: DeleteConversationsPayload,
    db: db_dependency,
    current_user: User = Depends(get_current_user),
):
    if not payload.ids:
        raise HTTPException(status_code=400, detail="Aucun identifiant fourni")

    # Sécurité : on ne supprime que les conversations appartenant à l'utilisateur
    deleted = (
        db.query(Conversation)
        .filter(
            Conversation.id.in_(payload.ids),
            Conversation.user_id == current_user.id,
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"deleted": deleted}