"""
rag_openrouter.py
─────────────────
Logique isolée pour appeler l'API OpenRouter.
On reconstruit l'historique complet de la conversation à chaque appel
(stateless côté LLM, la mémoire est dans la BDD).

Support proxy : si les variables d'environnement HTTPS_PROXY / HTTP_PROXY
sont définies, elles sont automatiquement utilisées par httpx.
"""

import os
import httpx
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
modele = "deepseek/deepseek-v4-flash"
# Modèle par défaut — changeable via variable d'env
DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", modele)

# Prompt système RAG strict : le modèle ne doit répondre qu'à partir du
# contexte fourni (extrait de la base de connaissance ChromaDB), jamais
# depuis ses connaissances générales.
SYSTEM_PROMPT_RAG = os.getenv(
    "OPENROUTER_SYSTEM_PROMPT_RAG",
    "Tu es un assistant virtuel qui répond UNIQUEMENT à partir du contexte fourni "
    "ci-dessous, extrait de la base de connaissance documentaire. "
    "Règles strictes :\n"
    "- N'utilise JAMAIS tes connaissances générales, même si tu penses connaître la réponse.\n"
    "- Si l'information demandée n'est pas dans le contexte, réponds explicitement que "
    "tu ne trouves pas cette information dans les documents disponibles. N'invente rien.\n"
    "- Cite la source (nom du document) quand c'est pertinent.\n"
    "- Réponds en Markdown, de façon claire et structurée.\n\n"
    "- N'invente rien du tous\n\n"
    "Contexte :\n{context}",
)

# Prompt utilisé quand la base de connaissance est vide ou qu'aucun chunk
# pertinent n'a été trouvé pour la question — le modèle doit le dire
# explicitement plutôt que de répondre depuis ses connaissances générales.
SYSTEM_PROMPT_NO_CONTEXT = os.getenv(
    "OPENROUTER_SYSTEM_PROMPT_NO_CONTEXT",
    "Tu es un assistant virtuel branché sur une base de connaissance documentaire. "
    "Aucun document pertinent n'a été trouvé pour cette question. "
    "Réponds à l'utilisateur que tu ne disposes d'aucune information sur ce sujet dans "
    "les documents disponibles, sans utiliser tes connaissances générales pour compléter.",
)

# ── Configuration proxy ──────────────────────────────────────────────
# Renseigne HTTPS_PROXY (prioritaire) ou HTTP_PROXY dans ton .env, ex :
# HTTPS_PROXY=http://user:password@proxy-host:port
PROXY_URL = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or None

# Si ton proxy d'entreprise fait de l'inspection SSL (MITM), renseigne le
# chemin vers le certificat CA à utiliser pour la vérification TLS.
# Sinon laisse SSL_VERIFY à True (comportement par défaut, sécurisé).
_ssl_verify_env = os.getenv("SSL_CERT_PATH")
SSL_VERIFY = _ssl_verify_env if _ssl_verify_env else True


def build_messages_payload(history: list[dict], context: str = "") -> list[dict]:
    """
    Construit la liste de messages à envoyer à OpenRouter en préfixant avec
    le message système adapté :
      - contexte trouvé  -> SYSTEM_PROMPT_RAG (répondre uniquement avec ce contexte)
      - contexte vide    -> SYSTEM_PROMPT_NO_CONTEXT (dire qu'on ne sait pas,
                             jamais improviser depuis les connaissances générales)

    `history` est une liste de dicts {"role": "user"|"assistant", "content": str}
    issus de la BDD (messages déjà sauvegardés + le nouveau message user).
    """
    system_content = (
        SYSTEM_PROMPT_RAG.format(context=context) if context else SYSTEM_PROMPT_NO_CONTEXT
    )
    return [{"role": "system", "content": system_content}, *history]


def _build_client_kwargs() -> dict:
    """
    Construit les kwargs pour httpx.AsyncClient, en ajoutant le proxy
    et la config SSL si nécessaire.
    """
    kwargs = {"timeout": 60.0, "verify": SSL_VERIFY}
    if PROXY_URL:
        kwargs["proxy"] = PROXY_URL
    return kwargs


async def call_openrouter(history: list[dict], model: str = DEFAULT_MODEL, context: str = "") -> dict:
    """
    Envoie l'historique à OpenRouter et retourne la réponse + les infos de consommation.

    Args:
        history: liste ordonnée de {"role": ..., "content": ...}
        model:   identifiant du modèle OpenRouter
        context: bloc de contexte récupéré depuis ChromaDB (rag_retriever.py).
                 Vide -> le modèle répond qu'il n'a pas l'information, jamais
                 depuis ses connaissances générales.

    Returns:
        dict {
            "content": str,            # texte de la réponse de l'assistant
            "input_tokens": int,       # tokens consommés en entrée (prompt)
            "output_tokens": int,      # tokens générés en sortie (completion)
        }

    Raises:
        RuntimeError si l'API répond avec une erreur ou si le proxy est
        inaccessible.
    """
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY manquant dans les variables d'environnement.")

    payload = {
        "model": model,
        "messages": build_messages_payload(history, context=context),
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        # Optionnel mais recommandé par OpenRouter pour les analytics
        "HTTP-Referer": os.getenv("APP_URL", "http://localhost:8000"),
        "X-Title": os.getenv("APP_NAME", "MonApp"),
    }

    try:
        async with httpx.AsyncClient(**_build_client_kwargs()) as client:
            response = await client.post(OPENROUTER_BASE_URL, json=payload, headers=headers)
    except httpx.ProxyError as e:
        raise RuntimeError(f"Erreur de connexion au proxy ({"PROXY_URL"}) : {e}") from e
    except httpx.ConnectError as e:
        raise RuntimeError(f"Impossible de se connecter à OpenRouter : {e}") from e

    if response.status_code != 200:
        raise RuntimeError(
            f"OpenRouter error {response.status_code}: {response.text}"
        )

    data = response.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Réponse OpenRouter inattendue : {data}") from e

    # OpenRouter renvoie un objet "usage" (compatible format OpenAI)
    usage = data.get("usage", {}) or {}
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)

    return {
        "content": content,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }