"""
Optional reranker for retrieval: stage-1 wide net, then rerank to top_k.
Disabled if cross-encoder not available; then use_reranker is False.

The CrossEncoder model is cached per (model_name, device) to avoid loading
it on every query call.
"""
import os
import threading
from typing import Any

RERANK_STAGE1_N = 40  # fetch more when reranker is on, then cut to n_results

_model_cache: dict[tuple[str, str], Any] = {}
_cache_lock = threading.Lock()


def _get_reranker_device() -> str:
    override = os.environ.get("LLMLIBRARIAN_RERANK_DEVICE", "").strip()
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


def _get_model(model_name: str, device: str) -> Any:
    """Return a cached CrossEncoder; load on first use."""
    key = (model_name, device)
    with _cache_lock:
        if key not in _model_cache:
            from sentence_transformers import CrossEncoder
            _model_cache[key] = CrossEncoder(model_name, device=device)
        return _model_cache[key]


def is_reranker_enabled() -> bool:
    if os.environ.get("LLMLIBRARIAN_RERANK", "").lower() in ("0", "false", "no"):
        return False
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def rerank(
    query: str,
    docs: list[str],
    metas: list[dict | None],
    dists: list[float | None],
    top_k: int = 12,
    force: bool = False,
) -> tuple[list[str], list[dict | None], list[float | None]]:
    """Rerank (docs, metas, dists) by relevance to query; return top_k."""
    if not docs:
        return docs, metas, dists
    try:
        model_name = os.environ.get("LLMLIBRARIAN_RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
        device = _get_reranker_device()
        model = _get_model(model_name, device)
        pairs = [[query, d or ""] for d in docs]
        scores = model.predict(pairs)
        combined = list(zip(scores, docs, metas or [None] * len(docs), dists or [0.0] * len(docs)))
        combined.sort(key=lambda x: x[0], reverse=True)
        top = combined[:top_k]
        return [t[1] for t in top], [t[2] for t in top], [t[3] for t in top]
    except Exception:
        return docs[:top_k], (metas or [None] * len(docs))[:top_k], (dists or [0.0] * len(docs))[:top_k]
