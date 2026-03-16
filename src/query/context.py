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


def build_file_roster(silo_slug: str, manifest_path) -> str:
    """Build a compact file inventory for a silo from the manifest."""
    import json
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""

    silo_data = manifest.get("silos", {}).get(silo_slug, {})
    files = silo_data.get("files", {})
    if not files:
        return ""

    base = silo_data.get("path", "")
    lines = []
    for path_str, meta in sorted(files.items()):
        rel = path_str.removeprefix(base).lstrip("/\\")
        size = meta.get("size", 0)
        size_str = f"{size // 1024}K" if size < 1_000_000 else f"{size / 1_048_576:.1f}M"
        lines.append(f"  {rel} ({size_str})")

    header = f"[FILE INVENTORY — {silo_slug} — {len(files)} files indexed]\n"
    return header + "\n".join(lines) + "\n"


def count_forms_from_manifest(
    silo_slug: str, manifest_path, year: str | None = None
) -> dict:
    """
    Count files by tax form type from the manifest using filename patterns.
    Returns {"counts": {"1099": [...], ...}, "total_matched": N, "total_files": N, "year": year}.
    """
    import json

    FORM_PATTERNS = [
        ("W-2",      re.compile(r"\bw.?2\b", re.IGNORECASE)),
        ("1099-NEC", re.compile(r"1099.?nec", re.IGNORECASE)),
        ("1099-INT", re.compile(r"1099.?int", re.IGNORECASE)),
        ("1099-DIV", re.compile(r"1099.?div", re.IGNORECASE)),
        ("1099-B",   re.compile(r"1099.?b\b", re.IGNORECASE)),
        ("1099-R",   re.compile(r"1099.?r\b", re.IGNORECASE)),
        ("1099",     re.compile(r"1099",       re.IGNORECASE)),
        ("1098-T",   re.compile(r"1098.?t\b",  re.IGNORECASE)),
        ("1098",     re.compile(r"1098",        re.IGNORECASE)),
        ("1040",     re.compile(r"1040",        re.IGNORECASE)),
    ]

    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

    silo_data = manifest.get("silos", {}).get(silo_slug, {})
    files = silo_data.get("files", {})
    if not files:
        return {}

    if year:
        files = {p: m for p, m in files.items() if year in p}

    counts: dict[str, list[str]] = {}
    matched: set[str] = set()

    for form_type, pattern in FORM_PATTERNS:
        matched_files = []
        for path_str in sorted(files):
            if path_str in matched:
                continue
            fname = path_str.rsplit("/", 1)[-1]
            if pattern.search(fname):
                matched_files.append(fname)
                matched.add(path_str)
        if matched_files:
            counts[form_type] = matched_files

    return {"counts": counts, "total_matched": len(matched), "total_files": len(files), "year": year}


def format_form_count_answer(result: dict, query: str, source_label: str) -> str:
    """Format the deterministic form-count result as a natural-language answer."""
    counts = result.get("counts", {})
    year = result.get("year")
    total = result.get("total_matched", 0)
    total_files = result.get("total_files", 0)

    if not counts:
        year_phrase = f" for {year}" if year else ""
        return f"No tax form files found{year_phrase} matching known form types."

    year_phrase = f" for {year}" if year else " across all indexed years"

    def _plural(form_type: str, n: int) -> str:
        if form_type == "W-2":
            return "W-2s" if n > 1 else "W-2"
        for prefix in ("1099", "1098", "1040"):
            if form_type.startswith(prefix):
                suffix = form_type[len(prefix):]
                return f"{prefix}{suffix}s" if n > 1 else form_type
        return form_type

    # Summary sentence
    parts = [f"{len(fnames)} {_plural(ft, len(fnames))}" for ft, fnames in counts.items()]
    if len(parts) == 1:
        summary = parts[0]
    elif len(parts) == 2:
        summary = f"{parts[0]} and {parts[1]}"
    else:
        summary = ", ".join(parts[:-1]) + f", and {parts[-1]}"

    lines = [f"You have {summary}{year_phrase}."]

    # Per-form detail
    for form_type, fnames in counts.items():
        label = _plural(form_type, len(fnames))
        lines.append(f"\n{label}:")
        for fn in fnames:
            lines.append(f"  • {fn}")

    unmatched = total_files - total
    if unmatched:
        lines.append(f"\n({unmatched} file(s) in the silo not matched to a known form type)")

    return "\n".join(lines)


def context_block(doc: str, meta: dict | None, show_silo: bool = False) -> str:
    """Format one chunk as a context block for the LLM prompt."""
    m = meta or {}
    src = m.get("source") or "?"
    short = shorten_path(src)
    line = m.get("line_start")
    page = m.get("page")
    region = m.get("region_index")
    loc = (
        f" (line {line})"
        if line is not None
        else f" (page {page})"
        if page is not None
        else f" (region {region})"
        if region is not None
        else ""
    )
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
