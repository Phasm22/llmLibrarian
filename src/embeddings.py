"""
Embedding function for ChromaDB. Uses Chroma's default (ONNX + all-MiniLM-L6-v2)
for local, deterministic embeddings. Override via LLMLIBRARIAN_EMBEDDING env if needed.
"""
import os
from typing import Any

def get_embedding_function() -> Any:
    """Return a Chroma-compatible embedding function for indexing and query."""
    from chromadb.utils import embedding_functions
    # Chroma default: ONNX + all-MiniLM-L6-v2 (~1s per 100 chunks on CPU/MPS)
    kind = os.environ.get("LLMLIBRARIAN_EMBEDDING", "default").lower()
    if kind == "default" or not kind:
        return embedding_functions.DefaultEmbeddingFunction()
    if kind == "sentence_transformer":
        model = os.environ.get("LLMLIBRARIAN_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        return embedding_functions.SentenceTransformerEmbeddingFunction(model_name=model)
    return embedding_functions.DefaultEmbeddingFunction()
