"""Compact MCP tools exposed only by the small-context startup profile."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def mcp_module(monkeypatch, tmp_path):
    import mcp_server

    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    monkeypatch.setattr(mcp_server, "_release_chroma", lambda: None)
    return mcp_server


def _register_silo(db: Path, slug: str = "notes-1234abcd") -> None:
    from state import update_silo

    source = db.parent / slug
    source.mkdir(exist_ok=True)
    update_silo(
        db,
        slug,
        str(source),
        files_indexed=2,
        chunks_count=7,
        updated_iso="2026-09-14T00:00:00+00:00",
        display_name="Notes",
    )


def test_lite_profile_registers_only_compact_tools():
    script = """
import asyncio
import json
import mcp_server
async def main():
    tools = await mcp_server.mcp.list_tools()
    print(json.dumps(sorted(tool.name for tool in tools)))
asyncio.run(main())
"""
    env = os.environ.copy()
    env["LLMLIBRARIAN_MCP_PROFILE"] = "lite"
    env["LLMLIBRARIAN_ENV_BOOTSTRAPPED"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(proc.stdout) == ["retrieve_knowledge", "silo_roster"]


def test_silo_roster_is_sorted_and_compact(mcp_module):
    _register_silo(Path(mcp_module._DB_PATH), "zebra-1234abcd")
    _register_silo(Path(mcp_module._DB_PATH), "alpha-1234abcd")

    out = mcp_module.silo_roster()

    assert out["db_exists"] is True
    assert [row["slug"] for row in out["silos"]] == ["alpha-1234abcd", "zebra-1234abcd"]
    assert set(out["silos"][0]) == {"slug", "display_name", "chunks_count"}


def test_silo_roster_reports_missing_db(monkeypatch, tmp_path):
    import mcp_server

    monkeypatch.setattr(mcp_server, "_DB_PATH", str(tmp_path / "missing"))
    out = mcp_server.silo_roster()

    assert out["db_exists"] is False
    assert out["silos"] == []


def test_retrieve_knowledge_projects_evidence_and_rebuild_signal(monkeypatch, mcp_module):
    _register_silo(Path(mcp_module._DB_PATH))
    audit_calls = []
    usage_calls = []

    def fake_retrieve(**kwargs):
        assert kwargs["silo"] == "notes-1234abcd"
        assert kwargs["n_results"] == 3
        return {
            "query": kwargs["query"],
            "chunks": [
                {
                    "text": "The useful evidence.",
                    "score": 0.9,
                    "source": "/notes/decision.md",
                    "section": "Decision",
                    "doc_type": "other",
                    "page": 2,
                    "line_start": 14,
                    "_signals": {"vector_rank": 1},
                    "mtime_iso": "2026-09-14",
                    "silo": "notes-1234abcd",
                }
            ],
            "write_in_progress": {
                "results_may_be_incomplete": True,
                "rebuilding": ["notes-1234abcd"],
            },
            "retryable": True,
            "tax_ledger": [{"raw_value": "999"}],
            "chunks_by_silo": {"notes-1234abcd": []},
        }

    monkeypatch.setitem(sys.modules, "query.core", SimpleNamespace(run_retrieve=fake_retrieve))
    monkeypatch.setattr(mcp_module, "_emit_query_audit", lambda **kwargs: audit_calls.append(kwargs))
    monkeypatch.setattr(mcp_module, "_emit_usage_event", lambda *args: usage_calls.append(args))

    out = mcp_module.retrieve_knowledge("what did I decide?", "notes-1234abcd")

    assert out["silo"] == "notes-1234abcd"
    assert out["results_may_be_incomplete"] is True
    assert out["retryable"] is True
    assert set(out["chunks"][0]) == {
        "text", "score", "source", "section", "doc_type", "page", "line_start"
    }
    assert "tax_ledger" not in out
    assert "chunks_by_silo" not in out
    assert audit_calls[0]["tool"] == "retrieve_knowledge"
    assert audit_calls[0]["params"] == {"n_results": 3}
    assert usage_calls[0][1]["profile"] == "lite"


@pytest.mark.parametrize("n_results", [0, 6, True, "3"])
def test_retrieve_knowledge_rejects_unsafe_result_counts(mcp_module, n_results):
    _register_silo(Path(mcp_module._DB_PATH))

    out = mcp_module.retrieve_knowledge("q", "notes-1234abcd", n_results=n_results)

    assert "n_results must be an integer from 1 through 5" in out["error"]
    assert out["chunks"] == []


def test_retrieve_knowledge_requires_exact_registered_slug(mcp_module):
    _register_silo(Path(mcp_module._DB_PATH))

    out = mcp_module.retrieve_knowledge("q", "Notes")

    assert "Unknown silo slug" in out["error"]
    assert out["chunks"] == []


def test_retrieve_knowledge_turns_lock_timeout_into_retryable_response(monkeypatch, mcp_module):
    _register_silo(Path(mcp_module._DB_PATH))

    class _TimeoutContext:
        def __enter__(self):
            raise TimeoutError("busy")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(mcp_module, "_mcp_chroma_lock", lambda _operation: _TimeoutContext())

    out = mcp_module.retrieve_knowledge("q", "notes-1234abcd")

    assert out["busy"] is True
    assert out["retryable"] is True
    assert out["chunks"] == []
