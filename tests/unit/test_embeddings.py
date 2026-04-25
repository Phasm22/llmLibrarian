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


def test_legacy_default_onnx_path_returns_default_function(monkeypatch: Any) -> None:
    """LLMLIBRARIAN_EMBEDDING=default opts into the ONNX MiniLM path; must not
    instantiate sentence-transformers."""
    _install_fake_chroma(monkeypatch)
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING", "default")

    ef = get_embedding_function()

    assert isinstance(ef, _FakeDefaultEmbeddingFunction)


def test_legacy_default_is_case_insensitive(monkeypatch: Any) -> None:
    _install_fake_chroma(monkeypatch)
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING", "DEFAULT")

    ef = get_embedding_function()
    assert isinstance(ef, _FakeDefaultEmbeddingFunction)


def test_default_path_uses_sentence_transformer_with_mpnet(monkeypatch: Any) -> None:
    """No LLMLIBRARIAN_EMBEDDING means the modern mpnet path."""
    _install_fake_chroma(monkeypatch)
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING", raising=False)
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING_BATCH_SIZE", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_DEVICE", "cpu")

    ef = get_embedding_function()
    assert isinstance(ef, _FakeSentenceTransformerEmbeddingFunction)
    assert ef.model_name == "all-mpnet-base-v2"


def test_explicit_device_arg_overrides_env(monkeypatch: Any) -> None:
    _install_fake_chroma(monkeypatch)
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING", raising=False)
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING_BATCH_SIZE", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_DEVICE", "cuda")

    ef = get_embedding_function(device="cpu")
    assert ef.device == "cpu", "explicit device kwarg must win over env"


def test_custom_model_env_used(monkeypatch: Any) -> None:
    _install_fake_chroma(monkeypatch)
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING", raising=False)
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING_BATCH_SIZE", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_MODEL", "BAAI/bge-large-en-v1.5")
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_DEVICE", "cpu")

    ef = get_embedding_function()
    assert ef.model_name == "BAAI/bge-large-en-v1.5"


def test_ingest_parallel_embedding_device_returns_none_when_user_pinned_device(monkeypatch: Any) -> None:
    """If user has pinned a device, never override silently."""
    from embeddings import ingest_parallel_embedding_device
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_DEVICE", "mps")

    assert ingest_parallel_embedding_device(file_count=10000) is None


def test_ingest_parallel_embedding_device_below_threshold_returns_none(monkeypatch: Any) -> None:
    from embeddings import ingest_parallel_embedding_device
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING_DEVICE", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_LARGE_INGEST_FILE_THRESHOLD", "400")

    assert ingest_parallel_embedding_device(file_count=399) is None


def test_ingest_parallel_embedding_device_disabled_via_zero_threshold(monkeypatch: Any) -> None:
    from embeddings import ingest_parallel_embedding_device
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING_DEVICE", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_LARGE_INGEST_FILE_THRESHOLD", "0")

    assert ingest_parallel_embedding_device(file_count=999999) is None


def test_ingest_parallel_embedding_device_returns_none_when_auto_is_not_mps(monkeypatch: Any) -> None:
    """Only protects against MPS thread-safety. CUDA/CPU users are unaffected."""
    from embeddings import ingest_parallel_embedding_device
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING_DEVICE", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_LARGE_INGEST_FILE_THRESHOLD", "10")

    import embeddings as e
    monkeypatch.setattr(e, "_best_device", lambda batch_size=None: "cpu")
    assert ingest_parallel_embedding_device(file_count=500) is None

    monkeypatch.setattr(e, "_best_device", lambda batch_size=None: "cuda")
    assert ingest_parallel_embedding_device(file_count=500) is None


def test_ingest_parallel_embedding_device_forces_cpu_when_mps_auto(monkeypatch: Any) -> None:
    from embeddings import ingest_parallel_embedding_device
    import embeddings as e
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING_DEVICE", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_LARGE_INGEST_FILE_THRESHOLD", "10")
    monkeypatch.setattr(e, "_best_device", lambda batch_size=None: "mps")

    assert ingest_parallel_embedding_device(file_count=500) == "cpu"


def test_ingest_parallel_embedding_device_invalid_threshold_uses_default(monkeypatch: Any) -> None:
    from embeddings import ingest_parallel_embedding_device
    import embeddings as e
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING_DEVICE", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_LARGE_INGEST_FILE_THRESHOLD", "not-an-int")
    monkeypatch.setattr(e, "_best_device", lambda batch_size=None: "mps")

    assert ingest_parallel_embedding_device(file_count=399) is None
    assert ingest_parallel_embedding_device(file_count=400) == "cpu"


def test_best_device_respects_explicit_override(monkeypatch: Any) -> None:
    from embeddings import _best_device
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_DEVICE", "cuda:1")

    assert _best_device(batch_size=100) == "cuda:1"


def test_best_device_falls_back_to_cpu_without_torch(monkeypatch: Any) -> None:
    from embeddings import _best_device
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING_DEVICE", raising=False)
    monkeypatch.setitem(sys.modules, "torch", None)  # Forces ImportError on `import torch`

    assert _best_device(batch_size=100) == "cpu"

