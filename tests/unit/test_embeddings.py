from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

from embeddings import _embedding_batch_size, get_embedding_function


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def encode(self, docs: list[str], **kwargs: Any) -> list[list[float]]:
        self.calls.append({"docs": docs, **kwargs})
        return [[1.0, 2.0] for _ in docs]


class _FakeSentenceTransformerEmbeddingFunction:
    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        device: str = "cpu",
        normalize_embeddings: bool = False,
        **_kwargs: Any,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.normalize_embeddings = normalize_embeddings
        self._model = _FakeModel()


class _FakeDefaultEmbeddingFunction:
    pass


def _install_fake_chroma(monkeypatch: Any) -> None:
    fake_embedding_functions = SimpleNamespace(
        DefaultEmbeddingFunction=_FakeDefaultEmbeddingFunction,
        SentenceTransformerEmbeddingFunction=_FakeSentenceTransformerEmbeddingFunction,
    )
    fake_utils = SimpleNamespace(embedding_functions=fake_embedding_functions)
    monkeypatch.setitem(sys.modules, "chromadb", SimpleNamespace(utils=fake_utils))
    monkeypatch.setitem(sys.modules, "chromadb.utils", fake_utils)


def test_embedding_batch_size_parses_and_clamps(monkeypatch: Any) -> None:
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING_BATCH_SIZE", raising=False)
    assert _embedding_batch_size() is None

    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_BATCH_SIZE", "128")
    assert _embedding_batch_size() == 128

    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_BATCH_SIZE", "0")
    assert _embedding_batch_size() == 1

    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_BATCH_SIZE", "99999")
    assert _embedding_batch_size() == 4096

    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_BATCH_SIZE", "not-an-int")
    assert _embedding_batch_size() is None


def test_embedding_batch_size_is_passed_to_sentence_transformer_encode(monkeypatch: Any) -> None:
    _install_fake_chroma(monkeypatch)
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_DEVICE", "cuda")
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_BATCH_SIZE", "128")

    ef = get_embedding_function(batch_size=512)
    result = ef(["alpha", "beta"])

    assert result[0].tolist() == [1.0, 2.0]
    assert ef.device == "cuda"
    assert ef._model.calls == [
        {
            "docs": ["alpha", "beta"],
            "batch_size": 128,
            "convert_to_numpy": True,
            "normalize_embeddings": False,
        }
    ]

