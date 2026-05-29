"""PgVector-backed memory store for agent context retrieval."""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from app.memory.embeddings import embed

logger = logging.getLogger(__name__)


def _vec_str(embedding: list[float]) -> str:
    return "[" + ",".join(str(v) for v in embedding) + "]"


class PgVectorStore:
    """Long-term memory store using PostgreSQL + pgvector.

    Stores text content with embeddings for semantic similarity search.
    Each entry is scoped to a project_id and carries arbitrary metadata.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def store(
        self,
        project_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Embed content and store it in the agent_memory table.

        Args:
            project_id: Project scope for the memory entry.
            content: Text content to store and index.
            metadata: Optional JSON metadata (e.g. insight type, run_id).

        Returns:
            The auto-generated row ID.
        """
        embedding = await embed(content)
        meta_json = json.dumps(metadata or {})
        embedding_str = _vec_str(embedding)

        async with self._pool.acquire() as conn:
            row_id = await conn.fetchval(
                """
                INSERT INTO agent_memory (project_id, content, metadata, embedding)
                VALUES ($1, $2, $3::jsonb, $4::vector)
                RETURNING id
                """,
                project_id,
                content,
                meta_json,
                embedding_str,
            )

        logger.debug("Stored memory entry %d for project %s", row_id, project_id)
        return row_id

    async def search(
        self,
        project_id: str,
        query: str,
        top_k: int = 5,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic similarity search over stored memories.

        Args:
            project_id: Scope search to this project.
            query: Natural language query to search for.
            top_k: Maximum number of results to return.
            metadata_filter: Optional key-value pairs that must exist in metadata.

        Returns:
            List of dicts with keys: id, content, metadata, similarity, created_at.
        """
        query_embedding = await embed(query)
        embedding_str = _vec_str(query_embedding)

        # Build optional metadata filter clause
        filter_clause = ""
        params: list[Any] = [project_id, embedding_str, top_k]

        if metadata_filter:
            filter_parts = []
            for key, value in metadata_filter.items():
                param_idx = len(params) + 1
                filter_parts.append(f"metadata->>'{key}' = ${param_idx}")
                params.append(str(value))
            filter_clause = "AND " + " AND ".join(filter_parts)

        sql = f"""
            SELECT
                id,
                content,
                metadata,
                1 - (embedding <=> $2::vector) AS similarity,
                created_at
            FROM agent_memory
            WHERE project_id = $1
              {filter_clause}
            ORDER BY embedding <=> $2::vector
            LIMIT $3
        """

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        results = []
        for row in rows:
            meta = row["metadata"]
            if isinstance(meta, str):
                meta = json.loads(meta)
            results.append(
                {
                    "id": row["id"],
                    "content": row["content"],
                    "metadata": meta,
                    "similarity": float(row["similarity"]),
                    "created_at": row["created_at"].isoformat(),
                }
            )

        return results

    async def delete(self, entry_id: int) -> bool:
        """Delete a memory entry by ID.

        Returns:
            True if a row was deleted, False if the ID was not found.
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM agent_memory WHERE id = $1",
                entry_id,
            )
        return result == "DELETE 1"
