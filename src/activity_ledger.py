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

Read with ``overview()`` (``pal activity``) or directly from SQLite (the macOS
app). Timestamps are epoch seconds (REAL) so both sides can range-query them.
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
SCHEMA_VERSION = 1
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
                now = str(time.time())
                for meta_key in ("files_since", "searches_since", "created_at"):
                    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (meta_key, now))
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
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


def _bucket_index(ts: float, buckets: list[datetime]) -> int | None:
    if not buckets or ts < buckets[0].timestamp():
        return None
    idx = 0
    for i, b in enumerate(buckets):
        if ts >= b.timestamp():
            idx = i
        else:
            break
    return idx


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


def overview(
    db_path: str | Path,
    window: str = "7d",
    *,
    now: datetime | None = None,
    silo: str | None = None,
    include_private: bool = False,
    check_staleness: bool = False,
    detail_limit: int = 20,
) -> dict[str, Any]:
    """Counts and comparisons for the Overview.

    Private silos keep their counts but lose paths and query text unless the
    caller names that silo (``silo=``) or passes ``include_private`` — the same
    naming-is-consent rule retrieval follows.
    """
    now_dt = (now or datetime.now()).astimezone()
    bounds = period_bounds(window, now_dt)
    start, end = bounds["start"].timestamp(), bounds["end"].timestamp()
    pstart, pend = bounds["prev_start"].timestamp(), bounds["prev_end"].timestamp()
    buckets: list[datetime] = bounds["buckets"]

    registry = _registry_rows(db_path)
    names = {str(r.get("slug")): str(r.get("display_name") or r.get("slug")) for r in registry if r.get("slug")}
    private = _private_slugs(db_path)

    def redacted(slug: str | None) -> bool:
        if not slug or include_private or (silo and slug == silo):
            return False
        return private is None or slug in private

    def label(slug: str | None) -> str:
        return names.get(slug or "", slug or "All collections")

    path = ledger_path(db_path)
    empty_meta = {"files_since": None, "searches_since": None}
    if not path.exists():
        conn = None
        meta = empty_meta
    else:
        conn = _connect(db_path, readonly=True)
        meta = {k: float(v) for k, v in conn.execute("SELECT key, value FROM meta") if k.endswith("_since")}

    def q(sql: str, args: tuple = ()) -> list[tuple]:
        return conn.execute(sql, args).fetchall() if conn is not None else []

    silo_clause = " AND silo = ?" if silo else ""
    silo_arg: tuple = (silo,) if silo else ()
    reindex = tuple(REINDEX_ORIGINS)
    origin_clause = f" AND origin NOT IN ({','.join('?' * len(reindex))})"

    # --- Library changes ---------------------------------------------------
    def file_rows(a: float, b: float) -> list[tuple]:
        return q(
            "SELECT ts, silo, path, kind FROM file_events WHERE ts >= ? AND ts <= ?"
            + origin_clause + silo_clause + " ORDER BY ts DESC",
            (a, b, *reindex, *silo_arg),
        )

    cur_files = file_rows(start, end)
    prev_files = file_rows(pstart, pend)
    kinds = ("added", "updated", "removed")

    def totals(rows: list[tuple]) -> dict[str, int]:
        t = {k: 0 for k in kinds}
        for _, _, _, kind in rows:
            t[kind] = t.get(kind, 0) + 1
        return t

    prev_by_silo: dict[str, int] = {}
    for _, s, _, _ in prev_files:
        prev_by_silo[s] = prev_by_silo.get(s, 0) + 1
    by_silo: dict[str, dict[str, Any]] = {}
    change_timeline = [{"start": _iso(b.timestamp()), **{k: 0 for k in kinds}} for b in buckets]
    for ts, s, p, kind in cur_files:
        row = by_silo.setdefault(s, {"silo": s, "name": label(s), "private": redacted(s),
                                     **{k: 0 for k in kinds}, "files": []})
        row[kind] += 1
        if not row["private"] and len(row["files"]) < detail_limit:
            row["files"].append({"path": p, "kind": kind, "ts": _iso(ts)})
        i = _bucket_index(ts, buckets)
        if i is not None:
            change_timeline[i][kind] += 1
    change_silos = sorted(
        (
            {**r, "total": r["added"] + r["updated"] + r["removed"], "previous_total": prev_by_silo.get(s, 0)}
            for s, r in by_silo.items()
        ),
        key=lambda r: (-r["total"], r["name"]),
    )
    silo_events = [
        {"silo": s, "name": label(s), "kind": k, "files": n, "ts": _iso(ts)}
        for ts, s, k, n in q(
            "SELECT ts, silo, kind, files FROM silo_events WHERE ts >= ? AND ts <= ?"
            + origin_clause + silo_clause + " ORDER BY ts DESC",
            (start, end, *reindex, *silo_arg),
        )
    ]

    # --- Library use -------------------------------------------------------
    def search_rows(a: float, b: float) -> list[tuple]:
        if silo:
            return q(
                "SELECT id, ts, silo, query_text, sources_found, client FROM searches"
                " WHERE ts >= ? AND ts <= ? AND (silo = ? OR id IN"
                " (SELECT search_id FROM search_hits WHERE silo = ?)) ORDER BY ts DESC",
                (a, b, silo, silo),
            )
        return q(
            "SELECT id, ts, silo, query_text, sources_found, client FROM searches"
            " WHERE ts >= ? AND ts <= ? ORDER BY ts DESC",
            (a, b),
        )

    cur_searches = search_rows(start, end)
    prev_searches = search_rows(pstart, pend)
    ids = [r[0] for r in cur_searches]
    hits_by_search: dict[int, list[tuple[str, str]]] = {}
    for i in range(0, len(ids), 500):
        part = ids[i : i + 500]
        for sid, s, p in q(
            f"SELECT search_id, silo, path FROM search_hits WHERE search_id IN ({','.join('?' * len(part))})",
            tuple(part),
        ):
            hits_by_search.setdefault(sid, []).append((s, p))

    use_by_silo: dict[str, dict[str, Any]] = {}
    doc_counts: dict[tuple[str, str], int] = {}
    by_client: dict[str, int] = {}
    search_timeline = [{"start": _iso(b.timestamp()), "searches": 0, "without_sources": 0} for b in buckets]
    no_source: dict[tuple[str, str | None], dict[str, Any]] = {}
    with_sources = 0
    for sid, ts, s_silo, text, found, client in cur_searches:
        by_client[client] = by_client.get(client, 0) + 1
        i = _bucket_index(ts, buckets)
        if i is not None:
            search_timeline[i]["searches"] += 1
        if found:
            with_sources += 1
        else:
            if i is not None:
                search_timeline[i]["without_sources"] += 1
            hidden = redacted(s_silo)
            key = ("(private collection)" if hidden else _norm_query(text) or "(query text not kept)", s_silo)
            grp = no_source.setdefault(key, {"query": key[0], "silo": s_silo, "name": label(s_silo),
                                             "count": 0, "last_ts": None})
            grp["count"] += 1
            grp["last_ts"] = grp["last_ts"] or _iso(ts)
        touched: dict[str, set[str]] = {}
        for h_silo, h_path in hits_by_search.get(sid, []):
            touched.setdefault(h_silo, set()).add(h_path)
            doc_counts[(h_silo, h_path)] = doc_counts.get((h_silo, h_path), 0) + 1
        if s_silo and s_silo not in touched:
            touched[s_silo] = set()
        for t_silo, paths in touched.items():
            row = use_by_silo.setdefault(t_silo, {"silo": t_silo, "name": label(t_silo),
                                                  "private": redacted(t_silo), "searches": 0, "files": set()})
            row["searches"] += 1
            row["files"].update(paths)
    use_silos = sorted(
        ({**{k: v for k, v in r.items() if k != "files"}, "unique_files": len(r["files"])}
         for r in use_by_silo.values()),
        key=lambda r: (-r["searches"], r["name"]),
    )
    if silo:
        use_silos = [r for r in use_silos if r["silo"] == silo]
    top_docs = [
        {"silo": s, "name": label(s), "path": p, "file": Path(p).name, "searches": n}
        for (s, p), n in sorted(doc_counts.items(), key=lambda kv: (-kv[1], kv[0][1]))
        if not redacted(s) and (not silo or s == silo)
    ][:detail_limit]
    recent_queries = [
        {"ts": _iso(ts), "silo": s, "name": label(s), "query": None if redacted(s) else text,
         "sources_found": bool(found), "client": client}
        for _, ts, s, text, found, client in cur_searches[:detail_limit]
    ]

    # --- Attention ---------------------------------------------------------
    fail_rows = q(
        "SELECT silo, path, error_class, error, first_seen, last_seen, occurrences FROM failures"
        " WHERE resolved_at IS NULL" + silo_clause + " ORDER BY last_seen DESC",
        silo_arg,
    )
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    open_by_silo: dict[str, int] = {}
    for f_silo, f_path, cls, err, first, last, occ in fail_rows:
        open_by_silo[f_silo] = open_by_silo.get(f_silo, 0) + 1
        g = groups.setdefault((f_silo, cls), {"silo": f_silo, "name": label(f_silo), "error_class": cls,
                                              "files": 0, "occurrences": 0, "sample_error": err,
                                              "last_seen": _iso(last), "paths": []})
        g["files"] += 1
        g["occurrences"] += occ
        if not redacted(f_silo) and len(g["paths"]) < 5:
            g["paths"].append(f_path)
        if redacted(f_silo):
            g["sample_error"] = None
    failure_groups = sorted(groups.values(), key=lambda g: (-g["files"], g["name"]))
    no_source_groups = sorted(no_source.values(), key=lambda g: (-g["count"], g["query"]))[:detail_limit]

    # --- Collection health (every silo, searched or not) --------------------
    counts = _manifest_counts(db_path)
    last_change = dict(q("SELECT silo, MAX(ts) FROM file_events WHERE 1=1" + origin_clause + " GROUP BY silo", reindex))
    last_search: dict[str, float] = dict(q("SELECT silo, MAX(ts) FROM searches WHERE silo IS NOT NULL GROUP BY silo"))
    for s, ts in q("SELECT h.silo, MAX(s.ts) FROM search_hits h JOIN searches s ON s.id = h.search_id GROUP BY h.silo"):
        last_search[s] = max(ts, last_search.get(s, 0.0))
    staleness: dict[str, dict] = {}
    if check_staleness:
        try:
            from operations import op_list_silos

            for row in op_list_silos(str(db_path), check_staleness=True):
                staleness[str(row.get("slug"))] = row
        except Exception:
            staleness = {}
    health = []
    for row in sorted(registry, key=lambda r: str(r.get("display_name") or r.get("slug") or "").lower()):
        slug = str(row.get("slug") or "")
        if not slug or (silo and slug != silo):
            continue
        h = {
            "silo": slug,
            "name": label(slug),
            "private": bool(private is None or slug in private),
            "files": counts.get(slug, int(row.get("files_indexed") or 0)),
            "updated": row.get("updated"),
            "path_exists": bool(row.get("path")) and Path(str(row.get("path"))).exists(),
            "last_change": _iso(last_change.get(slug)),
            "last_search": _iso(last_search.get(slug)),
            "open_failures": open_by_silo.get(slug, 0),
        }
        if slug in staleness:
            st = staleness[slug]
            h["is_stale"] = st.get("is_stale")
            h["stale_file_count"] = st.get("stale_file_count")
        health.append(h)
    behind = [
        {"silo": h["silo"], "name": h["name"],
         "reason": "missing_folder" if not h["path_exists"] else ("stale" if h.get("is_stale") else "failures"),
         "open_failures": h["open_failures"], "stale_file_count": h.get("stale_file_count")}
        for h in health
        if (not h["path_exists"]) or h.get("is_stale") or h["open_failures"]
    ]

    if conn is not None:
        conn.close()

    # --- History coverage + summary ----------------------------------------
    files_since = meta.get("files_since")
    searches_since = meta.get("searches_since")
    full_files = files_since is not None and files_since <= pstart
    full_searches = searches_since is not None and searches_since <= pstart
    history_since = min(x for x in (files_since, searches_since) if x is not None) if (files_since or searches_since) else None

    cur_tot = totals(cur_files)
    prev_tot = totals(prev_files)
    n_changes = sum(cur_tot.values())
    n_searches = len(cur_searches)
    n_prev_searches = len(prev_searches)
    n_without = n_searches - with_sources
    span, compared = _phrase(window)

    score: dict[str, int] = {}
    for r in change_silos:
        score[r["silo"]] = score.get(r["silo"], 0) + r["total"]
    for r in use_silos:
        score[r["silo"]] = score.get(r["silo"], 0) + r["searches"]
    busiest = sorted(score.items(), key=lambda kv: (-kv[1], label(kv[0])))
    if busiest and busiest[0][1] > 0:
        headline = f"{label(busiest[0][0])} was the busiest collection {span}."
    else:
        headline = f"No library activity {span}."
    sentences: list[str] = []
    if n_changes:
        top = [r["name"] for r in change_silos[:2] if r["total"]]
        if len(change_silos) == 1:
            sentences.append(f"All {n_changes} document change{'s' if n_changes != 1 else ''} were in {top[0]}.")
        else:
            sentences.append(f"Most document changes were in {_names_list(top)}.")
    elif not full_files and files_since and files_since > start:
        sentences.append(f"No document changes recorded since history began on {_iso(files_since)[:10]}.")
    else:
        sentences.append("No documents changed.")
    for ev in silo_events:
        if ev["kind"] == "silo_added":
            sentences.append(f"{ev['name']} was added with {ev['files']} file{'s' if ev['files'] != 1 else ''}.")
    search_word = f"{n_searches} search{'es' if n_searches != 1 else ''}"
    if full_searches:
        if n_searches > n_prev_searches:
            s = f"Searches increased to {n_searches} from {n_prev_searches} compared with {compared}"
        elif n_searches < n_prev_searches:
            s = f"Searches decreased to {n_searches} from {n_prev_searches} compared with {compared}"
        else:
            s = f"Searches held steady at {n_searches} compared with {compared}"
    else:
        s = f"{search_word.capitalize()} {span}"
    if n_without:
        s += f"; {n_without} returned no sources."
    else:
        s += "."
    sentences.append(s)
    if failure_groups:
        nf = sum(g["files"] for g in failure_groups)
        sentences.append(f"{nf} file{'s' if nf != 1 else ''} could not be indexed.")

    return {
        "window": window,
        "generated_at": _iso(now_dt.timestamp()),
        "silo": silo,
        "period": {"start": _iso(start), "end": _iso(end), "bucket_unit": bounds["bucket_unit"]},
        "previous_period": {"start": _iso(pstart), "end": _iso(pend)},
        "history": {
            "since": _iso(history_since),
            "files_since": _iso(files_since),
            "searches_since": _iso(searches_since),
            "full_comparison": bool(full_files and full_searches),
            "full_file_comparison": bool(full_files),
            "full_search_comparison": bool(full_searches),
        },
        "summary": {"headline": headline, "sentences": sentences},
        "changes": {
            "totals": cur_tot,
            "previous_totals": prev_tot if full_files else None,
            "by_silo": change_silos,
            "collections": silo_events,
            "timeline": change_timeline,
        },
        "use": {
            "searches": n_searches,
            "previous_searches": n_prev_searches if full_searches else None,
            "with_sources": with_sources,
            "without_sources": n_without,
            "by_silo": use_silos,
            "top_documents": top_docs,
            "by_client": [{"client": c, "searches": n} for c, n in sorted(by_client.items(), key=lambda kv: (-kv[1], kv[0]))],
            "recent": recent_queries,
            "timeline": search_timeline,
        },
        "attention": {
            "failures": failure_groups,
            "no_source_searches": no_source_groups,
            "falling_behind": behind,
        },
        "health": health,
        "ledger_path": str(path),
        "enabled": activity_enabled(),
    }


def format_overview(data: dict) -> str:
    """Human text for ``pal activity``."""
    lines: list[str] = []
    s = data["summary"]
    lines.append(s["headline"])
    lines.extend(f"  {x}" for x in s["sentences"])
    hist = data["history"]
    for flag, key, what in (
        ("full_file_comparison", "files_since", "Document-change"),
        ("full_search_comparison", "searches_since", "Search"),
    ):
        if not hist[flag] and hist.get(key):
            lines.append(f"  {what} history available since {hist[key][:10]}.")
    lines.append("")
    ch = data["changes"]
    t = ch["totals"]
    lines.append(f"Library changes  +{t['added']} added  ~{t['updated']} updated  -{t['removed']} removed")
    for r in ch["by_silo"][:10]:
        prev = f"  (prev {r['previous_total']})" if ch["previous_totals"] is not None else ""
        lines.append(f"  {r['name']:<28} +{r['added']} ~{r['updated']} -{r['removed']}{prev}")
    for ev in ch["collections"]:
        verb = "added" if ev["kind"] == "silo_added" else "removed"
        lines.append(f"  collection {verb}: {ev['name']} ({ev['files']} files)")
    lines.append("")
    u = data["use"]
    prev = f" (prev {u['previous_searches']})" if u["previous_searches"] is not None else ""
    lines.append(f"Library use  {u['searches']} searches{prev}, {u['with_sources']} found sources, {u['without_sources']} none")
    for r in u["by_silo"][:10]:
        lines.append(f"  {r['name']:<28} {r['searches']} searches, {r['unique_files']} files")
    if u["top_documents"]:
        lines.append("  Most surfaced:")
        lines.extend(f"    {d['searches']:>3}  {d['file']}  ({d['name']})" for d in u["top_documents"][:8])
    if u["by_client"]:
        lines.append("  Assistants: " + ", ".join(f"{c['client']} {c['searches']}" for c in u["by_client"][:6]))
    lines.append("")
    a = data["attention"]
    lines.append("Attention needed")
    if not (a["failures"] or a["no_source_searches"] or a["falling_behind"]):
        lines.append("  Nothing needs attention.")
    for g in a["failures"]:
        lines.append(f"  {g['name']}: {g['files']} file(s) failing ({g['error_class']})")
    for g in a["no_source_searches"][:8]:
        times = f" ×{g['count']}" if g["count"] > 1 else ""
        lines.append(f"  no sources{times}: {g['query']!r} in {g['name']}")
    for b in a["falling_behind"]:
        if b["reason"] != "failures":
            lines.append(f"  {b['name']}: {b['reason'].replace('_', ' ')}")
    lines.append("")
    lines.append("Collection health")
    for h in data["health"]:
        lock = " [private]" if h["private"] else ""
        last = (h["last_search"] or "never")[:10]
        lines.append(f"  {h['name']:<28} {h['files']:>6} files  last search {last}{lock}")
    return "\n".join(lines)


def to_json(data: dict) -> str:
    return json.dumps(data, indent=2)
