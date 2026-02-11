"""
Trust-first deterministic guardrails: field lookup (year/form/line) and
income-year-total. No LLM; no cross-year inference.
"""
import re
from typing import Any

from style import bold, dim, label_style

from query.context import query_mentioned_years
from query.formatting import format_source, style_answer


DIRECT_VALUE_METRIC_KEYWORDS = (
    "rank",
    "margin",
    "nps",
    "sla",
    "vendor",
    "close",
    "spoilage",
    "labor ratio",
    "loyalty",
    "cash conversion",
    "churn",
    "minutes",
    "%",
)
DIRECT_VALUE_SKIP_KEYWORDS = (
    "summary",
    "overview",
    "architecture",
    "design choices",
    "how",
    "why",
)
DIRECT_QUERY_STOPWORDS = {
    "what",
    "is",
    "the",
    "in",
    "of",
    "for",
    "as",
    "now",
    "latest",
    "current",
    "did",
    "does",
    "was",
    "are",
    "to",
    "on",
    "a",
    "an",
    "this",
    "that",
    "it",
}


def parse_csv_rank_request(query: str) -> dict[str, str] | None:
    """Parse rank lookup requests like 'ranked number 1 in 2020'."""
    q = (query or "").strip().lower()
    if not q:
        return None
    if "rank" not in q and "number" not in q and "#" not in q:
        return None
    rank_m = re.search(r"\b(?:rank(?:ed)?(?:\s+number)?|number|#)\s*(\d{1,3})\b", q)
    if not rank_m:
        return None
    year_m = re.search(r"\b(20\d{2})\b", q)
    out = {"rank": rank_m.group(1)}
    if year_m:
        out["year"] = year_m.group(1)
    return out


def extract_numeric_or_key_values_from_query(query: str) -> dict[str, Any] | None:
    """Return value-lookup query signals for deterministic direct guardrail, else None."""
    q = (query or "").strip().lower()
    if not q:
        return None
    if any(k in q for k in DIRECT_VALUE_SKIP_KEYWORDS):
        return None
    if not any(k in q for k in DIRECT_VALUE_METRIC_KEYWORDS):
        return None
    year_m = re.search(r"\b(20\d{2})\b", q)
    words = re.findall(r"[a-z][a-z0-9']{1,}", q)
    metric_tokens = {w for w in words if w in {"rank", "margin", "nps", "sla", "vendor", "close", "spoilage", "labor", "ratio", "loyalty", "cash", "conversion", "churn", "minutes"}}
    query_tokens = {w.strip("'") for w in words if w not in DIRECT_QUERY_STOPWORDS and w not in metric_tokens and not w.isdigit()}
    return {
        "query": q,
        "year": year_m.group(1) if year_m else None,
        "metric_tokens": metric_tokens,
        "query_tokens": query_tokens,
    }


def _extract_csv_field_value(doc: str, key: str) -> str | None:
    """Extract key=value field from row-style CSV chunks."""
    text = doc or ""
    pat = rf'(?i)\b"?{re.escape(key)}"?\s*=\s*"?([^"|\n]+)"?'
    m = re.search(pat, text)
    if not m:
        return None
    value = " ".join((m.group(1) or "").strip().split())
    return value or None


def _source_priority(meta: dict | None, canonical_tokens: list[str], deprioritized_tokens: list[str]) -> int:
    source = ((meta or {}).get("source") or "").lower()
    score = 0
    if any(tok.lower() in source for tok in canonical_tokens):
        score += 10
    if any(tok.lower() in source for tok in deprioritized_tokens):
        score -= 5
    return score


def extract_candidate_value_pairs_from_context(
    docs: list[str],
    metas: list[dict | None],
    query_signals: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Extract (label, value) pairs from retrieved context that overlap with query terms.
    Supports CSV row key/value and colon-form statements.
    """
    out: list[dict[str, Any]] = []
    q_tokens: set[str] = set(query_signals.get("query_tokens") or set())
    metric_tokens: set[str] = set(query_signals.get("metric_tokens") or set())
    year = query_signals.get("year")
    for i, doc in enumerate(docs):
        meta = metas[i] if i < len(metas) else None
        source = ((meta or {}).get("source") or "")
        source_l = source.lower()
        if year and year not in source_l:
            continue
        text = (doc or "").strip()
        if not text:
            continue

        if "CSV row" in text:
            # CSV row extraction is only relevant to rank-style direct lookups.
            if metric_tokens and "rank" not in metric_tokens:
                continue
            rank = _extract_csv_field_value(text, "rank")
            restaurant = _extract_csv_field_value(text, "restaurant")
            if rank and restaurant:
                label = "restaurant rank"
                label_tokens = set(re.findall(r"[a-z][a-z0-9']{1,}", f"{label} {restaurant}".lower()))
                overlap = len(q_tokens & label_tokens)
                if overlap >= 1:
                    out.append({"label": label, "value": rank, "meta": meta, "doc": text, "overlap": overlap})

        for m in re.finditer(r"(?im)\b([^:\n]{3,120})\s*:\s*([^\n]{1,80})", text):
            label = " ".join((m.group(1) or "").strip().split())
            value = " ".join((m.group(2) or "").strip().split())
            if not label or not value:
                continue
            label_tokens = set(re.findall(r"[a-z][a-z0-9']{1,}", label.lower()))
            if metric_tokens and len(metric_tokens & label_tokens) == 0:
                continue
            overlap = len(q_tokens & label_tokens)
            if overlap < 1:
                continue
            if not re.search(r"\d|%|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+", value):
                continue
            out.append({"label": label, "value": value, "meta": meta, "doc": text, "overlap": overlap})
    return out


def select_consistent_value(
    candidates: list[dict[str, Any]],
    canonical_tokens: list[str],
    deprioritized_tokens: list[str],
) -> dict[str, Any] | None:
    """
    Choose a deterministic value when supported by weighted evidence.
    Returns {'status': 'selected'|'abstain', ...} or None if insufficient candidates.
    """
    if not candidates:
        return None
    groups: dict[str, dict[str, Any]] = {}
    for c in candidates:
        value_key = str(c.get("value") or "").strip().lower()
        if not value_key:
            continue
        meta = c.get("meta")
        score = int(c.get("overlap") or 0) + _source_priority(meta if isinstance(meta, dict) or meta is None else None, canonical_tokens, deprioritized_tokens)
        g = groups.setdefault(value_key, {"score": 0, "items": []})
        g["score"] += score
        g["items"].append(c)
    if not groups:
        return None
    ranked = sorted(groups.items(), key=lambda kv: kv[1]["score"], reverse=True)
    best_val, best_group = ranked[0]
    best_score = int(best_group["score"])
    best_has_canonical = any(
        _source_priority(item.get("meta"), canonical_tokens, deprioritized_tokens) > 0
        for item in best_group["items"]
    )
    if best_score < 1:
        return {"status": "no_confident_match", "reason": "low_support"}
    if len(ranked) > 1:
        second_val, second_group = ranked[1]
        second_score = int(second_group["score"])
        second_has_canonical = any(
            _source_priority(item.get("meta"), canonical_tokens, deprioritized_tokens) > 0
            for item in second_group["items"]
        )
        if best_score == second_score:
            if best_has_canonical and not second_has_canonical:
                pass
            elif second_has_canonical and not best_has_canonical:
                return {"status": "no_confident_match", "reason": "canonical_lost_tie"}
            else:
                return {"status": "abstain_equal_conflict", "reason": "equal_conflict"}
        elif (best_score - second_score) < 2 and not best_has_canonical:
            return {"status": "no_confident_match", "reason": "weak_margin"}
    best_item = sorted(
        best_group["items"],
        key=lambda x: -_source_priority(x.get("meta"), canonical_tokens, deprioritized_tokens),
    )[0]
    return {"status": "selected", "value": best_val, "item": best_item}


def run_csv_rank_lookup_guardrail(
    *,
    collection: Any,
    use_unified: bool,
    silo: str | None,
    subscope_where: dict[str, Any] | None,
    query: str,
    source_label: str,
    no_color: bool,
) -> dict[str, Any] | None:
    """Deterministic rank lookup over CSV row chunks, or None when not applicable."""
    req = parse_csv_rank_request(query)
    if not req:
        return None
    requested_rank = req["rank"]
    requested_year = req.get("year")

    get_kw: dict[str, Any] = {"include": ["documents", "metadatas"]}
    if use_unified and silo:
        get_kw["where"] = {"silo": silo}
    elif subscope_where:
        get_kw["where"] = subscope_where
    try:
        all_rows = collection.get(**get_kw)
    except Exception:
        return None

    docs_all = _flatten_get_list(all_rows.get("documents"))
    metas_all = _flatten_get_list(all_rows.get("metadatas"))
    candidates: list[tuple[str, dict | None]] = []
    for i, doc_raw in enumerate(docs_all):
        doc = str(doc_raw or "")
        if "CSV row" not in doc:
            continue
        rank_value = _extract_csv_field_value(doc, "rank")
        if not rank_value or rank_value != requested_rank:
            continue
        meta = metas_all[i] if i < len(metas_all) else None
        candidates.append((doc, meta if isinstance(meta, dict) or meta is None else None))

    if not candidates:
        return None

    if requested_year:
        year_hits = [c for c in candidates if requested_year in (((c[1] or {}).get("source") or ""))]
        if year_hits:
            candidates = year_hits

    # Stable deterministic ordering by source path then row text.
    candidates.sort(key=lambda x: ((((x[1] or {}).get("source") or "")), x[0]))
    chosen_doc, chosen_meta = candidates[0]
    restaurant = _extract_csv_field_value(chosen_doc, "restaurant")
    if not restaurant:
        return None

    if len(candidates) == 1:
        answer = style_answer(f"Rank {requested_rank}: {restaurant}", no_color)
    else:
        answer = style_answer(
            f"Rank {requested_rank}: {restaurant} (multiple ranking tables matched; showing first deterministic match).",
            no_color,
        )

    out = [
        answer,
        "",
        dim(no_color, "---"),
        label_style(no_color, f"Answered by: {source_label}"),
        "",
        bold(no_color, "Sources:"),
        format_source(chosen_doc, chosen_meta, None, no_color=no_color),
    ]
    return {
        "response": "\n".join(out),
        "guardrail_no_match": False,
        "guardrail_reason": "csv_rank_lookup",
        "requested_year": requested_year,
        "requested_form": None,
        "requested_line": None,
        "num_docs": 1,
        "receipt_metas": [chosen_meta] if chosen_meta else None,
    }


def run_direct_value_consistency_guardrail(
    *,
    query: str,
    docs: list[str],
    metas: list[dict | None],
    source_label: str,
    no_color: bool,
    canonical_tokens: list[str],
    deprioritized_tokens: list[str],
) -> dict[str, Any] | None:
    """Deterministic value-consistency guardrail for direct factual lookups."""
    signals = extract_numeric_or_key_values_from_query(query)
    if not signals:
        return None
    candidates = extract_candidate_value_pairs_from_context(docs, metas, signals)
    decision = select_consistent_value(candidates, canonical_tokens, deprioritized_tokens)
    if decision is None:
        return None
    status = str(decision.get("status") or "")
    if status == "no_confident_match":
        return None
    if status == "abstain_equal_conflict":
        answer = style_answer("I don't have enough evidence to choose one value confidently.", no_color)
        out = [
            answer,
            "",
            dim(no_color, "---"),
            label_style(no_color, f"Answered by: {source_label}"),
            "",
            bold(no_color, "Sources:"),
        ]
        # Include top sources for debuggable grounding even on abstain.
        for c in candidates[:2]:
            out.append(format_source(str(c.get("doc") or ""), c.get("meta"), None, no_color=no_color))
        return {
            "response": "\n".join(out),
            "guardrail_no_match": True,
            "guardrail_reason": f"direct_value_{decision.get('reason')}",
            "requested_year": signals.get("year"),
            "requested_form": None,
            "requested_line": None,
            "num_docs": len(candidates),
            "receipt_metas": [c.get("meta") for c in candidates if c.get("meta")][:5] if candidates else None,
        }
    item = decision.get("item") or {}
    label = str(item.get("label") or "value")
    value = str(item.get("value") or decision.get("value") or "").strip()
    meta = item.get("meta")
    doc = str(item.get("doc") or "")
    if not value or not isinstance(meta, dict):
        return None
    answer = style_answer(f"{label}: {value}", no_color)
    out = [
        answer,
        "",
        dim(no_color, "---"),
        label_style(no_color, f"Answered by: {source_label}"),
        "",
        bold(no_color, "Sources:"),
        format_source(doc, meta, None, no_color=no_color),
    ]
    return {
        "response": "\n".join(out),
        "guardrail_no_match": False,
        "guardrail_reason": "direct_value_consistency",
        "requested_year": signals.get("year"),
        "requested_form": None,
        "requested_line": None,
        "num_docs": 1,
        "receipt_metas": [meta] if meta else None,
    }


def parse_field_lookup_request(query: str) -> dict[str, str] | None:
    """Parse explicit year/form/line lookup requests (e.g. "2024 form 1040 line 9")."""
    q = (query or "").strip().lower()
    if not q:
        return None
    year_m = re.search(r"\b(20\d{2})\b", q)
    line_m = re.search(r"\bline\s+(\d{1,3}[a-z]?)\b", q)
    form_m = re.search(r"\bform\s+(\d{3,4}(?:-[a-z0-9]+)?)\b", q)
    if not form_m:
        form_m = re.search(r"\b(1040(?:-sr)?)\b", q)
    if not (year_m and line_m and form_m):
        return None
    return {
        "year": year_m.group(1),
        "form": form_m.group(1).upper(),
        "line": line_m.group(1).lower(),
    }


def _field_lookup_no_match_message(year: str, form: str, line: str) -> str:
    """Deterministic trust-first response when the line is missing in requested-year docs."""
    return (
        f"I found {year} tax documents, but I could not find Form {form} line {line} in extractable text. "
        "I'm not inferring from other years."
    )


def _field_lookup_not_indexed_message(year: str) -> str:
    """Deterministic trust-first response when requested-year docs are not indexed."""
    return f"I could not find indexed tax documents for {year} in this silo."


def _flatten_get_list(values: list[Any] | None) -> list[Any]:
    """Normalize Chroma get() output into a flat list."""
    if not values:
        return []
    if values and isinstance(values[0], list):
        return values[0]  # type: ignore[index]
    return values


def _source_or_doc_has_form(source: str, doc: str, form: str) -> bool:
    f = (form or "").strip().lower()
    if not f:
        return False
    compact = f.replace("-", "")
    hay = f"{source}\n{doc}".lower()
    tokens = {
        f,
        compact,
        f"form {f}",
        f"form {compact}",
    }
    return any(tok in hay for tok in tokens if tok)


def _extract_exact_line_value(doc: str, line: str) -> str | None:
    """Extract value for an exact requested line from enriched text."""
    text = doc or ""
    line_pat = re.escape((line or "").strip().lower())
    if not line_pat:
        return None
    patterns = [
        rf"(?im)\bline\s*{line_pat}\s*[:\-]\s*([^\n|]+)",
        rf"(?im)^\|\s*{line_pat}\s*\|\s*([^|\n]+)\|",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        value = " ".join((m.group(1) or "").strip().split())
        if value:
            return value
    return None


def field_lookup_candidates_from_scope(
    docs: list[str],
    metas: list[dict | None],
    year: str,
    form: str,
) -> tuple[list[tuple[str, dict | None]], list[tuple[str, dict | None]]]:
    """
    Return (year_docs, year_form_docs), where each item is (doc, meta).
    Cross-year fallback is intentionally disallowed.
    """
    year_docs: list[tuple[str, dict | None]] = []
    year_form_docs: list[tuple[str, dict | None]] = []
    for i, doc in enumerate(docs):
        meta = metas[i] if i < len(metas) else None
        source = ((meta or {}).get("source") or "").lower()
        if year not in source:
            continue
        year_docs.append((doc, meta))
        if _source_or_doc_has_form(source, doc or "", form):
            year_form_docs.append((doc, meta))
    return year_docs, year_form_docs


def run_field_lookup_guardrail(
    *,
    collection: Any,
    use_unified: bool,
    silo: str | None,
    subscope_where: dict[str, Any] | None,
    query: str,
    source_label: str,
    no_color: bool,
) -> dict[str, Any] | None:
    """Return deterministic guardrail answer for explicit field lookups, or None if not applicable."""
    req = parse_field_lookup_request(query)
    if not req:
        return None

    get_kw: dict[str, Any] = {"include": ["documents", "metadatas"]}
    if use_unified and silo:
        get_kw["where"] = {"silo": silo}
    elif subscope_where:
        get_kw["where"] = subscope_where
    try:
        all_rows = collection.get(**get_kw)
    except Exception:
        return None
    docs_all = _flatten_get_list(all_rows.get("documents"))
    metas_all = _flatten_get_list(all_rows.get("metadatas"))
    year_docs, year_form_docs = field_lookup_candidates_from_scope(
        docs=[str(d or "") for d in docs_all],
        metas=[m if isinstance(m, dict) or m is None else None for m in metas_all],
        year=req["year"],
        form=req["form"],
    )

    if not year_docs:
        response = _field_lookup_not_indexed_message(req["year"])
        response = "\n".join([response, "", dim(no_color, "---"), label_style(no_color, f"Answered by: {source_label}")])
        return {
            "response": response,
            "guardrail_no_match": True,
            "guardrail_reason": "year_docs_not_indexed",
            "requested_year": req["year"],
            "requested_form": req["form"],
            "requested_line": req["line"],
            "num_docs": 0,
            "receipt_metas": None,
        }

    for doc, meta in year_form_docs:
        value = _extract_exact_line_value(doc or "", req["line"])
        if not value:
            continue
        answer = style_answer(
            f"Form {req['form']} line {req['line']} ({req['year']}): {value}",
            no_color,
        )
        out = [
            answer,
            "",
            dim(no_color, "---"),
            label_style(no_color, f"Answered by: {source_label}"),
            "",
            bold(no_color, "Sources:"),
            format_source(doc or "", meta, None, no_color=no_color),
        ]
        return {
            "response": "\n".join(out),
            "guardrail_no_match": False,
            "guardrail_reason": None,
            "requested_year": req["year"],
            "requested_form": req["form"],
            "requested_line": req["line"],
            "num_docs": 1,
            "receipt_metas": [meta] if meta else None,
        }

    response = _field_lookup_no_match_message(req["year"], req["form"], req["line"])
    response = "\n".join([response, "", dim(no_color, "---"), label_style(no_color, f"Answered by: {source_label}")])
    return {
        "response": response,
        "guardrail_no_match": True,
        "guardrail_reason": "missing_line_in_year_docs",
        "requested_year": req["year"],
        "requested_form": req["form"],
        "requested_line": req["line"],
        "num_docs": len(year_form_docs),
        "receipt_metas": [m for _d, m in year_form_docs if m][:5] if year_form_docs else None,
    }


def run_income_year_total_guardrail(
    *,
    collection: Any,
    use_unified: bool,
    silo: str | None,
    subscope_where: dict[str, Any] | None,
    query: str,
    source_label: str,
    no_color: bool,
) -> dict[str, Any] | None:
    """Deterministic income lookup for year-scoped queries (no cross-year, no LLM)."""
    years = query_mentioned_years(query)
    if not years:
        return None
    year = years[0]
    get_kw: dict[str, Any] = {"include": ["documents", "metadatas"]}
    if use_unified and silo:
        get_kw["where"] = {"silo": silo}
    elif subscope_where:
        get_kw["where"] = subscope_where
    try:
        all_rows = collection.get(**get_kw)
    except Exception:
        return None
    docs_all = _flatten_get_list(all_rows.get("documents"))
    metas_all = _flatten_get_list(all_rows.get("metadatas"))
    year_docs: list[tuple[str, dict | None]] = []
    for i, doc in enumerate(docs_all):
        meta = metas_all[i] if i < len(metas_all) else None
        source = ((meta or {}).get("source") or "").lower()
        if year not in source:
            continue
        year_docs.append((str(doc or ""), meta))

    if not year_docs:
        response = _field_lookup_not_indexed_message(year)
        response = "\n".join([response, "", dim(no_color, "---"), label_style(no_color, f"Answered by: {source_label}")])
        return {
            "response": response,
            "guardrail_no_match": True,
            "guardrail_reason": "year_docs_not_indexed",
            "requested_year": year,
            "requested_form": "1040",
            "requested_line": "9",
            "num_docs": 0,
            "receipt_metas": None,
        }

    def _scan_line(line_label: str) -> tuple[str | None, dict | None]:
        for doc, meta in year_docs:
            source = ((meta or {}).get("source") or "").lower()
            if not _source_or_doc_has_form(source, doc, "1040"):
                continue
            val = _extract_exact_line_value(doc, line_label)
            if val:
                return val, meta
        return None, None

    val9, meta9 = _scan_line("9")
    if val9:
        answer = style_answer(f"Form 1040 line 9 ({year}): {val9}", no_color)
        out = [
            answer,
            "",
            dim(no_color, "---"),
            label_style(no_color, f"Answered by: {source_label}"),
        ]
        if meta9:
            out.extend(
                [
                    "",
                    bold(no_color, "Sources:"),
                    format_source("", meta9, None, no_color=no_color),
                ]
            )
        return {
            "response": "\n".join(out),
            "guardrail_no_match": False,
            "guardrail_reason": "income_line9",
            "requested_year": year,
            "requested_form": "1040",
            "requested_line": "9",
            "num_docs": 1,
            "receipt_metas": [meta9] if meta9 else None,
        }

    val11, meta11 = _scan_line("11")
    if val11:
        answer = style_answer(f"Form 1040 line 11 (AGI) ({year}) [fallback from line 9]: {val11}", no_color)
        out = [
            answer,
            "",
            dim(no_color, "---"),
            label_style(no_color, f"Answered by: {source_label}"),
        ]
        if meta11:
            out.extend(
                [
                    "",
                    bold(no_color, "Sources:"),
                    format_source("", meta11, None, no_color=no_color),
                ]
            )
        return {
            "response": "\n".join(out),
            "guardrail_no_match": False,
            "guardrail_reason": "income_line11_fallback",
            "requested_year": year,
            "requested_form": "1040",
            "requested_line": "11",
            "num_docs": 1,
            "receipt_metas": [meta11] if meta11 else None,
        }

    response = (
        f"I can't find a single income field for {year}. "
        "Choose a definition: Total income (1040 line 9) / AGI (1040 line 11) / wages (W-2) / business income."
    )
    response = "\n".join([response, "", dim(no_color, "---"), label_style(no_color, f"Answered by: {source_label}")])
    return {
        "response": response,
        "guardrail_no_match": True,
        "guardrail_reason": "income_no_match",
        "requested_year": year,
        "requested_form": "1040",
        "requested_line": "9",
        "num_docs": len(year_docs),
        "receipt_metas": [m for _d, m in year_docs if m][:5] if year_docs else None,
    }
