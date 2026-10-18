"""Coverage for the activity ledger (src/activity_ledger.py).

The Overview's numbers are only as trustworthy as these properties: every
manifest write is diffed into exact add/update/remove events, a re-index is not
a document change, repeated failures group instead of piling up, searches count
unique files rather than chunks, and comparisons only claim a previous period
the ledger actually covers.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import activity_ledger as al
from file_registry import _update_file_manifest
from state import append_last_failures, set_last_failures, set_silo_private, update_silo

NOW = datetime(2026, 10, 18, 15, 0).astimezone()


@pytest.fixture(autouse=True)
def _ledger_on(monkeypatch):
    """conftest disables the ledger globally; this module is the one that tests it."""
    monkeypatch.setenv("LLMLIBRARIAN_ACTIVITY", "1")
    monkeypatch.setenv("LLMLIBRARIAN_QUERY_AUDIT", "1")


@pytest.fixture
def db(tmp_path):
    d = tmp_path / "db"
    d.mkdir()
    for slug, name in (("research-1", "Research"), ("notes-2", "Project Notes"), ("tax-3", "Tax")):
        root = tmp_path / name
        root.mkdir()
        update_silo(d, slug, str(root), 0, 0, "2026-10-01T00:00:00", display_name=name)
    set_silo_private(d, "tax-3", True)
    return d


def _root(db: Path, name: str) -> str:
    return str(db.parent / name)


def _put(db, slug: str, root: str, files: dict[str, str]) -> None:
    def fn(m):
        entry = m.setdefault("silos", {}).setdefault(slug, {"path": root, "files": {}})
        for path, h in files.items():
            if h is None:
                entry["files"].pop(path, None)
            else:
                entry["files"][path] = {"mtime": 1.0, "size": 10, "hash": h}

    _update_file_manifest(db, fn)


def _rows(db, sql, args=()):
    conn = sqlite3.connect(al.ledger_path(db))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _ts(days_ago: float = 0, hours_ago: float = 0) -> float:
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).timestamp()


def _backdate_history(db, days: int = 90) -> None:
    conn = sqlite3.connect(al.ledger_path(db))
    conn.execute("UPDATE meta SET value=? WHERE key IN ('files_since','searches_since')", (str(_ts(days)),))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Manifest diffs
# ---------------------------------------------------------------------------


def test_manifest_writes_record_added_updated_removed(db):
    root = _root(db, "Research")
    _put(db, "research-1", root, {f"{root}/a.md": "h1"})  # silo key appears -> silo event
    _put(db, "research-1", root, {f"{root}/b.md": "h2"})
    _put(db, "research-1", root, {f"{root}/a.md": "h1b"})
    _put(db, "research-1", root, {f"{root}/b.md": None})

    assert _rows(db, "SELECT silo, kind, files FROM silo_events") == [("research-1", "silo_added", 1)]
    assert _rows(db, "SELECT path, kind FROM file_events ORDER BY id") == [
        (f"{root}/b.md", "added"),
        (f"{root}/a.md", "updated"),
        (f"{root}/b.md", "removed"),
    ]


def test_unchanged_hash_records_nothing(db):
    root = _root(db, "Research")
    _put(db, "research-1", root, {f"{root}/a.md": "h1"})
    _put(db, "research-1", root, {f"{root}/a.md": "h1"})
    assert _rows(db, "SELECT COUNT(*) FROM file_events") == [(0,)]


def test_hashless_zip_counts_mtime_change_as_update():
    before = {"s": {"/r/x.zip": ("", 1.0, 10)}}
    after = {"s": {"/r/x.zip": ("", 2.0, 10)}}
    files, silos = al.diff_signatures(before, after)
    assert files == [("s", "/r/x.zip", "updated", 10)] and silos == []


def test_silo_removal_is_one_event_not_n_file_removals(db):
    root = _root(db, "Research")
    _put(db, "research-1", root, {f"{root}/{i}.md": str(i) for i in range(5)})

    def drop(m):
        m["silos"].pop("research-1")

    _update_file_manifest(db, drop)
    assert _rows(db, "SELECT kind, files FROM silo_events ORDER BY id") == [("silo_added", 5), ("silo_removed", 5)]
    assert _rows(db, "SELECT COUNT(*) FROM file_events") == [(0,)]


def test_repair_origin_is_excluded_from_library_changes(db):
    root = _root(db, "Research")
    _put(db, "research-1", root, {f"{root}/a.md": "h1"})
    with al.origin("repair"):
        _put(db, "research-1", root, {f"{root}/a.md": "h2"})
    _put(db, "research-1", root, {f"{root}/b.md": "h3"})
    data = al.overview(db, "today")
    assert data["changes"]["totals"] == {"added": 1, "updated": 0, "removed": 0}
    assert _rows(db, "SELECT origin FROM file_events ORDER BY id") == [("repair",), ("index",)]


def test_ledger_disabled_writes_nothing(db, monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_ACTIVITY", "0")
    root = _root(db, "Research")
    _put(db, "research-1", root, {f"{root}/a.md": "h1"})
    assert not al.ledger_path(db).exists()


def test_broken_ledger_never_breaks_manifest_write(db, monkeypatch):
    monkeypatch.setattr(al, "_connect", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("boom")))
    root = _root(db, "Research")
    _put(db, "research-1", root, {f"{root}/a.md": "h1"})
    manifest = json.loads((db / "llmli_file_manifest.json").read_text())
    assert f"{root}/a.md" in manifest["silos"]["research-1"]["files"]


def test_concurrent_writers_lose_nothing(db):
    root = _root(db, "Research")
    _put(db, "research-1", root, {f"{root}/seed.md": "s"})

    def worker(n):
        for i in range(10):
            al.record_search(db, tool="query_personal_knowledge", queries=[f"q{n}-{i}"], silo="research-1",
                             chunks=[{"source": f"{root}/seed.md", "silo": "research-1"}])

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _rows(db, "SELECT COUNT(*) FROM searches") == [(40,)]
    assert _rows(db, "SELECT COUNT(*) FROM search_hits") == [(40,)]


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def test_failures_group_by_silo_and_class_and_resolve_on_success(db):
    root = _root(db, "Research")
    _put(db, "research-1", root, {f"{root}/ok.md": "h"})
    bad = f"{root}/scan.pdf"
    set_last_failures(db, [{"path": bad, "error": "PdfReadError: file has not been decrypted (encrypted)"}])
    set_last_failures(db, [{"path": bad, "error": "PdfReadError: file has not been decrypted (encrypted)"}])
    append_last_failures(db, [{"path": f"{root}/big.csv", "error": "file too large"}])

    rows = _rows(db, "SELECT silo, path, error_class, occurrences FROM failures ORDER BY id")
    assert rows == [("research-1", bad, "encrypted", 2), ("research-1", f"{root}/big.csv", "too_large", 1)]

    # The merged JSON list is rewritten on append, but only the new entry occurred now.
    append_last_failures(db, [{"path": f"{root}/other.md", "error": "Permission denied"}])
    assert _rows(db, "SELECT occurrences FROM failures WHERE path=?", (bad,)) == [(2,)]

    _put(db, "research-1", root, {bad: "fixed"})
    assert _rows(db, "SELECT resolved_at IS NOT NULL FROM failures WHERE path=?", (bad,)) == [(1,)]

    groups = al.overview(db, "7d")["attention"]["failures"]
    assert {g["error_class"] for g in groups} == {"too_large", "permission"}


def test_silo_for_path_picks_longest_root():
    roots = {"outer": "/a", "inner": "/a/b"}
    assert al.silo_for_path("/a/b/c.md", roots) == "inner"
    assert al.silo_for_path("/a/c.md", roots) == "outer"
    assert al.silo_for_path("/ab/c.md", roots) is None


# ---------------------------------------------------------------------------
# Searches
# ---------------------------------------------------------------------------


def test_search_counts_unique_files_not_chunks(db):
    chunks = [
        {"source": "/r/a.md", "silo": "research-1"},
        {"source": "/r/a.md", "silo": "research-1"},
        {"source": "/n/b.md", "silo": "notes-2"},
    ]
    al.record_search(db, tool="multi_query_knowledge", queries=["x", "y"], silo=None, chunks=chunks,
                     outcome={"truncated": True}, client="Claude Code")
    assert _rows(db, "SELECT query_text, unique_files, sources_found, client, truncated FROM searches") == [
        ("x | y", 2, 1, "Claude Code", 1)
    ]
    assert sorted(_rows(db, "SELECT silo, path FROM search_hits")) == [("notes-2", "/n/b.md"), ("research-1", "/r/a.md")]


def test_empty_search_is_no_sources_and_text_dropped_when_audit_off(db, monkeypatch):
    al.record_search(db, tool="query_personal_knowledge", queries=["nothing"], silo="research-1", chunks=[],
                     keep_text=False)
    assert _rows(db, "SELECT query_text, sources_found FROM searches") == [(None, 0)]


def test_read_document_is_not_a_search(db):
    al.record_search(db, tool="read_document", queries=["/r/a.md"], silo="research-1",
                     chunks=[{"source": "/r/a.md", "silo": "research-1"}])
    assert not al.ledger_path(db).exists() or _rows(db, "SELECT COUNT(*) FROM searches") == [(0,)]


def test_mcp_query_tools_write_ledger_rows(db, monkeypatch, tmp_path):
    import contextlib
    import sys
    from types import SimpleNamespace

    import mcp_server

    monkeypatch.setenv("PAL_HOME", str(tmp_path / "pal"))
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    monkeypatch.setattr(mcp_server, "_release_chroma", lambda: None)
    monkeypatch.setattr(mcp_server, "_mcp_chroma_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setitem(
        sys.modules,
        "query.core",
        SimpleNamespace(run_retrieve=lambda *, query, **kw: {"chunks": [{"source": "/r/a.md", "silo": "research-1",
                                                                         "text": "t", "score": 0.5}]}),
    )
    mcp_server.query_personal_knowledge("what changed", silo="research-1")
    assert _rows(db, "SELECT tool, silo, query_text, unique_files FROM searches") == [
        ("query_personal_knowledge", "research-1", "what changed", 1)
    ]


# ---------------------------------------------------------------------------
# Overview math
# ---------------------------------------------------------------------------


def test_period_bounds_compare_like_with_like():
    b = al.period_bounds("today", NOW)
    assert b["start"].hour == 0 and b["start"].date() == NOW.date()
    assert b["prev_end"] == NOW - timedelta(days=1)
    assert len(b["buckets"]) == 16  # hours 0..15

    w = al.period_bounds("7d", NOW)
    assert w["start"].date() == (NOW - timedelta(days=6)).date()
    assert w["prev_start"].date() == (NOW - timedelta(days=13)).date()
    assert len(w["buckets"]) == 7

    m = al.period_bounds("30d", NOW)
    assert len(m["buckets"]) == 30 and m["prev_end"] == NOW - timedelta(days=30)

    with pytest.raises(ValueError):
        al.period_bounds("year", NOW)


def _seed_week(db):
    r, n = _root(db, "Research"), _root(db, "Project Notes")
    _put(db, "research-1", r, {f"{r}/seed.md": "s"})
    _put(db, "notes-2", n, {f"{n}/seed.md": "s"})
    _backdate_history(db)
    conn = sqlite3.connect(al.ledger_path(db))
    conn.execute("DELETE FROM silo_events")
    rows = [
        (_ts(1), "research-1", f"{r}/a.md", "added", "index", 1),
        (_ts(2), "research-1", f"{r}/b.md", "updated", "index", 1),
        (_ts(3), "research-1", f"{r}/c.md", "removed", "index", 1),
        (_ts(1), "notes-2", f"{n}/x.md", "added", "index", 1),
        (_ts(9), "notes-2", f"{n}/old.md", "added", "index", 1),  # previous week
        (_ts(1), "tax-3", "/tax/w2-2025.pdf", "added", "index", 1),
    ]
    conn.executemany("INSERT INTO file_events(ts, silo, path, kind, origin, size) VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    for days, q, silo, chunks in [
        (1, "deadline", "research-1", [{"source": f"{r}/a.md", "silo": "research-1"}]),
        (2, "deadline", "research-1", [{"source": f"{r}/a.md", "silo": "research-1"}]),
        (2, "budget", None, [{"source": f"{n}/x.md", "silo": "notes-2"}, {"source": f"{r}/a.md", "silo": "research-1"}]),
        (3, "Missing   Thing", "research-1", []),
        (4, "missing thing", "research-1", []),
        (2, "my agi", "tax-3", []),
        (10, "older", "notes-2", [{"source": f"{n}/x.md", "silo": "notes-2"}]),
    ]:
        al.record_search(db, tool="query_personal_knowledge", queries=[q], silo=silo, chunks=chunks,
                         client="Claude Code" if days != 2 else "Codex", now=_ts(days))


def test_overview_counts_and_compares(db):
    _seed_week(db)
    data = al.overview(db, "7d", now=NOW)

    assert data["history"]["full_comparison"] is True
    assert data["changes"]["totals"] == {"added": 3, "updated": 1, "removed": 1}
    assert data["changes"]["previous_totals"] == {"added": 1, "updated": 0, "removed": 0}
    by = {r["silo"]: r for r in data["changes"]["by_silo"]}
    assert by["research-1"]["total"] == 3 and by["notes-2"]["previous_total"] == 1

    u = data["use"]
    assert (u["searches"], u["previous_searches"], u["with_sources"], u["without_sources"]) == (6, 1, 3, 3)
    assert u["top_documents"][0]["file"] == "a.md" and u["top_documents"][0]["searches"] == 3
    assert {c["client"]: c["searches"] for c in u["by_client"]} == {"Claude Code": 3, "Codex": 3}
    assert sum(b["searches"] for b in u["timeline"]) == 6

    groups = data["attention"]["no_source_searches"]
    assert groups[0]["query"] == "missing thing" and groups[0]["count"] == 2

    assert data["summary"]["headline"] == "Research was the busiest collection this week."
    assert "Searches increased to 6 from 1 compared with the previous week; 3 returned no sources." in data["summary"]["sentences"]


def test_private_silo_counts_without_paths_or_text(db):
    _seed_week(db)
    data = al.overview(db, "7d", now=NOW)
    tax = next(r for r in data["changes"]["by_silo"] if r["silo"] == "tax-3")
    assert tax["added"] == 1 and tax["files"] == [] and tax["private"] is True
    assert all(d["silo"] != "tax-3" for d in data["use"]["top_documents"])
    priv = [g for g in data["attention"]["no_source_searches"] if g["silo"] == "tax-3"]
    assert priv and priv[0]["query"] == "(private collection)"
    assert "my agi" not in json.dumps(data)

    named = al.overview(db, "7d", now=NOW, silo="tax-3")
    assert named["changes"]["by_silo"][0]["files"][0]["path"] == "/tax/w2-2025.pdf"
    assert named["attention"]["no_source_searches"][0]["query"] == "my agi"


def test_history_shorter_than_previous_period_drops_comparison(db):
    al.record_search(db, tool="query_personal_knowledge", queries=["q"], silo="research-1", chunks=[], now=_ts(1))
    conn = sqlite3.connect(al.ledger_path(db))
    conn.execute("UPDATE meta SET value=? WHERE key IN ('files_since','searches_since')", (str(_ts(3)),))
    conn.commit()
    conn.close()
    data = al.overview(db, "7d", now=NOW)
    assert data["history"]["full_comparison"] is False
    assert data["use"]["previous_searches"] is None and data["changes"]["previous_totals"] is None
    assert not any("compared with" in s for s in data["summary"]["sentences"])
    assert "Search history available since" in al.format_overview(data)


def test_health_lists_unsearched_silos(db):
    _seed_week(db)
    health = {h["silo"]: h for h in al.overview(db, "7d", now=NOW)["health"]}
    assert set(health) == {"research-1", "notes-2", "tax-3"}
    assert health["tax-3"]["private"] is True
    assert health["research-1"]["files"] == 1 and health["research-1"]["last_search"]


def test_overview_without_ledger_is_empty_not_an_error(db):
    data = al.overview(db, "today", now=NOW)
    assert data["use"]["searches"] == 0 and data["summary"]["headline"] == "No library activity today."
    assert len(data["health"]) == 3


# ---------------------------------------------------------------------------
# Backfill + retention
# ---------------------------------------------------------------------------


def test_backfill_imports_audit_once(db, tmp_path):
    log = tmp_path / "audit.jsonl"
    entries = [
        {"ts": "2026-09-01T10:00:00+00:00", "tool": "query_personal_knowledge", "silo": "research-1",
         "queries": ["alpha"], "result": {"chunks": 2, "silos": {"research-1": 2},
                                          "sources": [{"file": "a.md", "path": "/r/a.md", "chunks": 2}], "sources_total": 1}},
        {"ts": "2026-09-02T10:00:00+00:00", "tool": "multi_query_knowledge", "silo": None,
         "queries": ["b"], "result": {"chunks": 0, "silos": {}, "sources": [], "sources_total": 0}},
        {"ts": "2026-09-03T10:00:00+00:00", "tool": "read_document", "silo": "research-1",
         "queries": ["/r/a.md"], "result": {"chunks": 1}},
    ]
    # A same-second repeat is a real second request, not a duplicate.
    entries.insert(1, dict(entries[0]))
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    first = al.backfill_from_audit(db, audit_path=log)
    second = al.backfill_from_audit(db, audit_path=log)
    assert (first["imported"], second["imported"], second["skipped_already_covered"]) == (3, 0, 3)
    assert _rows(db, "SELECT silo, path FROM search_hits") == [("research-1", "/r/a.md")] * 2
    since = float(_rows(db, "SELECT value FROM meta WHERE key='searches_since'")[0][0])
    assert since == datetime.fromisoformat("2026-09-01T10:00:00+00:00").timestamp()


def test_prune_drops_old_events_and_moves_history_forward(db, monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_ACTIVITY_RETENTION_DAYS", "30")
    al.record_search(db, tool="query_personal_knowledge", queries=["old"], silo=None,
                     chunks=[{"source": "/r/a.md", "silo": "research-1"}], now=_ts(60))
    al.record_search(db, tool="query_personal_knowledge", queries=["new"], silo=None, chunks=[], now=_ts(1))
    _backdate_history(db, 90)
    conn = sqlite3.connect(al.ledger_path(db), isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    al.prune(conn, now=NOW.timestamp())
    conn.close()
    assert _rows(db, "SELECT query_text FROM searches") == [("new",)]
    assert _rows(db, "SELECT COUNT(*) FROM search_hits") == [(0,)]
    since = float(_rows(db, "SELECT value FROM meta WHERE key='searches_since'")[0][0])
    assert abs(since - _ts(30)) < 1


def test_error_class_buckets():
    assert al.error_class("Operation timed out after 30s") == "timeout"
    assert al.error_class("ValueError: bad header") == "ValueError"
    assert al.error_class("weird") == "other"


def test_backfill_skips_records_the_live_ledger_already_has(db, tmp_path):
    al.record_search(db, tool="query_personal_knowledge", queries=["live"], silo=None, chunks=[])
    log = tmp_path / "audit.jsonl"
    future = datetime.now().astimezone() + timedelta(minutes=5)
    log.write_text(json.dumps({"ts": future.isoformat(), "tool": "query_personal_knowledge",
                               "queries": ["live"], "result": {"chunks": 0}}) + "\n")
    assert al.backfill_from_audit(db, audit_path=log)["imported"] == 0
    assert _rows(db, "SELECT COUNT(*) FROM searches") == [(1,)]
