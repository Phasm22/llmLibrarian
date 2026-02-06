"""
Optional reranker for retrieval: stage-1 wide net, then rerank to top_k.
Disabled if cross-encoder not available; then use_reranker is False.
"""
import os
from typing import Any

RERANK_STAGE1_N = 40  # fetch more when reranker is on, then cut to n_results

def is_reranker_enabled() -> bool:
    if os.environ.get("LLMLIBRARIAN_RERANK", "").lower() in ("0", "false", "no"):
        return False
    try:
        # Optional: cross-encoder or similar
        import sentence_transformers
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
    """Rerank (docs, metas, dists) by relevance to query; return top_k. If reranker not loaded, return as-is."""
    if not docs:
        return docs, metas, dists
    try:
        from sentence_transformers import CrossEncoder
        model_name = os.environ.get("LLMLIBRARIAN_RERANK_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2")
        model = CrossEncoder(model_name)
        pairs = [[query, d or ""] for d in docs]
        scores = model.predict(pairs)
        combined = list(zip(scores, docs, metas or [None] * len(docs), dists or [0.0] * len(docs)))
        combined.sort(key=lambda x: (x[0],), reverse=True)
        top = combined[:top_k]
        return [t[1] for t in top], [t[2] for t in top], [t[3] for t in top]
    except Exception:
        return docs[:top_k], (metas or [None] * len(docs))[:top_k], (dists or [0.0] * len(docs))[:top_k]
