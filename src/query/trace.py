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
    project_count: int | None = None,
    project_samples: list[str] | None = None,
    bound_silo: str | None = None,
    binding_confidence: float | None = None,
    binding_reason: str | None = None,
    weak_scope_gate: bool | None = None,
    catalog_retry_used: bool | None = None,
    catalog_retry_silo: str | None = None,
    filetype_hints: list[str] | None = None,
    answer_kind: str | None = None,
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
    if project_count is not None:
        payload["project_count"] = project_count
    if project_samples:
        payload["project_samples"] = project_samples[:5]
    if bound_silo:
        payload["bound_silo"] = bound_silo
    if binding_confidence is not None:
        payload["binding_confidence"] = round(float(binding_confidence), 3)
    if binding_reason:
        payload["binding_reason"] = binding_reason
    if weak_scope_gate is not None:
        payload["weak_scope_gate"] = bool(weak_scope_gate)
    if catalog_retry_used is not None:
        payload["catalog_retry_used"] = bool(catalog_retry_used)
    if catalog_retry_silo:
        payload["catalog_retry_silo"] = catalog_retry_silo
    if filetype_hints:
        payload["filetype_hints"] = list(filetype_hints)
    if answer_kind:
        payload["answer_kind"] = str(answer_kind)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass
