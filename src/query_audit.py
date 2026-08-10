"""Query audit trail — what was actually asked of the index, and what came back.

`usage.log` answers "was llmLibrarian queried this week" for the Argus
dashboard; it deliberately carries no query text. This module answers a
different question: *which* queries ran, scoped to which silo, and which
files the chunks actually came from. That is the record you need after a
session retrieves the wrong company's numbers out of a multi-filing silo,
or truncates mid-answer, and you want to know why without re-running it.

Written by the MCP query tools (best-effort — an audit failure must never
break a retrieval), read back by `pal queries` and the `recent_queries`
MCP tool. JSONL, one record per tool call, size-rotated.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG_BASENAME = "query-audit.jsonl"
MAX_BYTES = 5 * 1024 * 1024
ROTATE_KEEP = 2
MAX_QUERY_CHARS = 500
MAX_SOURCES = 12


def audit_enabled() -> bool:
    """Audit is on unless LLMLIBRARIAN_QUERY_AUDIT is explicitly falsey."""
    return (os.environ.get("LLMLIBRARIAN_QUERY_AUDIT") or "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _pal_home() -> Path:
    return Path(os.environ.get("PAL_HOME", str(Path.home() / ".pal"))).expanduser()


def audit_log_path() -> Path:
    return _pal_home() / "logs" / LOG_BASENAME


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.stat().st_size < MAX_BYTES:
            return
    except OSError:
        return
    for idx in range(ROTATE_KEEP, 0, -1):
        older = path.with_suffix(path.suffix + f".{idx}")
        if idx == ROTATE_KEEP:
            older.unlink(missing_ok=True)
            continue
        newer = path.with_suffix(path.suffix + f".{idx + 1}")
        if older.exists():
            older.replace(newer)
    path.replace(path.with_suffix(path.suffix + ".1"))


def _basename(source: str) -> str:
    return Path(source).name or source


def summarize_chunks(chunks: list[dict]) -> dict:
    """Per-silo and per-source breakdown of what a retrieval returned.

    The source histogram is the point: it makes cross-document bleed inside a
    single silo visible after the fact.
    """
    by_silo: dict[str, int] = {}
    by_source: dict[str, int] = {}
    scores: list[float] = []
    for chunk in chunks or []:
        silo = str(chunk.get("silo") or "") or "?"
        by_silo[silo] = by_silo.get(silo, 0) + 1
        source = str(chunk.get("source") or "") or "?"
        by_source[source] = by_source.get(source, 0) + 1
        try:
            scores.append(float(chunk.get("score") or 0.0))
        except (TypeError, ValueError):
            pass
    ranked = sorted(by_source.items(), key=lambda kv: kv[1], reverse=True)
    return {
        "chunks": len(chunks or []),
        "silos": dict(sorted(by_silo.items(), key=lambda kv: kv[1], reverse=True)),
        "sources": [
            {"file": _basename(src), "path": src, "chunks": n}
            for src, n in ranked[:MAX_SOURCES]
        ],
        "sources_total": len(ranked),
        "top_score": round(max(scores), 4) if scores else None,
    }


def record(
    *,
    tool: str,
    queries: list[str],
    silo: str | None,
    params: dict | None = None,
    chunks: list[dict] | None = None,
    outcome: dict | None = None,
    path: Path | None = None,
) -> None:
    """Append one audit record. Never raises."""
    if not audit_enabled():
        return
    try:
        target = path or audit_log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(target)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool,
            "silo": silo or None,
            "queries": [str(q)[:MAX_QUERY_CHARS] for q in (queries or [])],
            "params": {k: v for k, v in (params or {}).items() if v is not None},
            "result": {**summarize_chunks(chunks or []), **(outcome or {})},
        }
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        return


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def _iter_files(path: Path) -> list[Path]:
    """Newest-last ordering: rotated files first, then the live log."""
    files = [
        path.with_suffix(path.suffix + f".{idx}")
        for idx in range(ROTATE_KEEP, 0, -1)
    ]
    files.append(path)
    return [f for f in files if f.exists()]


def _parse_since(since: str | None) -> datetime | None:
    """Accept `24h`, `7d`, `30m`, or an ISO date/timestamp."""
    if not since:
        return None
    raw = since.strip()
    if raw and raw[-1].lower() in {"m", "h", "d"} and raw[:-1].replace(".", "", 1).isdigit():
        amount = float(raw[:-1])
        unit = raw[-1].lower()
        delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}[unit]
        return datetime.now(timezone.utc) - delta
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _record_ts(entry: dict) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(entry.get("ts") or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_records(
    *,
    limit: int = 20,
    silo: str | None = None,
    contains: str | None = None,
    since: str | None = None,
    tool: str | None = None,
    path: Path | None = None,
) -> list[dict]:
    """Most-recent-last list of audit records matching the filters."""
    target = path or audit_log_path()
    cutoff = _parse_since(since)
    needle = (contains or "").lower().strip()
    matched: list[dict] = []
    for file in _iter_files(target):
        try:
            lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if silo and (entry.get("silo") or "") != silo:
                continue
            if tool and entry.get("tool") != tool:
                continue
            if cutoff:
                ts = _record_ts(entry)
                if ts is None or ts < cutoff:
                    continue
            if needle:
                hay = " ".join(entry.get("queries") or []).lower()
                sources = " ".join(
                    str(s.get("file") or "") for s in (entry.get("result") or {}).get("sources") or []
                ).lower()
                if needle not in hay and needle not in sources and needle not in (entry.get("silo") or "").lower():
                    continue
            matched.append(entry)
    return matched[-limit:] if limit and limit > 0 else matched


def summarize_records(records: list[dict]) -> dict:
    """Roll-up across records: call counts, silo mix, most-hit source files."""
    by_silo: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    by_source: dict[str, int] = {}
    empty = 0
    truncated = 0
    errored = 0
    for entry in records:
        silo = entry.get("silo") or "unscoped"
        by_silo[silo] = by_silo.get(silo, 0) + 1
        tool = entry.get("tool") or "?"
        by_tool[tool] = by_tool.get(tool, 0) + 1
        result = entry.get("result") or {}
        if not result.get("chunks"):
            empty += 1
        if result.get("truncated"):
            truncated += 1
        if result.get("errors"):
            errored += 1
        for src in result.get("sources") or []:
            name = str(src.get("file") or "?")
            by_source[name] = by_source.get(name, 0) + int(src.get("chunks") or 0)
    return {
        "calls": len(records),
        "by_tool": dict(sorted(by_tool.items(), key=lambda kv: kv[1], reverse=True)),
        "by_silo": dict(sorted(by_silo.items(), key=lambda kv: kv[1], reverse=True)),
        "top_sources": [
            {"file": name, "chunks": n}
            for name, n in sorted(by_source.items(), key=lambda kv: kv[1], reverse=True)[:10]
        ],
        "empty_results": empty,
        "truncated_results": truncated,
        "errored_calls": errored,
        "first_ts": (records[0].get("ts") if records else None),
        "last_ts": (records[-1].get("ts") if records else None),
    }
