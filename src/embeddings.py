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
  - LLMLIBRARIAN_MPS_BATCH_THRESHOLD -> chunk count above which MPS is preferred (default: 24)
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


def get_embedding_function(batch_size: int | None = None) -> Any:
    """
    Return a Chroma-compatible embedding function for indexing and query.

    Pass batch_size when known (e.g. number of chunks to embed) so the
    function can pick the optimal device for the workload.
    """
    from chromadb.utils import embedding_functions
    kind = os.environ.get("LLMLIBRARIAN_EMBEDDING", "").lower()
    if kind == "default":
        # Explicit opt-in to ONNX MiniLM path; onnxruntime will automatically
        # use CoreMLExecutionProvider on macOS when available.
        return embedding_functions.DefaultEmbeddingFunction()
    model = os.environ.get("LLMLIBRARIAN_EMBEDDING_MODEL", "all-mpnet-base-v2")
    device = _best_device(batch_size)
    return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=model, device=device)
