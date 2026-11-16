from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from embeddings import EmbeddingDependencyError, _embedding_batch_size, get_embedding_function


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
        self.kwargs = _kwargs
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


def test_sentence_transformer_initialization_failure_is_actionable(monkeypatch: Any) -> None:
    class _BrokenSentenceTransformerEmbeddingFunction:
        def __init__(self, **_kwargs: Any) -> None:
            raise ValueError("sentence_transformers is not installed")

    fake_embedding_functions = SimpleNamespace(
        DefaultEmbeddingFunction=_FakeDefaultEmbeddingFunction,
        SentenceTransformerEmbeddingFunction=_BrokenSentenceTransformerEmbeddingFunction,
    )
    fake_utils = SimpleNamespace(embedding_functions=fake_embedding_functions)
    monkeypatch.setitem(sys.modules, "chromadb", SimpleNamespace(utils=fake_utils))
    monkeypatch.setitem(sys.modules, "chromadb.utils", fake_utils)
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING", raising=False)
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING_BATCH_SIZE", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_MODEL", "broken-model")

    with pytest.raises(EmbeddingDependencyError) as excinfo:
        get_embedding_function()

    assert "sentence-transformers" in str(excinfo.value)
    assert "uv sync --extra image" in str(excinfo.value)


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


def test_qwen_queries_use_instruction_but_documents_do_not(monkeypatch):
    from embeddings import QWEN3_MODEL
    _install_fake_chroma(monkeypatch)
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_MODEL", QWEN3_MODEL)
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_DEVICE", "cpu")
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_BATCH_SIZE", "8")
    ef = get_embedding_function()
    ef(["a document"])
    ef.embed_query(["a question"])
    docs, query = ef._model.calls
    assert "prompt_name" not in docs
    assert query["prompt_name"] == "query"
    assert docs["normalize_embeddings"] is query["normalize_embeddings"] is True
    assert docs["batch_size"] == query["batch_size"] == 8
    assert ef.expected_dimension == 1024


def test_qwen_matching_is_by_name_substring_not_exact_string(monkeypatch):
    """Any Qwen3-Embedding variant (4B, 8B, ...) gets the query-instruction
    adapter, not just the exact 0.6B model string."""
    _install_fake_chroma(monkeypatch)
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-4B")
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_DEVICE", "cpu")
    ef = get_embedding_function()
    ef(["a document"])
    ef.embed_query(["a question"])
    docs, query = ef._model.calls
    assert "prompt_name" not in docs
    assert query["prompt_name"] == "query"
    assert ef.expected_dimension == 2560


def test_qwen_caps_max_seq_length_per_call_side(monkeypatch):
    from embeddings import QWEN3_MODEL
    _install_fake_chroma(monkeypatch)
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_MODEL", QWEN3_MODEL)
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_DEVICE", "cpu")
    ef = get_embedding_function()
    ef(["a document"])
    assert ef._model.max_seq_length == 8192
    ef.embed_query(["a question"])
    assert ef._model.max_seq_length == 512


def test_qwen_loads_bf16_on_cuda_but_not_cpu(monkeypatch):
    from embeddings import QWEN3_MODEL
    import torch

    _install_fake_chroma(monkeypatch)
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_MODEL", QWEN3_MODEL)

    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_DEVICE", "cpu")
    cpu_ef = get_embedding_function()
    assert "model_kwargs" not in cpu_ef.kwargs

    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_DEVICE", "cuda")
    cuda_ef = get_embedding_function(device="cuda")
    assert cuda_ef.kwargs["model_kwargs"]["torch_dtype"] is torch.bfloat16


def test_explicit_model_validation_refuses_unreadable_collection(monkeypatch):
    from embeddings import validate_embedding_dimension
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B")
    def broken():
        raise OSError("offline")
    with pytest.raises(RuntimeError, match="Cannot validate"):
        validate_embedding_dimension(SimpleNamespace(count=broken), object())


def test_qwen_rejects_768_collection_before_encoding(monkeypatch):
    from embeddings import validate_embedding_dimension
    collection = SimpleNamespace(count=lambda: 3, get=lambda **kw: {"embeddings": [[0.0] * 768]})
    with pytest.raises(RuntimeError, match="768-dimensional.*1024-dimensional"):
        validate_embedding_dimension(collection, SimpleNamespace(expected_dimension=1024))


def test_explicit_model_never_falls_back_to_onnx(monkeypatch):
    from chroma_client import _SafeClient
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B")
    calls = []
    def conflict(**kwargs):
        calls.append(kwargs)
        raise ValueError("embedding function conflict with default")
    client = _SafeClient(SimpleNamespace(get_or_create_collection=conflict))
    with pytest.raises(ValueError, match="conflict"):
        client.get_or_create_collection("llmli", embedding_function=object())
    assert len(calls) == 1


def test_collection_model_identity_blocks_same_dimension_mixing(monkeypatch):
    from chroma_client import _SafeClient
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_MODEL", "new-model")
    collection = SimpleNamespace(metadata={"llmli_embedding_model": "old-model"})
    client = _SafeClient(SimpleNamespace(get_or_create_collection=lambda **kw: collection))
    with pytest.raises(RuntimeError, match="Embedding model mismatch"):
        client.get_or_create_collection("llmli", embedding_function=SimpleNamespace(model_name="new-model"))


def test_qwen_adapter_roundtrip_and_rejects_old_collection(monkeypatch, tmp_path):
    import chromadb
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
    from chroma_client import _SafeClient
    from embeddings import QWEN3_MODEL, _reset_ef_cache_for_tests
    model = _FakeModel()
    def encode(docs, **kwargs):
        model.calls.append({"docs": docs, **kwargs})
        return [[1.0] + [0.0] * 1023 for _ in docs]
    model.encode = encode
    def init(self, model_name, device, normalize_embeddings):
        self.model_name, self.device = model_name, device
        self.normalize_embeddings, self.kwargs, self._model = normalize_embeddings, {}, model
    monkeypatch.setattr(SentenceTransformerEmbeddingFunction, "__init__", init)
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_MODEL", QWEN3_MODEL)
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_DEVICE", "cpu")
    _reset_ef_cache_for_tests()
    ef = get_embedding_function()
    raw = chromadb.PersistentClient(path=str(tmp_path / "db"))
    client = _SafeClient(raw)
    old = raw.create_collection("old", embedding_function=None)
    old.add(ids=["old"], embeddings=[[1.0] * 768])
    with pytest.raises(RuntimeError, match="Embedding dimension mismatch"):
        client.get_or_create_collection("old", embedding_function=ef)
    col = client.get_or_create_collection("fresh", embedding_function=ef)
    col.add(ids=["new"], documents=["vegetable soup"])
    result = col.query(query_texts=["soup recipe"], n_results=1)
    assert result["ids"] == [["new"]]
    assert "prompt_name" not in model.calls[0]
    assert model.calls[1]["prompt_name"] == "query"
    assert col.metadata["llmli_embedding_model"] == QWEN3_MODEL


def test_watch_service_environment_keeps_embedding_contract():
    from jobs_runtime import _env_for_service
    settings = {
        "LLMLIBRARIAN_EMBEDDING_MODEL": "Qwen/Qwen3-Embedding-0.6B",
        "LLMLIBRARIAN_EMBEDDING_DEVICE": "cpu",
        "LLMLIBRARIAN_EMBEDDING_BATCH_SIZE": "8",
    }
    assert _env_for_service(settings) == settings
