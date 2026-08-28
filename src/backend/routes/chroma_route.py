import sqlite3
import os
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional
import math

router = APIRouter(prefix="/chroma", tags=["ChromaDB"])

# Chemin absolu résolu depuis l'emplacement de CE fichier,
# indépendamment du répertoire de travail au lancement de uvicorn.
_HERE = os.path.dirname(os.path.abspath(__file__))
CHROMA_DB_PATH = os.getenv(
    "CHROMA_DB_PATH",
    os.path.join(_HERE, "../rag/chromadb/chroma.sqlite3"),
)
# Normalise les ".." pour un affichage lisible dans les logs
CHROMA_DB_PATH = os.path.normpath(CHROMA_DB_PATH)


def get_chroma_connection():
    if not os.path.exists(CHROMA_DB_PATH):
        raise HTTPException(
            status_code=503,
            detail=f"Base ChromaDB introuvable : {CHROMA_DB_PATH}",
        )
    conn = sqlite3.connect(CHROMA_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Modèles de réponse ────────────────────────────────────────────────────────

class ChunkItem(BaseModel):
    id: int
    embedding_id: str
    created_at: str
    document: Optional[str] = None
    chunk_index: Optional[int] = None
    source: Optional[str] = None
    markdown_path: Optional[str] = None
    pages: Optional[str] = None
    batch: Optional[str] = None
    # Statut d'inclusion dans la recherche RAG (metadata "inclus" côté
    # Chroma, 0 ou 1 — voir rag_route.py:set_inclus_for_source). Par défaut
    # à 1 pour les chunks anciens qui n'ont pas encore cette clé (voir
    # rag_route.py:_backfill_missing_inclus_field, qui les corrige de toute
    # façon au démarrage).
    inclus: int = 1


class PaginatedChunks(BaseModel):
    items: list[ChunkItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class CollectionInfo(BaseModel):
    id: str
    name: str
    dimension: Optional[int]
    total_embeddings: int


class StatsResponse(BaseModel):
    total_chunks: int
    total_sources: int
    collections: list[CollectionInfo]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_metadata_dict(rows) -> dict:
    """Transforme les lignes embedding_metadata en dict clé→valeur."""
    meta: dict = {}
    for row in rows:
        key = row["key"]
        value = (
            row["string_value"]
            or (str(row["int_value"]) if row["int_value"] is not None else None)
            or (str(row["float_value"]) if row["float_value"] is not None else None)
        )
        meta[key] = value
    return meta


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/stats", response_model=StatsResponse, summary="Statistiques globales")
def get_stats():
    conn = get_chroma_connection()
    try:
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM embeddings")
        total_chunks = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(DISTINCT string_value) FROM embedding_metadata WHERE key = 'source'"
        )
        total_sources = cur.fetchone()[0]

        cur.execute("SELECT id, name, dimension FROM collections")
        collections_raw = cur.fetchall()

        collections = []
        for col in collections_raw:
            cur.execute("SELECT COUNT(*) FROM embeddings")
            count = cur.fetchone()[0]
            collections.append(
                CollectionInfo(
                    id=col["id"],
                    name=col["name"],
                    dimension=col["dimension"],
                    total_embeddings=count,
                )
            )

        return StatsResponse(
            total_chunks=total_chunks,
            total_sources=total_sources,
            collections=collections,
        )
    finally:
        conn.close()


@router.get("/chunks", response_model=PaginatedChunks, summary="Liste paginée des chunks")
def get_chunks(
    page: int = Query(1, ge=1, description="Numéro de page"),
    page_size: int = Query(20, ge=1, le=100, description="Taille de la page"),
    source: Optional[str] = Query(None, description="Filtrer par nom de fichier source"),
    search: Optional[str] = Query(None, description="Recherche plein texte dans le contenu"),
):
    conn = get_chroma_connection()
    try:
        cur = conn.cursor()
        offset = (page - 1) * page_size

        base_query = "SELECT DISTINCT e.id, e.embedding_id, e.created_at FROM embeddings e"
        count_query = "SELECT COUNT(DISTINCT e.id) FROM embeddings e"
        joins = ""
        conditions = []
        params: list = []

        if source:
            joins += " JOIN embedding_metadata em_src ON e.id = em_src.id AND em_src.key = 'source'"
            conditions.append("em_src.string_value LIKE ?")
            params.append(f"%{source}%")

        if search:
            joins += " JOIN embedding_fulltext_search_content efts ON e.id = efts.id"
            conditions.append("efts.c0 LIKE ?")
            params.append(f"%{search}%")

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        cur.execute(f"{count_query}{joins} {where_clause}", params)
        total = cur.fetchone()[0]

        cur.execute(
            f"{base_query}{joins} {where_clause} ORDER BY e.id LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        rows = cur.fetchall()

        items: list[ChunkItem] = []
        for row in rows:
            cur.execute(
                "SELECT key, string_value, int_value, float_value FROM embedding_metadata WHERE id = ?",
                (row["id"],),
            )
            meta = _build_metadata_dict(cur.fetchall())
            items.append(
                ChunkItem(
                    id=row["id"],
                    embedding_id=row["embedding_id"],
                    created_at=row["created_at"],
                    document=meta.get("chroma:document"),
                    chunk_index=int(meta["chunk_index"]) if meta.get("chunk_index") else None,
                    source=meta.get("source"),
                    markdown_path=meta.get("markdown_path"),
                    pages=meta.get("pages"),
                    batch=meta.get("batch"),
                    inclus=int(meta["inclus"]) if meta.get("inclus") is not None else 1,
                )
            )

        return PaginatedChunks(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=math.ceil(total / page_size) if total > 0 else 1,
        )
    finally:
        conn.close()


@router.get("/chunks/{chunk_id}", response_model=ChunkItem, summary="Détail d'un chunk")
def get_chunk(chunk_id: int):
    conn = get_chroma_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, embedding_id, created_at FROM embeddings WHERE id = ?", (chunk_id,)
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Chunk introuvable")

        cur.execute(
            "SELECT key, string_value, int_value, float_value FROM embedding_metadata WHERE id = ?",
            (chunk_id,),
        )
        meta = _build_metadata_dict(cur.fetchall())

        return ChunkItem(
            id=row["id"],
            embedding_id=row["embedding_id"],
            created_at=row["created_at"],
            document=meta.get("chroma:document"),
            chunk_index=int(meta["chunk_index"]) if meta.get("chunk_index") else None,
            source=meta.get("source"),
            markdown_path=meta.get("markdown_path"),
            pages=meta.get("pages"),
            batch=meta.get("batch"),
            inclus=int(meta["inclus"]) if meta.get("inclus") is not None else 1,
        )
    finally:
        conn.close()


@router.get("/sources", summary="Liste des sources distinctes")
def get_sources():
    conn = get_chroma_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT string_value AS source, COUNT(*) AS chunk_count
            FROM embedding_metadata
            WHERE key = 'source'
            GROUP BY string_value
            ORDER BY string_value
            """
        )
        return [{"source": r["source"], "chunk_count": r["chunk_count"]} for r in cur.fetchall()]
    finally:
        conn.close()