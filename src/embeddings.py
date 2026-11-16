"""
Embedding function for ChromaDB.
Default: all-mpnet-base-v2 via sentence-transformers (768-dim, better recall than MiniLM).

Device auto-selected based on batch size:
  - Small batches (<= LLMLIBRARIAN_MPS_BATCH_THRESHOLD, default 24): CPU is faster on Apple Silicon
    due to MPS kernel launch overhead dominating small workloads.
  - Large batches: MPS (Apple Silicon) > CUDA > CPU.
  Override with LLMLIBRARIAN_EMBEDDING_DEVICE to pin a specific device.

Other env vars:
  - LLMLIBRARIAN_EMBEDDING=default  -> Chroma ONNX DefaultEmbeddingFunction (all-MiniLM-L6-v2,
                                        384-dim, uses CoreML EP automatically on macOS)
  - LLMLIBRARIAN_EMBEDDING_MODEL    -> override model name (default: all-mpnet-base-v2)
  - LLMLIBRARIAN_EMBEDDING_BATCH_SIZE -> sentence-transformers encode batch size (default:
                                        library default, usually 32)
  - LLMLIBRARIAN_MPS_BATCH_THRESHOLD -> chunk count above which MPS is preferred (default: 24)
  - LLMLIBRARIAN_LARGE_INGEST_FILE_THRESHOLD -> during `add`/`pull`, if the file list is at least this
    many entries and auto device would be MPS, pin embeddings to CPU so parallel workers (default 8)
    are safe. Set to 0 to disable. Default: 400.
"""
import os
import threading
from typing import Any

# Empirically measured crossover on Apple M-series: MPS beats CPU at ~24+ texts per batch.
_DEFAULT_MPS_THRESHOLD = 24
QWEN3_MODEL = "Qwen/Qwen3-Embedding-0.6B"

# Known Qwen3-Embedding variants and their output dimension. Keyed by size tag
# because HF repo ids vary (owner prefix, casing); falls back to the 0.6B
# default below for any variant not listed here.
_QWEN3_EMBEDDING_DIMENSIONS = {"0.6b": 1024, "4b": 2560, "8b": 4096}
# Sane ceilings so a stray huge query/doc doesn't blow up latency or memory.
_QWEN3_QUERY_MAX_SEQ_LENGTH = 512
_QWEN3_DOC_MAX_SEQ_LENGTH = 8192


def _is_qwen3_embedding_model(model_name: str) -> bool:
    return "qwen3-embedding" in model_name.lower()


def _qwen3_expected_dimension(model_name: str) -> int:
    lowered = model_name.lower()
    for tag, dim in _QWEN3_EMBEDDING_DIMENSIONS.items():
        if tag in lowered:
            return dim
    return _QWEN3_EMBEDDING_DIMENSIONS["0.6b"]

# Cache sentence-transformer embedding functions across calls. Without this,
# every ingest / update_file / remove_file call in a long-lived MCP server
# instantiates a fresh SentenceTransformer (~500 MB of torch tensors), fragments
# the glibc heap, and drives anon RSS into the tens of GB over hours.
_ef_cache: dict[tuple, Any] = {}
_ef_cache_lock = threading.Lock()


class EmbeddingDependencyError(RuntimeError):
    """The configured text-embedding backend is missing or cannot initialize."""


def _mps_threshold() -> int:
    try:
        return int(os.environ.get("LLMLIBRARIAN_MPS_BATCH_THRESHOLD", _DEFAULT_MPS_THRESHOLD))
    except (TypeError, ValueError):
        return _DEFAULT_MPS_THRESHOLD


def _best_device(batch_size: int | None = None) -> str:
    """
    Pick the fastest available torch device for the given batch size.

    On Apple Silicon, MPS has kernel-launch overhead that makes it slower
    than CPU for small batches. The crossover is ~24 texts: below that,
    use CPU; at or above, MPS wins (2x throughput at batch=64).
    """
    override = os.environ.get("LLMLIBRARIAN_EMBEDDING_DEVICE", "").strip()
    if override:
        return override
    try:
        import torch
        if torch.backends.mps.is_available():
            if batch_size is not None and batch_size < _mps_threshold():
                return "cpu"
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except ImportError:
        pass
    return "cpu"


def ingest_parallel_embedding_device(file_count: int) -> str | None:
    """
    For large multi-file ingests on Apple Silicon, auto device picks MPS, but ingest then
    forces embedding_workers=1 (MPS is not thread-safe). Prefer CPU for those runs so
    multiple embedding threads can run without overriding the user's device env.
    """
    if os.environ.get("LLMLIBRARIAN_EMBEDDING_DEVICE", "").strip():
        return None
    try:
        thresh = int(os.environ.get("LLMLIBRARIAN_LARGE_INGEST_FILE_THRESHOLD", "400"))
    except (TypeError, ValueError):
        thresh = 400
    if thresh <= 0 or file_count < thresh:
        return None
    batch_hint = max(file_count, 64)
    if _best_device(batch_size=batch_hint) != "mps":
        return None
    return "cpu"


def _embedding_batch_size() -> int | None:
    raw = os.environ.get("LLMLIBRARIAN_EMBEDDING_BATCH_SIZE", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return max(1, min(value, 4096))


def get_embedding_function(batch_size: int | None = None, device: str | None = None) -> Any:
    """
    Return a Chroma-compatible embedding function for indexing and query.

    Pass batch_size when known (e.g. number of chunks to embed) so the
    function can pick the optimal device for the workload.
    If device is set (e.g. "cpu" from ingest_parallel_embedding_device), it overrides auto selection.
    """
    from chromadb.utils import embedding_functions
    import model_idle

    model_idle.touch()
    kind = os.environ.get("LLMLIBRARIAN_EMBEDDING", "").lower()
    model = os.environ.get("LLMLIBRARIAN_EMBEDDING_MODEL", "all-mpnet-base-v2")
    resolved = device if device is not None else _best_device(batch_size)
    encode_batch_size = _embedding_batch_size()
    cache_key = (kind, model, resolved, encode_batch_size)
    with _ef_cache_lock:
        cached = _ef_cache.get(cache_key)
        if cached is not None:
            return cached

        try:
            if kind == "default":
                # Explicit opt-in to ONNX MiniLM path; onnxruntime will automatically
                # use CoreMLExecutionProvider on macOS when available.
                ef: Any = embedding_functions.DefaultEmbeddingFunction()
            elif _is_qwen3_embedding_model(model):
                # Qwen retrieval is asymmetric: instruction on queries only.
                # A per-instance cache avoids Chroma's model-name-only class cache
                # retaining tensors after the idle reaper or sharing CPU/MPS models.
                base_cls = embedding_functions.SentenceTransformerEmbeddingFunction

                class Qwen3EmbeddingFunction(base_cls):
                    expected_dimension = _qwen3_expected_dimension(model)

                    def __init__(self) -> None:
                        self.models = {}
                        extra: dict[str, Any] = {}
                        if resolved.startswith("cuda"):
                            # bf16 halves memory and is the dtype Qwen3-Embedding
                            # was trained/evaluated in; CPU/MPS stay fp32.
                            import torch

                            extra["model_kwargs"] = {"torch_dtype": torch.bfloat16}
                        super().__init__(model_name=model, device=resolved, normalize_embeddings=True, **extra)
                        self._seq_len_lock = threading.Lock()

                    def _encode(self, input: Any, *, query: bool) -> Any:
                        import numpy as np

                        model_idle.touch()
                        kwargs = {"convert_to_numpy": True, "normalize_embeddings": True}
                        if encode_batch_size is not None:
                            kwargs["batch_size"] = encode_batch_size
                        if query:
                            kwargs["prompt_name"] = "query"
                        # Shared model instance: pin the seq-length cap for this
                        # call's side (query vs. doc) and encode under the same
                        # lock so a concurrent call for the other side can't
                        # mutate it mid-encode.
                        with self._seq_len_lock:
                            self._model.max_seq_length = (
                                _QWEN3_QUERY_MAX_SEQ_LENGTH if query else _QWEN3_DOC_MAX_SEQ_LENGTH
                            )
                            result = self._model.encode(list(input), **kwargs)
                        return [np.asarray(row, dtype=np.float32) for row in result]

                    def __call__(self, input: Any) -> Any:
                        return self._encode(input, query=False)

                    def embed_query(self, input: Any) -> Any:
                        return self._encode(input, query=True)

                ef = Qwen3EmbeddingFunction()
            elif encode_batch_size is not None:
                base_cls = embedding_functions.SentenceTransformerEmbeddingFunction

                class BatchedSentenceTransformerEmbeddingFunction(base_cls):  # type: ignore[misc, valid-type]
                    def __call__(self, input: Any) -> Any:
                        import numpy as np

                        embeddings = self._model.encode(
                            list(input),
                            batch_size=encode_batch_size,
                            convert_to_numpy=True,
                            normalize_embeddings=self.normalize_embeddings,
                        )
                        return [np.array(embedding, dtype=np.float32) for embedding in embeddings]

                ef = BatchedSentenceTransformerEmbeddingFunction(model_name=model, device=resolved)
            else:
                ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=model, device=resolved)
        except Exception as exc:
            backend = "Chroma ONNX" if kind == "default" else "sentence-transformers"
            raise EmbeddingDependencyError(
                f"Text embedding backend {backend!r} could not initialize: "
                f"{type(exc).__name__}: {exc}. "
                "Run `uv sync` in the project checkout"
                + (" (or `uv sync --extra image` for photo indexing)." if kind != "default" else ".")
            ) from exc

        _ef_cache[cache_key] = ef
        return ef


def validate_embedding_dimension(collection: Any, ef: Any) -> None:
    """Raise a clear error if `ef` would produce vectors of a different
    dimension than what's already stored in `collection`.

    Chroma itself only catches this deep inside collection.add(), after
    chunking/embedding work has already happened, with an opaque
    "Collection expecting embedding with dimension of X, got Y" error and a
    partially-completed ingest. Checking here — once, before any of that
    work starts — fails fast with an actionable message instead.

    No-op (not an error) for a fresh/empty collection: there's nothing to
    conflict with yet, so any dimension is valid.
    """
    try:
        existing_count = collection.count()
    except Exception as exc:
        if os.environ.get("LLMLIBRARIAN_EMBEDDING_MODEL", "").strip():
            raise RuntimeError("Cannot validate configured embedding model against the collection") from exc
        return
    if not existing_count:
        return

    try:
        peek = collection.get(limit=1, include=["embeddings"])
        existing_embeddings = peek.get("embeddings")
        if existing_embeddings is None or len(existing_embeddings) == 0:
            return
        existing_dim = len(existing_embeddings[0])
    except Exception as exc:
        if os.environ.get("LLMLIBRARIAN_EMBEDDING_MODEL", "").strip():
            raise RuntimeError("Cannot validate configured embedding model against the collection") from exc
        return

    try:
        new_dim = getattr(ef, "expected_dimension", None)
        if new_dim is None:
            probe = ef(["dimension probe"])
            new_dim = len(probe[0])
    except Exception as exc:
        if os.environ.get("LLMLIBRARIAN_EMBEDDING_MODEL", "").strip():
            raise RuntimeError("Cannot validate configured embedding model against the collection") from exc
        return

    if existing_dim and new_dim and existing_dim != new_dim:
        raise RuntimeError(
            "Embedding dimension mismatch: this collection already holds "
            f"{existing_dim}-dimensional vectors, but the configured embedding "
            f"function produces {new_dim}-dimensional vectors.\n"
            "This means LLMLIBRARIAN_EMBEDDING or LLMLIBRARIAN_EMBEDDING_MODEL "
            "doesn't match whatever model this collection was originally built with.\n"
            "Options:\n"
            "  - Unset LLMLIBRARIAN_EMBEDDING / LLMLIBRARIAN_EMBEDDING_MODEL (or set them "
            "back) to match the original model\n"
            "  - If you intend to switch models, every silo needs a full reindex — there is "
            "no partial-reindex path for a dimension change"
        )


def _reset_ef_cache_for_tests() -> None:
    """Drop the cached embedding functions. Test-only helper."""
    with _ef_cache_lock:
        _ef_cache.clear()
