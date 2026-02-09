"""
Trace logging: append JSON-lines to LLMLIBRARIAN_TRACE file for debugging and audit.
"""
import json
import os
from typing import Any


def write_trace(
    intent: str,
    n_stage1: int,
    n_results: int,
    model: str,
    silo: str | None,
    source_label: str,
    num_docs: int,
    time_ms: float,
    query_len: int,
    hybrid_used: bool = False,
    receipt_metas: list[dict | None] | None = None,
    guardrail_no_match: bool | None = None,
    guardrail_reason: str | None = None,
    requested_year: str | None = None,
    requested_form: str | None = None,
    requested_line: str | None = None,
) -> None:
    """Append one JSON-line to LLMLIBRARIAN_TRACE file (if set). Optional receipt: source paths and chunk hashes for chunks sent to the LLM. No-op if env unset. Does not raise."""
    path = os.environ.get("LLMLIBRARIAN_TRACE")
    if not path:
        return
    payload: dict[str, Any] = {
        "intent": intent,
        "n_stage1": n_stage1,
        "n_results": n_results,
        "model": model,
        "silo": silo,
        "source_label": source_label,
        "num_docs": num_docs,
        "time_ms": round(time_ms, 2),
        "query_len": query_len,
        "hybrid": hybrid_used,
    }
    if receipt_metas:
        payload["receipt"] = [
            {
                "source": (m or {}).get("source") or "",
                "chunk_hash": (m or {}).get("chunk_hash") or "",
            }
            for m in receipt_metas
        ]
    if guardrail_no_match is not None:
        payload["guardrail_no_match"] = bool(guardrail_no_match)
    if guardrail_reason:
        payload["guardrail_reason"] = guardrail_reason
    if requested_year:
        payload["requested_year"] = requested_year
    if requested_form:
        payload["requested_form"] = requested_form
    if requested_line:
        payload["requested_line"] = requested_line
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
