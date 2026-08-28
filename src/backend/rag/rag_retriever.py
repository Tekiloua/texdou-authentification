"""
rag_retriever.py
─────────────────
Interroge la collection Chroma unique (alimentée par rag_route.py) pour
une question donnée, et construit le bloc de contexte à injecter dans le
prompt système de call_openrouter.

Recherche sur l'ensemble de la collection, sans filtre par "base de
connaissance" (fonctionnalité retirée).
"""

import os
from pathlib import Path

import chromadb
import httpx
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"
EMBEDDING_MODEL = os.getenv("OPENROUTER_EMBEDDING_MODEL", "perplexity/pplx-embed-v1-4b")

# Même dossier que rag_route.py : src/backend/rag/
RAG_DIR = Path(os.getenv("RAG_DIR", Path(__file__).resolve().parent.parent / "rag"))
CHROMA_DIR = Path(os.getenv("CHROMA_DIR", RAG_DIR / "chromadb"))

_chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
_collection = _chroma_client.get_or_create_collection(name="documents")

_HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
}


async def _embed_query(query: str) -> list[float]:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY manquant dans les variables d'environnement.")

    payload = {"model": EMBEDDING_MODEL, "input": [query]}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(OPENROUTER_EMBEDDINGS_URL, json=payload, headers=_HEADERS)
    if resp.status_code != 200:
        raise RuntimeError(f"Erreur embedding ({EMBEDDING_MODEL}) {resp.status_code}: {resp.text}")
    return resp.json()["data"][0]["embedding"]


async def retrieve_context(query: str, n_results: int = 10) -> list[dict]:
    """
    Retourne les chunks les plus proches de la question, triés par
    pertinence (distance croissante), sur toute la collection Chroma.
    """
    if _collection.count() == 0:
        return []

    query_vector = await _embed_query(query)
    res = _collection.query(
        query_embeddings=[query_vector],
        n_results=n_results,
        # N'interroge que les chunks dont l'inclusion RAG est active. Les
        # chunks rattachés à un texte dont la case "RAG" a été décochée
        # (metadata "inclus" = 0, voir rag_route.py:set_inclus_for_source)
        # sont ainsi totalement ignorés, sans être supprimés de la base.
        where={"inclus": 1},
    )

    chunks = []
    for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
        chunks.append({"text": doc, "source": meta.get("source", "inconnu"), "distance": dist})
    return chunks


async def build_context_block(query: str, n_results: int = 10) -> str:
    """
    Construit le bloc de contexte texte à insérer dans le prompt système.
    Retourne une chaîne vide si la base de connaissance est vide ou si rien
    n'est trouvé — call_openrouter doit alors répondre qu'il n'a pas
    l'information, jamais improviser.
    """
    chunks = await retrieve_context(query, n_results=n_results)
    if not chunks:
        return ""

    parts = [f"[Source: {c['source']}]\n{c['text']}" for c in chunks]
    return "\n\n---\n\n".join(parts)