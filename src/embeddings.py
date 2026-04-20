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
from typing import Any

# Empirically measured crossover on Apple M-series: MPS beats CPU at ~24+ texts per batch.
_DEFAULT_MPS_THRESHOLD = 24


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
    kind = os.environ.get("LLMLIBRARIAN_EMBEDDING", "").lower()
    if kind == "default":
        # Explicit opt-in to ONNX MiniLM path; onnxruntime will automatically
        # use CoreMLExecutionProvider on macOS when available.
        return embedding_functions.DefaultEmbeddingFunction()
    model = os.environ.get("LLMLIBRARIAN_EMBEDDING_MODEL", "all-mpnet-base-v2")
    resolved = device if device is not None else _best_device(batch_size)
    encode_batch_size = _embedding_batch_size()
    if encode_batch_size is not None:
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

        return BatchedSentenceTransformerEmbeddingFunction(model_name=model, device=resolved)
    return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=model, device=resolved)
