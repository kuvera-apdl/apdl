"""Local embedding wiring — the heavy model is patched, so no download here.

Real-model behaviour is exercised end-to-end against the running stack; these
unit tests lock the async/threading wiring and output shape.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from app.memory import embeddings


class _FakeVec:
    """Stand-in for the numpy array fastembed yields (has .tolist())."""

    def __init__(self, data: list[float]) -> None:
        self._data = data

    def tolist(self) -> list[float]:
        return self._data


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts, batch_size=256):
        self.calls.append(list(texts))
        for index, _ in enumerate(texts):
            # Per-index sentinel in slot 0 lets tests assert order preservation.
            yield _FakeVec([float(index)] + [0.0] * (embeddings.EMBEDDING_DIMENSIONS - 1))


@pytest.fixture
def fake_model(monkeypatch):
    model = _FakeModel()
    monkeypatch.setattr(embeddings, "_get_model", lambda: model)
    return model


def test_dimensions_is_384():
    assert embeddings.EMBEDDING_DIMENSIONS == 384


def test_model_load_is_local_only(monkeypatch, tmp_path):
    calls = []

    def fake_text_embedding(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(embeddings, "_model", None)
    monkeypatch.setattr(embeddings, "_MODEL_PATH", str(tmp_path))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("HF_HUB_DISABLE_TELEMETRY", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "fastembed",
        SimpleNamespace(TextEmbedding=fake_text_embedding),
    )

    embeddings._get_model()

    assert calls == [
        {
            "model_name": embeddings.EMBEDDING_MODEL,
            "cache_dir": embeddings._CACHE_DIR,
            "specific_model_path": str(tmp_path),
            "local_files_only": True,
        }
    ]
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["HF_HUB_DISABLE_TELEMETRY"] == "1"


@pytest.mark.asyncio
async def test_embed_returns_single_384_vector(fake_model):
    vec = await embeddings.embed("hello world")
    assert isinstance(vec, list)
    assert len(vec) == 384
    assert fake_model.calls == [["hello world"]]


@pytest.mark.asyncio
async def test_embed_batch_preserves_order_and_count(fake_model):
    vecs = await embeddings.embed_batch(["a", "b", "c"])
    assert len(vecs) == 3
    assert all(len(v) == 384 for v in vecs)
    assert [v[0] for v in vecs] == [0.0, 1.0, 2.0]


@pytest.mark.asyncio
async def test_embed_batch_empty_short_circuits(fake_model):
    assert await embeddings.embed_batch([]) == []
    assert fake_model.calls == []
