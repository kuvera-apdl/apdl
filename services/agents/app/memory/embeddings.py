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

#: Known fastembed models → vector width. The dimension MUST track the model:
#: the agent_memory column is declared/migrated from this constant, so a model
#: change with a stale width breaks every store()/search() at runtime.
_MODEL_DIMENSIONS = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "intfloat/multilingual-e5-large": 1024,
}

if os.getenv("EMBEDDING_DIMENSIONS"):
    EMBEDDING_DIMENSIONS = int(os.environ["EMBEDDING_DIMENSIONS"])
elif EMBEDDING_MODEL in _MODEL_DIMENSIONS:
    EMBEDDING_DIMENSIONS = _MODEL_DIMENSIONS[EMBEDDING_MODEL]
else:
    # Fail fast at import: a silently-wrong width would surface later as a
    # pgvector dimension mismatch on every memory operation.
    raise RuntimeError(
        f"Unknown EMBEDDING_MODEL {EMBEDDING_MODEL!r}: set EMBEDDING_DIMENSIONS "
        f"explicitly, or use one of {sorted(_MODEL_DIMENSIONS)}"
    )
_CACHE_DIR = os.getenv("FASTEMBED_CACHE_DIR", "/app/.fastembed_cache")
_MODEL_PATH = os.getenv("EMBEDDING_MODEL_PATH")

_model: Any = None


def _get_model() -> Any:
    """Lazily load an already-provisioned embedding model without networking.

    The ``fastembed`` import is deferred so this module imports without the
    heavy optional dependency present — unit tests patch ``_get_model``.
    """
    global _model
    if _model is None:
        # Set these before importing fastembed/huggingface_hub. The explicit
        # local_files_only argument below remains the fail-closed enforcement
        # even if huggingface_hub was imported earlier in the process.
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

        from fastembed import TextEmbedding

        model_options: dict[str, Any] = {"local_files_only": True}
        if _MODEL_PATH:
            model_options["specific_model_path"] = _MODEL_PATH

        logger.info(
            "Loading local embedding model %s from %s",
            EMBEDDING_MODEL,
            _MODEL_PATH or _CACHE_DIR,
        )
        _model = TextEmbedding(
            model_name=EMBEDDING_MODEL,
            cache_dir=_CACHE_DIR,
            **model_options,
        )
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
