"""
Context analysis: query classification helpers, recency scoring, timing detection,
and context block formatting for the LLM prompt.
"""
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from query.formatting import shorten_path

# Timing/performance answer policy: detect speed questions and measurement vs causal intent.
TIMING_PATTERN = re.compile(
    r"ollama_sec|eval_duration_ns|_sec\s*=|duration_ns|\bduration\b", re.IGNORECASE
)

# Doc_type bonus for recency re-rank (authoritative types slightly boosted). Small values so similarity still dominates.
DOC_TYPE_BONUS: dict[str, float] = {
    "transcript": 0.15,
    "audit": 0.15,
    "tax_return": 0.15,
    "syllabus": 0.08,
    "paper": 0.05,
    "homework": 0.05,
    "other": 0.0,
}
RECENCY_WEIGHT = 0.15  # w in final_score = sim_score + w * recency_score (+ doc_type bonus)


def query_implies_recency(query: str) -> bool:
    """True if the query asks for latest/current/recent or a time range (so recency tie-breaker is applied)."""
    q = (query or "").strip().lower()
    return bool(
        re.search(
            r"\b(?:latest|current|now|today|most\s+recent|recently)\b|"
            r"\bthis\s+(?:year|month|semester)\b|\blast\s+(?:year|semester|month)\b|"
            r"\bin\s+20\d{2}\b|\b20\d{2}\b",
            q,
        )
    )


def query_mentioned_years(query: str) -> list[str]:
    """Extract 20XX years mentioned in the query (e.g. 2023, 2024) for path-year boosting."""
    return list(dict.fromkeys(re.findall(r"\b(20\d{2})\b", query or "")))


def query_asks_for_agi(query: str) -> bool:
    """True if the query is asking for AGI / adjusted gross income (prefer tax return docs over W-2/1099)."""
    q = (query or "").strip().lower()
    return bool(re.search(r"\b(?:agi|adjusted\s+gross\s+income)\b", q))


def path_looks_like_tax_return(path: str) -> bool:
    """True if path suggests a main tax return (1040 / Income Tax Return), not W-2/1099."""
    p = (path or "").lower()
    return bool(
        re.search(r"income\s+tax\s+return|tax\s+return\.pdf|1040(?:\s|\.|$)", p)
        or "federal income tax return" in p
        or "state income tax return" in p
    )


def query_implies_speed(query: str) -> bool:
    """True if the query is about speed or performance (fast, slow, latency, etc.)."""
    q = (query or "").strip().lower()
    return bool(
        re.search(
            r"\b(?:fast|slow|quick|latency|performance|how\s+long|timing|duration)\b", q
        )
    )


def query_implies_measurement_intent(query: str) -> bool:
    """True if the user is asking for measured timings (how long, latency, show timings), not causal why."""
    q = (query or "").strip().lower()
    return bool(
        re.search(
            r"\b(?:how\s+long\s+does\s+it\s+take|what'?s?\s+the\s+latency|"
            r"show\s+timings?|timing\s+data|measured\s+time|run\s+time)\b",
            q,
        )
    )


def context_has_timing_patterns(docs: list[str]) -> bool:
    """True if any chunk contains timing-like patterns (ollama_sec, eval_duration_ns, etc.)."""
    combined = " ".join(docs or [])
    return bool(TIMING_PATTERN.search(combined))


def recency_score(mtime: float, half_life_days: float = 365.0) -> float:
    """Decay from 1.0 (recent) to 0 (old). mtime = file mtime (seconds since epoch)."""
    if mtime <= 0:
        return 0.0
    try:
        now = datetime.now(timezone.utc).timestamp()
        age_days = (now - float(mtime)) / 86400.0
        return math.exp(-0.693 * age_days / half_life_days)
    except (ValueError, TypeError, OSError):
        return 0.0


def context_block(doc: str, meta: dict | None, show_silo: bool = False) -> str:
    """Format one chunk as a context block for the LLM prompt."""
    m = meta or {}
    src = m.get("source") or "?"
    short = shorten_path(src)
    line = m.get("line_start")
    page = m.get("page")
    loc = f" (line {line})" if line is not None else f" (page {page})" if page is not None else ""
    mtime = m.get("mtime")
    if mtime is not None:
        try:
            mt = datetime.fromtimestamp(float(mtime), tz=timezone.utc).strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            mt = str(mtime)
    else:
        mt = "?"
    silo_val = m.get("silo") or "?"
    doc_type = m.get("doc_type") or "other"
    header = f"file={short}{loc} mtime={mt} silo={silo_val} doc_type={doc_type}"
    if show_silo and m.get("silo"):
        header = f"[silo: {m.get('silo')}] " + header
    return f"{header}\n{(doc or '')[:1000]}"
