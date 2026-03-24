"""
Embedding function for ChromaDB.
Default: all-mpnet-base-v2 via sentence-transformers (768-dim, better recall than MiniLM).
Device auto-selected: MPS (Apple Silicon) > CUDA > CPU.
Override via env vars:
  - LLMLIBRARIAN_EMBEDDING=default  -> Chroma ONNX DefaultEmbeddingFunction (all-MiniLM-L6-v2, 384-dim, CPU only)
  - LLMLIBRARIAN_EMBEDDING_MODEL    -> override model name (default: all-mpnet-base-v2)
  - LLMLIBRARIAN_EMBEDDING_DEVICE   -> override device (mps/cuda/cpu)
"""
import os
from typing import Any


def _best_device() -> str:
    """Pick the fastest available torch device."""
    override = os.environ.get("LLMLIBRARIAN_EMBEDDING_DEVICE", "").strip()
    if override:
        return override
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except ImportError:
        pass
    return "cpu"


def get_embedding_function() -> Any:
    """Return a Chroma-compatible embedding function for indexing and query."""
    from chromadb.utils import embedding_functions
    kind = os.environ.get("LLMLIBRARIAN_EMBEDDING", "").lower()
    if kind == "default":
        # Explicit opt-in to legacy ONNX MiniLM (set LLMLIBRARIAN_EMBEDDING=default to revert)
        return embedding_functions.DefaultEmbeddingFunction()
    model = os.environ.get("LLMLIBRARIAN_EMBEDDING_MODEL", "all-mpnet-base-v2")
    device = _best_device()
    return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=model, device=device)
