"""Activity ledger — durable, time-ranged history of how the library changes and is used.

The query audit rotates by size and the watcher logs sample five paths per
batch; neither can answer "what changed this month compared with last month"
honestly. This ledger records explicit events in one SQLite file next to the
index, so an overview can be built from counts rather than log scraping:

- ``file_events`` / ``silo_events`` — diffed from every manifest write
  (``file_registry._update_file_manifest`` is the one choke point every ingest
  path goes through), so ``llmli add``, ``pal pull``, watcher drains, and MCP
  ``update_file`` / ``remove_file`` are all covered.
- ``failures`` — one row per open problem (silo, path, error class); repeated
  failures bump a counter instead of adding rows, and a later successful index
  of the path resolves it.
- ``searches`` / ``search_hits`` — one row per retrieval request and one row
  per *unique file* it surfaced. "Sources found" is all this can claim; it says
  nothing about whether the answer was good.

Writes are best-effort and never raise: a ledger failure must never break an
ingest or a retrieval. ``LLMLIBRARIAN_ACTIVITY=0`` turns the ledger off.

Read with ``summary()`` (Overview aggregates) and ``detail()`` (the rows behind
one number), via ``pal activity`` or directly from SQLite in the macOS app. Both
readers query the ``activity_*_v1`` views, which hold the event semantics once.
Timestamps are epoch seconds (REAL) so both sides can range-query them.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

LEDGER_BASENAME = "llmli_activity.sqlite3"
SCHEMA_VERSION = 2
DEFAULT_RETENTION_DAYS = 180
PRUNE_INTERVAL_SECONDS = 24 * 3600
MAX_QUERY_CHARS = 500

# Origins whose manifest writes re-index existing files rather than change them.
REINDEX_ORIGINS = ("repair", "rehydrate")

WINDOWS = {"today": 1, "7d": 7, "30d": 30}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS file_events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    silo TEXT NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    origin TEXT NOT NULL,
    size INTEGER
);
CREATE INDEX IF NOT EXISTS file_events_ts ON file_events(ts);
CREATE INDEX IF NOT EXISTS file_events_silo_path ON file_events(silo, path);
CREATE TABLE IF NOT EXISTS silo_events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    silo TEXT NOT NULL,
    kind TEXT NOT NULL,
    origin TEXT NOT NULL,
    files INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS silo_events_ts ON silo_events(ts);
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    tool TEXT NOT NULL,
    silo TEXT,
    query_text TEXT,
    unique_files INTEGER NOT NULL DEFAULT 0,
    sources_found INTEGER NOT NULL DEFAULT 0,
    client TEXT NOT NULL DEFAULT 'unknown',
    cached INTEGER NOT NULL DEFAULT 0,
    errored INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS searches_ts ON searches(ts);
CREATE TABLE IF NOT EXISTS search_hits (
    search_id INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    silo TEXT NOT NULL,
    path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS search_hits_search ON search_hits(search_id);
CREATE INDEX IF NOT EXISTS search_hits_silo_path ON search_hits(silo, path);
CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY,
    silo TEXT NOT NULL,
    path TEXT NOT NULL,
    error_class TEXT NOT NULL,
    error TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS failures_open ON failures(silo, path, error_class, resolved_at);
"""

# Read contract shared with the macOS app (ActivityLedger.swift). The rules that
# decide what an event *means* live here, once: re-index origins are not changes,
# buckets are local-time day/hour, a search "touches" its scoped silo and every
# silo it surfaced a file from, failures group by (silo, error class). Readers
# only filter by ts and group. Bump the _vN suffix to change a view's shape.
_VIEWS = """
CREATE VIEW IF NOT EXISTS activity_changes_v1 AS
    SELECT id, ts, silo, path, kind, origin,
           date(ts, 'unixepoch', 'localtime') AS day,
           CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS hour
    FROM file_events WHERE origin NOT IN ('repair', 'rehydrate');
CREATE VIEW IF NOT EXISTS activity_collection_events_v1 AS
    SELECT id, ts, silo, kind, files FROM silo_events WHERE origin NOT IN ('repair', 'rehydrate');
CREATE VIEW IF NOT EXISTS activity_searches_v1 AS
    SELECT id, ts, tool, silo, query_text, unique_files, sources_found, client,
           date(ts, 'unixepoch', 'localtime') AS day,
           CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS hour
    FROM searches;
CREATE VIEW IF NOT EXISTS activity_search_silos_v1 AS
    SELECT id AS search_id, ts, silo FROM searches WHERE silo IS NOT NULL
    UNION
    SELECT h.search_id, s.ts, h.silo FROM search_hits h JOIN searches s ON s.id = h.search_id;
CREATE VIEW IF NOT EXISTS activity_failure_groups_v1 AS
    SELECT silo, error_class, COUNT(*) AS files, SUM(occurrences) AS occurrences,
           MAX(last_seen) AS last_seen,
           (SELECT f2.error FROM failures f2
             WHERE f2.silo = f.silo AND f2.error_class = f.error_class AND f2.resolved_at IS NULL
             ORDER BY f2.last_seen DESC LIMIT 1) AS sample_error
    FROM failures f WHERE resolved_at IS NULL GROUP BY silo, error_class;
"""

_ORIGIN: contextvars.ContextVar[str] = contextvars.ContextVar("llmli_activity_origin", default="index")
_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()


def activity_enabled() -> bool:
    """On unless LLMLIBRARIAN_ACTIVITY is explicitly falsey."""
    return (os.environ.get("LLMLIBRARIAN_ACTIVITY") or "1").strip().lower() not in {"0", "false", "no", "off"}


def retention_days() -> int:
    raw = (os.environ.get("LLMLIBRARIAN_ACTIVITY_RETENTION_DAYS") or "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_RETENTION_DAYS
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def ledger_path(db_path: str | Path) -> Path:
    p = Path(db_path).resolve()
    return (p if p.is_dir() else p.parent) / LEDGER_BASENAME


@contextlib.contextmanager
def origin(name: str) -> Iterator[None]:
    """Tag manifest writes made inside this block (``repair``, ``mcp``, ``add`` …)."""
    token = _ORIGIN.set(name)
    try:
        yield
    finally:
        _ORIGIN.reset(token)


def current_origin() -> str:
    return _ORIGIN.get()


def _connect(db_path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = ledger_path(db_path)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    if not readonly:
        key = str(path)
        with _INIT_LOCK:
            if key not in _INITIALIZED or not path.exists():
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                conn.executescript(_VIEWS)
                now = str(time.time())
                for meta_key in ("files_since", "searches_since", "created_at"):
                    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (meta_key, now))
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                _INITIALIZED.add(key)
    return conn


@contextlib.contextmanager
def _writer(db_path: str | Path) -> Iterator[sqlite3.Connection | None]:
    """Yield a connection inside BEGIN IMMEDIATE, or None when disabled/broken.

    Swallows every error: callers are ingest and retrieval paths.
    """
    if not activity_enabled():
        yield None
        return
    try:
        conn = _connect(db_path)
    except Exception:
        yield None
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
        _maybe_prune(conn)
    except Exception:
        with contextlib.suppress(Exception):
            conn.execute("ROLLBACK")
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _maybe_prune(conn: sqlite3.Connection, *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    row = conn.execute("SELECT value FROM meta WHERE key='last_prune'").fetchone()
    if row and now - float(row[0]) < PRUNE_INTERVAL_SECONDS:
        return
    prune(conn, now=now)


def prune(conn: sqlite3.Connection, *, now: float | None = None) -> None:
    """Drop events older than the retention window, then move ``*_since`` forward."""
    now = time.time() if now is None else now
    cutoff = now - retention_days() * 86400
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM file_events WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM silo_events WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM searches WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM failures WHERE resolved_at IS NOT NULL AND resolved_at < ?", (cutoff,))
        for key in ("files_since", "searches_since"):
            conn.execute(
                "UPDATE meta SET value = ? WHERE key = ? AND CAST(value AS REAL) < ?",
                (str(cutoff), key, cutoff),
            )
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_prune', ?)", (str(now),))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# Write side: manifest diffs
# ---------------------------------------------------------------------------


FileSig = tuple[str, float, int]


def manifest_signature(manifest: dict) -> dict[str, dict[str, FileSig]]:
    """``{silo: {path: (hash, mtime, size)}}`` — the part of a manifest a diff needs."""
    out: dict[str, dict[str, FileSig]] = {}
    for slug, entry in (manifest.get("silos") or {}).items():
        files = (entry or {}).get("files") if isinstance(entry, dict) else None
        sigs: dict[str, FileSig] = {}
        if isinstance(files, dict):
            for path, info in files.items():
                if not isinstance(info, dict):
                    continue
                try:
                    sigs[str(path)] = (
                        str(info.get("hash") or ""),
                        float(info.get("mtime") or 0.0),
                        int(info.get("size") or 0),
                    )
                except (TypeError, ValueError):
                    continue
        out[str(slug)] = sigs
    return out


def _changed(old: FileSig, new: FileSig) -> bool:
    if old[0] or new[0]:
        return old[0] != new[0]
    # Zips carry no content hash; fall back to mtime/size.
    return old[1] != new[1] or old[2] != new[2]


def diff_signatures(
    before: dict[str, dict[str, FileSig]],
    after: dict[str, dict[str, FileSig]],
) -> tuple[list[tuple[str, str, str, int]], list[tuple[str, str, int]]]:
    """Return (file_changes, silo_changes).

    file_changes: (silo, path, kind, size). silo_changes: (silo, kind, files).
    A silo that appears or vanishes is one silo event, not N file events —
    indexing a new folder is "a collection was added", not 2,000 edits.
    """
    files: list[tuple[str, str, str, int]] = []
    silos: list[tuple[str, str, int]] = []
    for slug in sorted(set(before) | set(after)):
        old = before.get(slug)
        new = after.get(slug)
        if old is None and new is not None:
            silos.append((slug, "silo_added", len(new)))
            continue
        if new is None and old is not None:
            silos.append((slug, "silo_removed", len(old)))
            continue
        assert old is not None and new is not None
        for path, sig in new.items():
            prev = old.get(path)
            if prev is None:
                files.append((slug, path, "added", sig[2]))
            elif _changed(prev, sig):
                files.append((slug, path, "updated", sig[2]))
        for path, sig in old.items():
            if path not in new:
                files.append((slug, path, "removed", sig[2]))
    return files, silos


def record_manifest_change(
    db_path: str | Path,
    before: dict[str, dict[str, FileSig]],
    after: dict[str, dict[str, FileSig]],
    *,
    now: float | None = None,
) -> None:
    """Record what one manifest write changed. Never raises."""
    if not activity_enabled():
        return
    try:
        files, silos = diff_signatures(before, after)
    except Exception:
        return
    if not files and not silos:
        return
    ts = time.time() if now is None else now
    org = current_origin()
    with _writer(db_path) as conn:
        if conn is None:
            return
        conn.executemany(
            "INSERT INTO file_events(ts, silo, path, kind, origin, size) VALUES (?, ?, ?, ?, ?, ?)",
            [(ts, s, p, k, org, size) for s, p, k, size in files],
        )
        conn.executemany(
            "INSERT INTO silo_events(ts, silo, kind, origin, files) VALUES (?, ?, ?, ?, ?)",
            [(ts, s, k, org, n) for s, k, n in silos],
        )
        indexed = [(ts, s, p) for s, p, k, _ in files if k in ("added", "updated")]
        conn.executemany(
            "UPDATE failures SET resolved_at = ? WHERE silo = ? AND path = ? AND resolved_at IS NULL",
            indexed,
        )
        for slug, kind, _ in silos:
            # A repair drops and rebuilds the silo; its failures are still open.
            if kind == "silo_removed" and org not in REINDEX_ORIGINS:
                conn.execute(
                    "UPDATE failures SET resolved_at = ? WHERE silo = ? AND resolved_at IS NULL",
                    (ts, slug),
                )


# ---------------------------------------------------------------------------
# Write side: failures
# ---------------------------------------------------------------------------

_ERROR_TYPE = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:Error|Exception|Timeout|Warning))\b")
_ERROR_PHRASES = (
    ("timeout", ("timed out", "timeout")),
    ("permission", ("permission denied", "operation not permitted", "not permitted")),
    ("unsupported", ("unsupported", "not supported", "unknown file type")),
    ("encrypted", ("encrypted", "password")),
    ("too_large", ("too large", "exceeds", "max_file", "size limit")),
    ("no_text", ("no text", "empty", "no extractable")),
    ("busy", ("busy", "lock", "locked")),
    ("not_found", ("no such file", "not found", "does not exist")),
)


def error_class(error: str) -> str:
    """Short, stable grouping key for an error message."""
    text = str(error or "")
    lower = text.lower()
    for name, needles in _ERROR_PHRASES:
        if any(n in lower for n in needles):
            return name
    m = _ERROR_TYPE.search(text)
    if m:
        return m.group(1)
    return "other"


def silo_for_path(path: str, silo_roots: dict[str, str]) -> str | None:
    """Longest silo root that contains ``path`` (shared with list_silos failure matching)."""
    best: tuple[int, str] | None = None
    for slug, root in silo_roots.items():
        if not root:
            continue
        r = root.rstrip("/")
        if path == r or path.startswith(r + "/"):
            if best is None or len(r) > best[0]:
                best = (len(r), slug)
    return best[1] if best else None


def _silo_roots(db_path: str | Path) -> dict[str, str]:
    roots: dict[str, str] = {}
    try:
        from file_registry import _read_file_manifest

        for slug, entry in (_read_file_manifest(db_path).get("silos") or {}).items():
            if isinstance(entry, dict) and entry.get("path"):
                roots[str(slug)] = str(entry["path"])
    except Exception:
        pass
    try:
        from state import list_silos

        for row in list_silos(db_path):
            slug = str(row.get("slug") or "")
            if slug and row.get("path") and slug not in roots:
                roots[slug] = str(row["path"])
    except Exception:
        pass
    return roots


def record_failures(db_path: str | Path, entries: list[dict], *, now: float | None = None) -> None:
    """Upsert open failures by (silo, path, error_class). Never raises."""
    if not activity_enabled() or not entries:
        return
    ts = time.time() if now is None else now
    try:
        roots = _silo_roots(db_path)
    except Exception:
        roots = {}
    with _writer(db_path) as conn:
        if conn is None:
            return
        for row in entries:
            path = str((row or {}).get("path") or "")
            if not path:
                continue
            err = str((row or {}).get("error") or "")[:1000]
            silo = silo_for_path(path, roots) or "unknown"
            cls = error_class(err)
            hit = conn.execute(
                "SELECT id FROM failures WHERE silo=? AND path=? AND error_class=? AND resolved_at IS NULL",
                (silo, path, cls),
            ).fetchone()
            if hit:
                conn.execute(
                    "UPDATE failures SET last_seen=?, occurrences=occurrences+1, error=? WHERE id=?",
                    (ts, err, hit[0]),
                )
            else:
                conn.execute(
                    "INSERT INTO failures(silo, path, error_class, error, first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (silo, path, cls, err, ts, ts),
                )


# ---------------------------------------------------------------------------
# Write side: searches
# ---------------------------------------------------------------------------

NON_SEARCH_TOOLS = frozenset({"read_document"})


def _unique_sources(chunks: list[dict] | None) -> list[tuple[str, str]]:
    seen: dict[tuple[str, str], None] = {}
    for chunk in chunks or []:
        source = str((chunk or {}).get("source") or "")
        if not source:
            continue
        seen.setdefault((str(chunk.get("silo") or "") or "?", source), None)
    return list(seen)


def record_search(
    db_path: str | Path,
    *,
    tool: str,
    queries: list[str],
    silo: str | None,
    chunks: list[dict] | None = None,
    outcome: dict | None = None,
    client: str | None = None,
    keep_text: bool = True,
    now: float | None = None,
) -> None:
    """Record one retrieval request and the unique files it surfaced. Never raises."""
    if not activity_enabled() or tool in NON_SEARCH_TOOLS:
        return
    try:
        hits = _unique_sources(chunks)
        out = outcome or {}
        text = " | ".join(str(q) for q in (queries or []) if q)[:MAX_QUERY_CHARS] if keep_text else None
        row = (
            time.time() if now is None else now,
            tool,
            silo or None,
            text or None,
            len(hits),
            1 if hits else 0,
            client or "unknown",
            1 if out.get("cached") else 0,
            1 if out.get("errors") else 0,
            1 if out.get("truncated") else 0,
        )
    except Exception:
        return
    with _writer(db_path) as conn:
        if conn is None:
            return
        cur = conn.execute(
            "INSERT INTO searches(ts, tool, silo, query_text, unique_files, sources_found, client,"
            " cached, errored, truncated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        conn.executemany(
            "INSERT INTO search_hits(search_id, silo, path) VALUES (?, ?, ?)",
            [(cur.lastrowid, s, p) for s, p in hits],
        )


def backfill_from_audit(db_path: str | Path, *, audit_path: Path | None = None) -> dict:
    """Import query-audit.jsonl history into ``searches``. Idempotent.

    The audit keeps only the top 12 sources per record, so imported searches can
    under-count ``unique_files`` for very broad queries; ``sources_found`` is exact.
    """
    import query_audit

    if not activity_enabled():
        return {"status": "disabled", "imported": 0}
    records = query_audit.read_records(limit=0, path=audit_path)
    imported = 0
    skipped = 0
    earliest: float | None = None
    with _writer(db_path) as conn:
        if conn is None:
            return {"status": "error", "imported": 0}
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        # Live recording covers everything from the ledger's creation on, and a
        # previous backfill covers up to its high-water mark. Same-second repeats
        # are real requests, so there is no content-based dedupe.
        live_from = float(meta.get("created_at") or time.time())
        done_through = float(meta.get("backfill_through") or "-inf")
        newest: float | None = None
        for entry in records:
            tool = str(entry.get("tool") or "")
            if not tool or tool in NON_SEARCH_TOOLS:
                continue
            ts_dt = query_audit._record_ts(entry)
            if ts_dt is None:
                continue
            ts = ts_dt.timestamp()
            if ts <= done_through or ts >= live_from:
                skipped += 1
                continue
            newest = ts if newest is None else max(newest, ts)
            text = " | ".join(str(q) for q in entry.get("queries") or [])[:MAX_QUERY_CHARS] or None
            result = entry.get("result") or {}
            sources = result.get("sources") or []
            # The audit has no per-source silo; attribute only when unambiguous.
            result_silos = [k for k in (result.get("silos") or {}) if k and k != "?"]
            hit_silo = entry.get("silo") or (result_silos[0] if len(result_silos) == 1 else "?")
            hits = list(
                dict.fromkeys(
                    (hit_silo, str(s.get("path") or s.get("file") or ""))
                    for s in sources
                    if s.get("path") or s.get("file")
                )
            )
            unique = max(int(result.get("sources_total") or 0), len(hits))
            cur = conn.execute(
                "INSERT INTO searches(ts, tool, silo, query_text, unique_files, sources_found, client,"
                " cached, errored, truncated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    tool,
                    entry.get("silo") or None,
                    text,
                    unique,
                    1 if (result.get("chunks") or unique) else 0,
                    query_audit.client_label(entry),
                    1 if result.get("cached") else 0,
                    1 if result.get("errors") else 0,
                    1 if result.get("truncated") else 0,
                ),
            )
            conn.executemany(
                "INSERT INTO search_hits(search_id, silo, path) VALUES (?, ?, ?)",
                [(cur.lastrowid, s, p) for s, p in hits],
            )
            imported += 1
            earliest = ts if earliest is None else min(earliest, ts)
        if earliest is not None:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='searches_since' AND CAST(value AS REAL) > ?",
                (str(earliest), earliest),
            )
        if newest is not None:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('backfill_through', ?)",
                (str(max(newest, done_through)),),
            )
    return {"status": "ok", "imported": imported, "skipped_already_covered": skipped,
            "searches_since": _iso(earliest) if earliest else None}


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day).astimezone()


def period_bounds(window: str, now: datetime) -> dict[str, Any]:
    """Current and previous period for a window, in local time.

    Periods start at local midnight and end at ``now``; the previous period is the
    same span shifted back by the window length, so "today so far" compares with
    "yesterday up to this time", and 7 days with the 7 days before.
    """
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {sorted(WINDOWS)}")
    days = WINDOWS[window]
    now = now.astimezone()
    start = _midnight(now.date() - timedelta(days=days - 1))
    prev_start = _midnight(start.date() - timedelta(days=days))
    prev_end = now - timedelta(days=days)
    if window == "today":
        buckets = [start + timedelta(hours=h) for h in range(now.hour + 1)]
    else:
        buckets = [_midnight(start.date() + timedelta(days=i)) for i in range(days)]
    return {
        "days": days,
        "start": start,
        "end": now,
        "prev_start": prev_start,
        "prev_end": prev_end,
        "buckets": buckets,
        "bucket_unit": "hour" if window == "today" else "day",
    }


def bucket_key(b: datetime, window: str) -> tuple[str, int | None]:
    """(local day, hour) for Today's hourly buckets, (local day, None) otherwise."""
    return (b.strftime("%Y-%m-%d"), b.hour if window == "today" else None)


def _norm_query(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _private_slugs(db_path: str | Path) -> set[str] | None:
    """Private slugs, or None when the registry can't be read (treat all as private)."""
    try:
        from state import private_silo_slugs

        return set(private_silo_slugs(db_path))
    except Exception:
        return None


def _registry_rows(db_path: str | Path) -> list[dict]:
    try:
        from state import list_silos

        return list(list_silos(db_path))
    except Exception:
        return []


def _manifest_counts(db_path: str | Path) -> dict[str, int]:
    try:
        from file_registry import _read_file_manifest

        return {
            str(slug): len((entry or {}).get("files") or {})
            for slug, entry in (_read_file_manifest(db_path).get("silos") or {}).items()
            if isinstance(entry, dict)
        }
    except Exception:
        return {}


def _phrase(window: str) -> tuple[str, str]:
    return {
        "today": ("today", "the same time yesterday"),
        "7d": ("this week", "the previous week"),
        "30d": ("in the last 30 days", "the 30 days before"),
    }[window]


def _names_list(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _plural(n: int, noun: str, plural: str | None = None) -> str:
    return f"{n} {noun if n == 1 else (plural or noun + 's')}"


# Queries both readers run against the views. ActivityLedger.swift carries the
# same strings; keep them identical so the shared fixture tests the same SQL.
SUMMARY_SQL = {
    "change_buckets": "SELECT day, hour, kind, COUNT(*) FROM activity_changes_v1"
                      " WHERE ts >= ? AND ts <= ? GROUP BY day, hour, kind",
    "change_silos": "SELECT silo, COUNT(*) FROM activity_changes_v1 WHERE ts >= ? AND ts <= ? GROUP BY silo",
    "change_count": "SELECT COUNT(*) FROM activity_changes_v1 WHERE ts >= ? AND ts <= ?",
    "search_buckets": "SELECT day, hour, COUNT(*), SUM(sources_found = 0) FROM activity_searches_v1"
                      " WHERE ts >= ? AND ts <= ? GROUP BY day, hour",
    "search_count": "SELECT COUNT(*), IFNULL(SUM(sources_found = 0), 0) FROM activity_searches_v1"
                    " WHERE ts >= ? AND ts <= ?",
    "search_silos": "SELECT silo, COUNT(DISTINCT search_id) FROM activity_search_silos_v1"
                    " WHERE ts >= ? AND ts <= ? GROUP BY silo",
    "collection_events": "SELECT ts, silo, kind, files FROM activity_collection_events_v1"
                         " WHERE ts >= ? AND ts <= ? ORDER BY ts DESC",
    "last_change": "SELECT silo, MAX(ts) FROM activity_changes_v1 GROUP BY silo",
    "last_search": "SELECT silo, MAX(ts) FROM activity_search_silos_v1 GROUP BY silo",
    "failure_groups": "SELECT silo, error_class, files, occurrences, last_seen, sample_error"
                      " FROM activity_failure_groups_v1",
}


def _open_for_read(db_path: str | Path) -> sqlite3.Connection | None:
    """Connection with the current views, or None when no ledger exists yet.

    Opens through the writer path so an older ledger gains the views; it never
    creates a ledger that is not there.
    """
    if not ledger_path(db_path).exists():
        return None
    return _connect(db_path)


def _meta(conn: sqlite3.Connection | None) -> dict[str, float]:
    if conn is None:
        return {}
    out: dict[str, float] = {}
    for k, v in conn.execute("SELECT key, value FROM meta"):
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def summary(
    db_path: str | Path,
    window: str = "7d",
    *,
    now: datetime | None = None,
    include_private: bool = False,
    check_staleness: bool = False,
) -> dict[str, Any]:
    """Aggregates for the Overview: totals with deltas, buckets, one row per collection.

    O(collections + buckets) regardless of event volume. Lists of files and
    queries are ``detail()``'s job. Private silos keep their counts; only the
    failure sample error is withheld unless ``include_private``.
    """
    now_dt = (now or datetime.now()).astimezone()
    b = period_bounds(window, now_dt)
    cur = (b["start"].timestamp(), b["end"].timestamp())
    prev = (b["prev_start"].timestamp(), b["prev_end"].timestamp())

    registry = _registry_rows(db_path)
    names = {str(r.get("slug")): str(r.get("display_name") or r.get("slug")) for r in registry if r.get("slug")}
    private = _private_slugs(db_path)

    def label(slug: str | None) -> str:
        return names.get(slug or "", slug or "All collections")

    def is_private(slug: str) -> bool:
        return private is None or slug in private

    conn = _open_for_read(db_path)

    def q(name: str, args: tuple = ()) -> list[tuple]:
        return conn.execute(SUMMARY_SQL[name], args).fetchall() if conn is not None else []

    try:
        meta = _meta(conn)
        keys = [bucket_key(x, window) for x in b["buckets"]]
        index = {k: i for i, k in enumerate(keys)}
        buckets = [
            {"start": _iso(x.timestamp()), "added": 0, "updated": 0, "removed": 0, "searches": 0, "no_source": 0}
            for x in b["buckets"]
        ]
        totals = {"added": 0, "updated": 0, "removed": 0}
        for day, hour, kind, n in q("change_buckets", cur):
            totals[kind] = totals.get(kind, 0) + n
            i = index.get((day, hour if window == "today" else None))
            if i is not None:
                buckets[i][kind] += n
        searches = no_source = 0
        for day, hour, n, miss in q("search_buckets", cur):
            searches += n
            no_source += miss or 0
            i = index.get((day, hour if window == "today" else None))
            if i is not None:
                buckets[i]["searches"] += n
                buckets[i]["no_source"] += miss or 0
        prev_changes = (q("change_count", prev) or [(0,)])[0][0]
        prev_searches, prev_no_source = (q("search_count", prev) or [(0, 0)])[0]
        change_by_silo = dict(q("change_silos", cur))
        search_by_silo = dict(q("search_silos", cur))
        last_change = dict(q("last_change"))
        last_search = dict(q("last_search"))
        events = q("collection_events", cur)
        fail_rows = q("failure_groups")
    finally:
        if conn is not None:
            conn.close()

    files_since = meta.get("files_since")
    searches_since = meta.get("searches_since")
    full_files = files_since is not None and files_since <= prev[0]
    full_searches = searches_since is not None and searches_since <= prev[0]

    open_failures: dict[str, int] = {}
    failures = []
    for f_silo, cls, files, occ, last, sample in fail_rows:
        open_failures[f_silo] = open_failures.get(f_silo, 0) + files
        failures.append({
            "silo": f_silo, "name": label(f_silo), "error_class": cls, "files": files, "occurrences": occ,
            "last_seen": _iso(last), "sample_error": None if (is_private(f_silo) and not include_private) else sample,
        })
    failures.sort(key=lambda g: (-g["files"], g["name"], g["error_class"]))

    staleness: dict[str, dict] = {}
    if check_staleness:
        try:
            from operations import op_list_silos

            staleness = {str(r.get("slug")): r for r in op_list_silos(str(db_path), check_staleness=True)}
        except Exception:
            staleness = {}

    counts = _manifest_counts(db_path)
    # Registered collections only: activity from removed silos or unattributed
    # backfilled hits ("?") still counts in the totals but has no row to land on.
    collections = []
    for slug in names:
        lc, ls = last_change.get(slug), last_search.get(slug)
        row = {
            "silo": slug,
            "name": label(slug),
            "private": is_private(slug),
            "files": counts.get(slug, 0),
            "changes": change_by_silo.get(slug, 0),
            "searches": search_by_silo.get(slug, 0),
            "last_change": _iso(lc),
            "last_search": _iso(ls),
            "last_activity": _iso(max(x for x in (lc, ls) if x is not None)) if (lc or ls) else None,
            "open_failures": open_failures.get(slug, 0),
        }
        if slug in staleness:
            row["is_stale"] = staleness[slug].get("is_stale")
            row["stale_file_count"] = staleness[slug].get("stale_file_count")
        collections.append(row)
    collections.sort(key=lambda r: (-(r["changes"] + r["searches"]), r["name"].lower()))

    collection_events = [
        {"silo": s, "name": label(s), "kind": k, "files": n, "ts": _iso(ts)} for ts, s, k, n in events
    ]

    # --- headline + sentences (the CLI's voice; the app shows the headline) ---
    span, compared = _phrase(window)
    busiest = collections[0] if collections and collections[0]["changes"] + collections[0]["searches"] > 0 else None
    headline = f"{busiest['name']} was the busiest collection {span}." if busiest else f"No library activity {span}."
    n_changes = sum(totals.values())
    sentences: list[str] = []
    changed = sorted((r for r in collections if r["changes"]), key=lambda r: (-r["changes"], r["name"].lower()))
    if n_changes:
        if len(changed) == 1:
            sentences.append(f"All {_plural(n_changes, 'document change')} were in {changed[0]['name']}.")
        else:
            sentences.append(f"Most document changes were in {_names_list([r['name'] for r in changed[:2]])}.")
    elif not full_files and files_since and files_since > cur[0]:
        sentences.append(f"No document changes recorded since history began on {_iso(files_since)[:10]}.")
    else:
        sentences.append("No documents changed.")
    for ev in collection_events:
        if ev["kind"] == "silo_added":
            sentences.append(f"{ev['name']} was added with {_plural(ev['files'], 'file')}.")
    if full_searches:
        if searches > prev_searches:
            line = f"Searches increased to {searches} from {prev_searches} compared with {compared}"
        elif searches < prev_searches:
            line = f"Searches decreased to {searches} from {prev_searches} compared with {compared}"
        else:
            line = f"Searches held steady at {searches} compared with {compared}"
    else:
        line = f"{_plural(searches, 'search', 'searches').capitalize()} {span}"
    line += f"; {no_source} returned no sources." if no_source else "."
    sentences.append(line)
    if failures:
        nf = sum(g["files"] for g in failures)
        sentences.append(f"{_plural(nf, 'file')} could not be indexed.")

    return {
        "window": window,
        "generated_at": _iso(now_dt.timestamp()),
        "period": {"start": _iso(cur[0]), "end": _iso(cur[1]), "bucket_unit": b["bucket_unit"]},
        "previous_period": {"start": _iso(prev[0]), "end": _iso(prev[1])},
        "history": {
            "files_since": _iso(files_since),
            "searches_since": _iso(searches_since),
            "full_file_comparison": bool(full_files),
            "full_search_comparison": bool(full_searches),
        },
        "headline": headline,
        "sentences": sentences,
        "totals": {
            **totals,
            "changes": n_changes,
            "previous_changes": prev_changes if full_files else None,
            "searches": searches,
            "previous_searches": prev_searches if full_searches else None,
            "no_source": no_source,
            "previous_no_source": (prev_no_source or 0) if full_searches else None,
        },
        "buckets": buckets,
        "collections": collections,
        "collection_events": collection_events,
        "failures": failures,
        "ledger_path": str(ledger_path(db_path)),
        "enabled": activity_enabled(),
    }


DETAIL_KINDS = ("changes", "searches", "documents", "clients", "failures")


def detail(
    db_path: str | Path,
    kind: str,
    window: str = "7d",
    *,
    silo: str | None = None,
    no_source_only: bool = False,
    include_private: bool = False,
    limit: int = 50,
    offset: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rows behind one Overview number, filtered and paged.

    Private silos' paths, query text and errors are withheld unless the caller
    names the silo or passes ``include_private`` (naming is consent).
    """
    if kind not in DETAIL_KINDS:
        raise ValueError(f"kind must be one of {', '.join(DETAIL_KINDS)}")
    b = period_bounds(window, (now or datetime.now()).astimezone())
    span = (b["start"].timestamp(), b["end"].timestamp())
    registry = _registry_rows(db_path)
    names = {str(r.get("slug")): str(r.get("display_name") or r.get("slug")) for r in registry if r.get("slug")}
    private = _private_slugs(db_path)

    def hidden(slug: str | None) -> bool:
        if not slug or include_private or (silo and slug == silo):
            return False
        return private is None or slug in private

    def label(slug: str | None) -> str:
        return names.get(slug or "", slug or "All collections")

    out: dict[str, Any] = {"kind": kind, "window": window, "silo": silo, "rows": [], "total": 0,
                           "limit": limit, "offset": offset}
    conn = _open_for_read(db_path)
    if conn is None:
        return out
    try:
        if kind == "changes":
            where = " WHERE ts >= ? AND ts <= ?" + (" AND silo = ?" if silo else "")
            args = (*span, *((silo,) if silo else ()))
            out["total"] = conn.execute("SELECT COUNT(*) FROM activity_changes_v1" + where, args).fetchone()[0]
            out["rows"] = [
                {"ts": _iso(ts), "silo": s, "name": label(s), "kind": k, "path": None if hidden(s) else p}
                for ts, s, p, k in conn.execute(
                    "SELECT ts, silo, path, kind FROM activity_changes_v1" + where
                    + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
            ]
        elif kind == "searches":
            where = " WHERE ts >= ? AND ts <= ?"
            args: tuple = span
            if silo:
                where += " AND id IN (SELECT search_id FROM activity_search_silos_v1 WHERE silo = ?)"
                args = (*args, silo)
            if no_source_only:
                where += " AND sources_found = 0"
            out["total"] = conn.execute("SELECT COUNT(*) FROM activity_searches_v1" + where, args).fetchone()[0]
            rows = conn.execute(
                "SELECT id, ts, silo, query_text, sources_found, unique_files, client FROM activity_searches_v1"
                + where + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            hits: dict[int, list[tuple[str, str]]] = {}
            ids = [r[0] for r in rows]
            if ids:
                for sid, h_silo, h_path in conn.execute(
                    f"SELECT search_id, silo, path FROM search_hits WHERE search_id IN ({','.join('?' * len(ids))})"
                    " ORDER BY rowid", ids):
                    hits.setdefault(sid, []).append((h_silo, h_path))
            out["rows"] = [
                {
                    "id": sid, "ts": _iso(ts), "silo": s, "name": label(s),
                    "query": None if hidden(s) else text,
                    "sources_found": bool(found), "unique_files": n, "client": client,
                    "files": [{"silo": hs, "path": hp} for hs, hp in hits.get(sid, []) if not hidden(hs)],
                }
                for sid, ts, s, text, found, n, client in rows
            ]
        elif kind == "documents":
            where = " WHERE s.ts >= ? AND s.ts <= ?" + (" AND h.silo = ?" if silo else "")
            args = (*span, *((silo,) if silo else ()))
            rows = conn.execute(
                "SELECT h.silo, h.path, COUNT(DISTINCT h.search_id) AS n FROM search_hits h"
                " JOIN searches s ON s.id = h.search_id" + where
                + " GROUP BY h.silo, h.path ORDER BY n DESC, h.path", args).fetchall()
            visible = [(s, p, n) for s, p, n in rows if not hidden(s)]
            out["total"] = len(visible)
            out["rows"] = [{"silo": s, "name": label(s), "path": p, "file": Path(p).name, "searches": n}
                           for s, p, n in visible[offset: offset + limit]]
        elif kind == "clients":
            rows = conn.execute(
                "SELECT client, COUNT(*) FROM activity_searches_v1 WHERE ts >= ? AND ts <= ?"
                " GROUP BY client ORDER BY COUNT(*) DESC, client", span).fetchall()
            out["total"] = len(rows)
            out["rows"] = [{"client": c, "searches": n} for c, n in rows[offset: offset + limit]]
        elif kind == "failures":
            where = " WHERE resolved_at IS NULL" + (" AND silo = ?" if silo else "")
            args = (silo,) if silo else ()
            out["total"] = conn.execute("SELECT COUNT(*) FROM failures" + where, args).fetchone()[0]
            out["rows"] = [
                {"silo": s, "name": label(s), "error_class": cls, "occurrences": occ,
                 "first_seen": _iso(first), "last_seen": _iso(last),
                 "path": None if hidden(s) else p, "error": None if hidden(s) else err}
                for s, p, cls, err, first, last, occ in conn.execute(
                    "SELECT silo, path, error_class, error, first_seen, last_seen, occurrences FROM failures"
                    + where + " ORDER BY last_seen DESC, id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
            ]
    finally:
        conn.close()
    return out


def format_summary(data: dict) -> str:
    """Human text for ``pal activity``: headline, three numbers, collections, problems."""
    t = data["totals"]
    hist = data["history"]
    lines = [data["headline"], *(f"  {s}" for s in data["sentences"])]
    for flag, key, what in (
        ("full_file_comparison", "files_since", "Document-change"),
        ("full_search_comparison", "searches_since", "Search"),
    ):
        if not hist[flag] and hist.get(key):
            lines.append(f"  {what} history available since {hist[key][:10]}.")

    def vs(prev: int | None) -> str:
        return f" (prev {prev})" if prev is not None else ""

    lines += [
        "",
        f"Documents changed  {t['changes']}{vs(t['previous_changes'])}   +{t['added']} ~{t['updated']} -{t['removed']}",
        f"Searches           {t['searches']}{vs(t['previous_searches'])}",
        f"Found no sources   {t['no_source']}{vs(t['previous_no_source'])}",
        "",
        f"{'Collection':<28} {'changed':>8} {'searches':>9}  last activity",
    ]
    for r in data["collections"]:
        lock = " [private]" if r["private"] else ""
        fail = f"  {r['open_failures']} failing" if r["open_failures"] else ""
        lines.append(f"{r['name'][:28]:<28} {r['changes']:>8} {r['searches']:>9}  "
                     f"{(r['last_activity'] or 'never')[:10]}{fail}{lock}")
    if data["failures"]:
        lines += ["", "Needs action"]
        for g in data["failures"]:
            lines.append(f"  {g['name']}: {_plural(g['files'], 'file')} failing ({g['error_class']})")
    return "\n".join(lines)


def format_detail(data: dict) -> str:
    """Human text for ``pal activity --detail KIND``."""
    rows = data["rows"]
    head = f"{data['kind']} — {len(rows)} of {data['total']}"
    lines = [head]
    for r in rows:
        if data["kind"] == "changes":
            lines.append(f"  {r['ts'][:16]}  {r['kind']:<7}  {r['name']}: {r['path'] or '(private)'}")
        elif data["kind"] == "searches":
            mark = " " if r["sources_found"] else "∅"
            lines.append(f"  {r['ts'][:16]} {mark} {r['client']:<14} {r['name']}: {r['query'] or '(private or not kept)'}")
        elif data["kind"] == "documents":
            lines.append(f"  {r['searches']:>4}  {r['file']}  ({r['name']})")
        elif data["kind"] == "clients":
            lines.append(f"  {r['searches']:>4}  {r['client']}")
        elif data["kind"] == "failures":
            lines.append(f"  {r['last_seen'][:16]}  {r['error_class']:<12} {r['name']}: {r['path'] or '(private)'}")
    return "\n".join(lines)

def to_json(data: dict) -> str:
    return json.dumps(data, indent=2)
