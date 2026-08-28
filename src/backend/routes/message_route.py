from fastapi import APIRouter, HTTPException, Depends
from src.backend.db.database import db_dependency
from src.backend.models.conversation import Conversation
from src.backend.models.message import Message, RoleEnum
from src.backend.models.consommation import Consommation
from src.backend.schemas.message_schemas import (
    MessageCreate,
    MessageResponse,
    ChatResponse,
)
from src.backend.routes.user_route import get_current_user
from src.backend.models.user import User
from src.backend.rag.rag_openrouter import call_openrouter
from src.backend.rag.rag_retriever import build_context_block

router = APIRouter(prefix="/conversations/{conversation_id}/messages", tags=["messages"])


def _get_conversation_or_404(conversation_id: int, user_id: int, db) -> Conversation:
    """Vérifie que la conversation existe et appartient à l'utilisateur."""
    conv = (
        db.query(Conversation)
        .filter(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
        )
        .first()
    )
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    return conv


# ─── LISTER LES MESSAGES D'UNE CONVERSATION ──────────────────────────────────

@router.get("", response_model=list[MessageResponse])
def get_messages(
    conversation_id: int,
    db: db_dependency,
    current_user: User = Depends(get_current_user),
):
    _get_conversation_or_404(conversation_id, current_user.id, db)

    return (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        # Tri par id (ordre d'insertion réel) plutôt que created_at seul :
        # deux messages créés dans la même seconde/milliseconde (user +
        # assistant, envoyés coup sur coup) peuvent avoir un created_at
        # identique, ce qui rend ORDER BY created_at instable et peut
        # afficher la réponse avant la question selon les requêtes.
        .order_by(Message.id.asc())
        .all()
    )


# ─── ENVOYER UN MESSAGE ET OBTENIR LA RÉPONSE DU LLM ────────────────────────

@router.post("", response_model=ChatResponse, status_code=201)
async def send_message(
    conversation_id: int,
    payload: MessageCreate,
    db: db_dependency,
    current_user: User = Depends(get_current_user),
):
    """
    1. Vérifie la conversation.
    2. Sauvegarde le message utilisateur en BDD.
    3. Reconstruit l'historique complet et appelle OpenRouter.
    4. Sauvegarde la réponse assistant en BDD.
    5. Enregistre la consommation de tokens (input + output) liée à l'utilisateur.
    6. Retourne les deux messages.
    """
    _get_conversation_or_404(conversation_id, current_user.id, db)

    # 1 — Sauvegarder le message utilisateur
    user_msg = Message(
        conversation_id=conversation_id,
        role=RoleEnum.user,
        contenu=payload.contenu,
    )
    db.add(user_msg)
    db.commit()
    db.refresh(user_msg)

    # 2 — Reconstruire l'historique complet pour le contexte LLM
    # (même remarque que pour get_messages : tri par id, pas par created_at)
    historique = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.id.asc())
        .all()
    )
    history_payload = [
        {"role": msg.role.value, "content": msg.contenu}
        for msg in historique
    ]

    # 3 — Récupérer le contexte pertinent depuis ChromaDB, sur l'ensemble de
    # la base documentaire indexée (plus de filtre par "base de
    # connaissance" sélectionnée côté frontend). Si rien n'est trouvé (base
    # vide, ou aucun chunk pertinent), context="" et call_openrouter
    # répondra explicitement qu'il n'a pas l'information, sans jamais
    # improviser depuis ses connaissances générales.
    try:
        context = await build_context_block(payload.contenu)
    except RuntimeError as e:
        print(f"[RAG retrieval ERROR] {e}")
        context = ""

    # 4 — Appel OpenRouter (async), contraint au contexte récupéré
    try:
        resultat_llm = await call_openrouter(history_payload, context=context)
    except RuntimeError as e:
        # On rollback le message user pour ne pas polluer la BDD
        # (optionnel selon la stratégie souhaitée)
        print(f"[OpenRouter ERROR] {e}")  # visible dans les logs uvicorn
        raise HTTPException(status_code=502, detail=str(e))

    reponse_texte = resultat_llm["content"]
    input_tokens = resultat_llm["input_tokens"]
    output_tokens = resultat_llm["output_tokens"]

    # 4 — Sauvegarder la réponse de l'assistant
    assistant_msg = Message(
        conversation_id=conversation_id,
        role=RoleEnum.assistant,
        contenu=reponse_texte,
    )
    db.add(assistant_msg)

    # 5 — Enregistrer la consommation de tokens (historique indépendant du user,
    #     identifié par son numero, pas par son id — survit à la suppression du compte)
    consommation = Consommation(
        input=input_tokens,
        output=output_tokens,
        numero=current_user.numero,
    )
    db.add(consommation)

    db.commit()
    db.refresh(assistant_msg)

    return ChatResponse(
        user_message=user_msg,
        assistant_message=assistant_msg,
    )


# ─── SUPPRIMER UN MESSAGE ─────────────────────────────────────────────────────

@router.delete("/{message_id}", status_code=200)
def delete_message(
    conversation_id: int,
    message_id: int,
    db: db_dependency,
    current_user: User = Depends(get_current_user),
):
    _get_conversation_or_404(conversation_id, current_user.id, db)

    msg = (
        db.query(Message)
        .filter(
            Message.id == message_id,
            Message.conversation_id == conversation_id,
        )
        .first()
    )
    if not msg:
        raise HTTPException(status_code=404, detail="Message introuvable")

    db.delete(msg)
    db.commit()
    return {"deleted": 1}