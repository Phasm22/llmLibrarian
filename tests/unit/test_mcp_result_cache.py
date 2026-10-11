"""Identical MCP retrieval calls share one computation.

2026-10-02: the same query+silo arrived 6× and 10× within a second as parallel
tool calls. See src/mcp_result_cache.py.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from mcp_result_cache import ResultCache, repeat_fields


def test_parallel_burst_computes_once():
    cache = ResultCache()
    calls = []
    gate = threading.Event()

    def compute():
        calls.append(1)
        gate.wait(2)
        return ({"chunks": [{"text": "x"}]}, None)

    results = []

    def worker():
        results.append(cache.get_or_compute("k", compute, ttl_seconds=30))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(5)

    assert len(calls) == 1
    assert len(results) == 10
    assert sorted(meta["repeat_count"] for _v, meta in results) == list(range(1, 11))
    assert all(value == ({"chunks": [{"text": "x"}]}, None) for value, _m in results)


def test_ttl_expires():
    now = [0.0]
    cache = ResultCache(clock=lambda: now[0])
    calls = []

    def compute():
        calls.append(1)
        return ({"chunks": []}, None)

    cache.get_or_compute("k", compute, ttl_seconds=30)
    now[0] = 10
    _v, meta = cache.get_or_compute("k", compute, ttl_seconds=30)
    assert meta["cached"] and len(calls) == 1
    now[0] = 45
    cache.get_or_compute("k", compute, ttl_seconds=30)
    assert len(calls) == 2


@pytest.mark.parametrize("transient", [{"busy": True}, {"error": "x"}, {"write_in_progress": {"silos": ["s"]}}, {"retryable": True}])
def test_transient_results_are_not_cached(transient):
    cache = ResultCache()
    calls = []

    def compute():
        calls.append(1)
        return ({"chunks": [], **transient}, None)

    cache.get_or_compute("k", compute, ttl_seconds=30)
    cache.get_or_compute("k", compute, ttl_seconds=30)
    assert len(calls) == 2


def test_leader_failure_does_not_poison_the_key():
    cache = ResultCache()
    with pytest.raises(RuntimeError):
        cache.get_or_compute("k", lambda: (_ for _ in ()).throw(RuntimeError("boom")), ttl_seconds=30)
    value, meta = cache.get_or_compute("k", lambda: ({"chunks": []}, None), ttl_seconds=30)
    assert value == ({"chunks": []}, None) and meta["repeat_count"] == 1


def test_returned_values_are_independent_copies():
    cache = ResultCache()
    first, _ = cache.get_or_compute("k", lambda: ({"chunks": [{"text": "a"}]}, None), ttl_seconds=30)
    first[0]["chunks"].append("mutated")
    second, _ = cache.get_or_compute("k", lambda: ({"chunks": []}, None), ttl_seconds=30)
    assert second[0]["chunks"] == [{"text": "a"}]


def test_repeat_fields_escalate():
    assert repeat_fields({"repeat_count": 1}, window_seconds=30) == {}
    assert "repeat_notice" not in repeat_fields({"repeat_count": 2, "first_seen": 0}, window_seconds=30)
    assert "will not change" in repeat_fields({"repeat_count": 3, "first_seen": 0}, window_seconds=30)["repeat_notice"]


def test_query_tool_shares_work_audits_each_call_and_invalidates_on_ingest(monkeypatch, tmp_path):
    import mcp_server
    import query.retrieve_locked as retrieve_locked

    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.setenv("LLMLIBRARIAN_MCP_RESULT_CACHE_SECONDS", "30")
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    monkeypatch.setattr(mcp_server, "_release_chroma", lambda: None)
    mcp_server._RESULT_CACHE.clear()
    phases = []
    monkeypatch.setattr(
        retrieve_locked,
        "execute_retrieve_chroma_phase",
        lambda **kw: phases.append(1) or {"query": kw["query"], "intent": kw["intent"], "chunks": [
            {"text": "Bulk ferment four hours.", "score": 0.6, "source": "/r/bread.txt", "silo": "recipes-1"}
        ]},
    )
    audits = []
    monkeypatch.setattr(mcp_server, "_emit_query_audit", lambda **kw: audits.append(kw))

    responses = [mcp_server.query_personal_knowledge("sourdough  timing", silo="recipes-1") for _ in range(3)]

    assert len(phases) == 1
    assert len(audits) == 3 and [a["outcome"].get("cached", False) for a in audits] == [False, True, True]
    assert "repeat_of" not in responses[0]
    assert responses[2]["repeat_of"]["count"] == 3 and responses[2]["repeat_notice"]

    # An ingest commit rewrites the manifest; the next call must not be served stale.
    manifest = db / "llmli_file_manifest.json"
    manifest.write_text("{}")
    os.utime(manifest, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    mcp_server.query_personal_knowledge("sourdough timing", silo="recipes-1")
    assert len(phases) == 2
    mcp_server._RESULT_CACHE.clear()
