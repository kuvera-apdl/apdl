"""Local, in-process text embeddings via fastembed (ONNX).

No external service or API key: the embedding model runs inside the agents
container. See ``local-files/docs/plans/agent-memory-backend-options.md``
(Option 1) for the rationale.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# bge-small-en-v1.5 ships as ONNX via fastembed and emits 384-dim vectors.
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
EMBEDDING_DIMENSIONS = 384
_CACHE_DIR = os.getenv("FASTEMBED_CACHE_DIR", "/app/.fastembed_cache")

_model: Any = None


def _get_model() -> Any:
    """Lazily load the embedding model (downloaded once, cached on disk).

    The ``fastembed`` import is deferred so this module imports without the
    heavy optional dependency present — unit tests patch ``_get_model``.
    """
    global _model
    if _model is None:
        from fastembed import TextEmbedding

        logger.info("Loading local embedding model %s", EMBEDDING_MODEL)
        _model = TextEmbedding(model_name=EMBEDDING_MODEL, cache_dir=_CACHE_DIR)
    return _model


def _embed_sync(texts: list[str], batch_size: int = 256) -> list[list[float]]:
    """Run the synchronous model over a list of texts (one vector per input)."""
    model = _get_model()
    return [vector.tolist() for vector in model.embed(texts, batch_size=batch_size)]


async def embed(text: str) -> list[float]:
    """Embed a single string and return a 384-dimensional vector.

    The model runs on a worker thread so it never blocks the event loop.
    """
    vectors = await asyncio.to_thread(_embed_sync, [text])
    return vectors[0]


async def embed_batch(texts: list[str], batch_size: int = 64) -> list[list[float]]:
    """Embed multiple texts, returning one vector per input (order preserved)."""
    if not texts:
        return []
    return await asyncio.to_thread(_embed_sync, texts, batch_size)
