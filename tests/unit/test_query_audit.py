"""Coverage for the query audit trail (src/query_audit.py + MCP recent_queries).

The audit log is what you read after a session retrieved the wrong document
out of a multi-document silo, so the properties that matter are: the query
text survives, the per-source-file breakdown is recorded, filters select the
right records, and a broken log never breaks a retrieval.
"""

from __future__ import annotations

import contextlib
import json
import sys
from types import SimpleNamespace

import pytest

import query_audit


@pytest.fixture(autouse=True)
def _audit_on(monkeypatch):
    """conftest disables auditing globally; this module is the one that tests it."""
    monkeypatch.setenv("LLMLIBRARIAN_QUERY_AUDIT", "1")


def _chunks():
    return [
        {"text": "a", "score": 0.9, "source": "/f/AMTM_10K/amtm.htm", "silo": "hot_seat"},
        {"text": "b", "score": 0.7, "source": "/f/AMTM_10K/amtm.htm", "silo": "hot_seat"},
        {"text": "c", "score": 0.5, "source": "/f/AVAV_10K/avav.htm", "silo": "hot_seat"},
    ]


def test_record_captures_query_text_and_source_breakdown(tmp_path):
    log = tmp_path / "query-audit.jsonl"
    query_audit.record(
        tool="multi_query_knowledge",
        queries=["Amentum backlog", "Amentum cash flow"],
        silo="hot_seat",
        params={"n_results": 12, "section": None},
        chunks=_chunks(),
        outcome={"truncated": True},
        path=log,
    )

    entry = json.loads(log.read_text().strip())
    assert entry["queries"] == ["Amentum backlog", "Amentum cash flow"]
    assert entry["silo"] == "hot_seat"
    assert entry["params"] == {"n_results": 12}  # None values dropped
    result = entry["result"]
    assert result["chunks"] == 3
    assert result["truncated"] is True
    assert result["top_score"] == 0.9
    # The cross-document breakdown is the diagnostic payload.
    assert result["sources"] == [
        {"file": "amtm.htm", "path": "/f/AMTM_10K/amtm.htm", "chunks": 2},
        {"file": "avav.htm", "path": "/f/AVAV_10K/avav.htm", "chunks": 1},
    ]


def test_long_queries_are_truncated(tmp_path):
    log = tmp_path / "audit.jsonl"
    query_audit.record(tool="t", queries=["x" * 5000], silo=None, path=log)
    entry = json.loads(log.read_text().strip())
    assert len(entry["queries"][0]) == query_audit.MAX_QUERY_CHARS


def test_record_is_silent_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_QUERY_AUDIT", "0")
    log = tmp_path / "audit.jsonl"
    query_audit.record(tool="t", queries=["q"], silo=None, path=log)
    assert not log.exists()


def test_record_never_raises_on_bad_path(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    # Parent is a file → mkdir/open both fail; must swallow.
    query_audit.record(tool="t", queries=["q"], silo=None, path=blocker / "audit.jsonl")


def _seed(log, entries):
    for tool, silo, queries, sources in entries:
        query_audit.record(
            tool=tool,
            queries=queries,
            silo=silo,
            chunks=[{"source": s, "silo": silo or "", "score": 0.5, "text": "t"} for s in sources],
            path=log,
        )


@pytest.fixture
def seeded(tmp_path):
    log = tmp_path / "audit.jsonl"
    _seed(
        log,
        [
            ("query_personal_knowledge", "hot_seat", ["Amentum backlog"], ["/f/amtm.htm"]),
            ("multi_query_knowledge", "hot_seat", ["AVAV revenue"], ["/f/avav.htm", "/f/amtm.htm"]),
            ("query_personal_knowledge", "tjs-pc", ["journal entry"], ["/f/note.md"]),
        ],
    )
    return log


def test_filters_by_silo_and_tool(seeded):
    assert len(query_audit.read_records(path=seeded, silo="hot_seat")) == 2
    assert len(query_audit.read_records(path=seeded, tool="multi_query_knowledge")) == 1


def test_contains_matches_query_text_and_source_filename(seeded):
    assert len(query_audit.read_records(path=seeded, contains="backlog")) == 1
    # 'amtm' appears only as a source filename, never in query text.
    assert len(query_audit.read_records(path=seeded, contains="amtm")) == 2
    assert len(query_audit.read_records(path=seeded, contains="nothing")) == 0


def test_limit_keeps_most_recent(seeded):
    recs = query_audit.read_records(path=seeded, limit=1)
    assert [r["queries"] for r in recs] == [["journal entry"]]


def test_since_window_filters_out_old_records(seeded, tmp_path):
    assert len(query_audit.read_records(path=seeded, since="1h")) == 3
    assert len(query_audit.read_records(path=seeded, since="2099-01-01")) == 0


def test_read_records_skips_corrupt_lines(seeded):
    with seeded.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n\n")
    assert len(query_audit.read_records(path=seeded)) == 3


def test_summarize_records_rolls_up_sources(seeded):
    roll = query_audit.summarize_records(query_audit.read_records(path=seeded))
    assert roll["calls"] == 3
    assert roll["by_silo"] == {"hot_seat": 2, "tjs-pc": 1}
    assert roll["top_sources"][0] == {"file": "amtm.htm", "chunks": 2}


def test_rotation_caps_growth_and_keeps_generations(tmp_path, monkeypatch):
    log = tmp_path / "audit.jsonl"
    monkeypatch.setattr(query_audit, "MAX_BYTES", 200)
    for i in range(20):
        query_audit.record(tool="t", queries=[f"q{i}"], silo="s", path=log)

    assert log.stat().st_size < 400  # live log stays bounded
    assert log.with_suffix(log.suffix + ".1").exists()
    assert log.with_suffix(log.suffix + ".2").exists()
    # Only ROTATE_KEEP generations survive — nothing beyond .2 accumulates.
    assert not log.with_suffix(log.suffix + ".3").exists()
    # The newest records are always the ones retained.
    queries = [r["queries"][0] for r in query_audit.read_records(path=log, limit=100)]
    assert queries[-1] == "q19"
    assert queries == sorted(queries, key=lambda q: int(q[1:]))


def test_read_records_spans_rotated_files_oldest_first(tmp_path):
    log = tmp_path / "audit.jsonl"
    rotated = log.with_suffix(log.suffix + ".1")
    query_audit.record(tool="t", queries=["older"], silo="s", path=rotated)
    query_audit.record(tool="t", queries=["newer"], silo="s", path=log)

    recs = query_audit.read_records(path=log)
    assert [r["queries"][0] for r in recs] == ["older", "newer"]


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------


@pytest.fixture
def mcp(monkeypatch, tmp_path):
    import mcp_server

    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.setenv("PAL_HOME", str(tmp_path / "pal"))
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    monkeypatch.setattr(mcp_server, "_release_chroma", lambda: None)
    monkeypatch.setattr(mcp_server, "_mcp_chroma_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setitem(
        sys.modules,
        "query.core",
        SimpleNamespace(run_retrieve=lambda *, query, **kw: {"chunks": _chunks()}),
    )
    return mcp_server


def test_query_tools_write_audit_records_readable_by_recent_queries(mcp):
    mcp.query_personal_knowledge("Amentum contract types", silo="hot_seat", n_results=12)
    mcp.multi_query_knowledge(["AVAV revenue", "AVAV backlog"], silo="hot_seat", n_results=5)

    out = mcp.recent_queries(limit=10)

    assert out["summary"]["calls"] == 2
    assert out["summary"]["by_tool"] == {
        "query_personal_knowledge": 1,
        "multi_query_knowledge": 1,
    }
    assert [r["tool"] for r in out["records"]] == [
        "query_personal_knowledge",
        "multi_query_knowledge",
    ]
    assert out["records"][1]["queries"] == ["AVAV revenue", "AVAV backlog"]
    assert out["records"][0]["params"]["n_results"] == 12


def test_recent_queries_summary_only_omits_records(mcp):
    mcp.query_personal_knowledge("q", silo="hot_seat")
    out = mcp.recent_queries(summary_only=True)
    assert "records" not in out
    assert out["summary"]["calls"] == 1


def test_recent_queries_on_empty_log_explains_itself(mcp):
    out = mcp.recent_queries()
    assert out["records"] == []
    assert "note" in out


def test_audit_failure_does_not_break_retrieval(mcp, monkeypatch):
    monkeypatch.setattr(
        query_audit, "record", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    res = mcp.query_personal_knowledge("q", silo="hot_seat")
    assert len(res["chunks"]) == 3
