"""
Optional reranker for retrieval: stage-1 wide net, then rerank to top_k.
Disabled if cross-encoder not available; then use_reranker is False.

The CrossEncoder model is cached per (model_name, device) to avoid loading
it on every query call.
"""
import logging
import os
import re
import threading
import time
from typing import Any

RERANK_STAGE1_N = 40  # fetch more when reranker is on, then cut to n_results
RERANK_HEAD_MIN = 20
DEFAULT_RERANK_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

_logger = logging.getLogger("llmLibrarian.reranker")
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
    import model_idle

    model_idle.touch()
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


def rerank_decision(query: str, use_reranker: bool | None) -> str:
    """``disabled``, ``year_skipped`` or ``eligible`` for one query.

    A query naming a year is left in retrieval order: ask year-boosts by path
    before ranking, and the cross-encoder sees chunk text, not paths.
    """
    if use_reranker is None:
        use_reranker = is_reranker_enabled()
    if not use_reranker:
        return "disabled"
    if re.search(r"\b(20\d{2})\b", query or ""):
        return "year_skipped"
    return "eligible"


def rerank_order(query: str, docs: list[str]) -> tuple[list[int] | None, dict[str, Any]]:
    """Indices of ``docs`` best-first by cross-encoder relevance, or None on failure.

    Ties keep the incoming order, so the result is deterministic for a fixed model.
    """
    model_name = os.environ.get("LLMLIBRARIAN_RERANK_MODEL", DEFAULT_RERANK_MODEL)
    diag: dict[str, Any] = {"model": model_name, "candidates": len(docs)}
    if not docs:
        diag["status"] = "applied"
        return [], diag
    started = time.perf_counter()
    try:
        model = _get_model(model_name, _get_reranker_device())
        scores = [float(s) for s in model.predict([[query, d or ""] for d in docs])]
    except Exception as exc:
        _logger.warning("rerank failed, keeping retrieval order: %s: %s", type(exc).__name__, exc)
        diag["status"] = "failed"
        diag["error"] = f"{type(exc).__name__}: {exc}"
        return None, diag
    diag["status"] = "applied"
    diag["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return sorted(range(len(docs)), key=lambda i: (-scores[i], i)), diag


def rerank_head_size(n_results: int) -> int:
    """How many leading candidates the cross-encoder scores; the rest keep retrieval order."""
    return max(RERANK_HEAD_MIN, 2 * max(1, n_results))


def rerank_head(
    query: str,
    docs: list[str],
    metas: list[dict | None],
    dists: list[float | None],
    head: int,
) -> tuple[list[str], list[dict | None], list[float | None], dict[str, Any]]:
    """Rerank the first ``head`` candidates and append the rest unchanged.

    Bounds cross-encoder cost (it is linear in candidates) while leaving the
    tail available for diversity backfill.
    """
    head = min(head, len(docs))
    order, diag = rerank_order(query, docs[:head])
    if order is None:
        order = list(range(head))
    order = order + list(range(head, len(docs)))
    metas = list(metas or [None] * len(docs))
    dists = list(dists or [None] * len(docs))
    return [docs[i] for i in order], [metas[i] for i in order], [dists[i] for i in order], diag


def rerank(
    query: str,
    docs: list[str],
    metas: list[dict | None],
    dists: list[float | None],
    top_k: int | None = 12,
) -> tuple[list[str], list[dict | None], list[float | None]]:
    """Rerank (docs, metas, dists) by relevance to query; return top_k (all when None).

    Distances travel with their chunk; the cross-encoder logit is never returned.
    On model failure the incoming order is kept.
    """
    if not docs:
        return docs, metas, dists
    metas = list(metas or [None] * len(docs))
    dists = list(dists or [None] * len(docs))
    order, _diag = rerank_order(query, docs)
    if order is None:
        order = list(range(len(docs)))
    if top_k is not None:
        order = order[:top_k]
    return [docs[i] for i in order], [metas[i] for i in order], [dists[i] for i in order]
