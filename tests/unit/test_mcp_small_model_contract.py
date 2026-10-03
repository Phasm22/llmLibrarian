"""Contract a small local model can follow without looping.

Pins the failures seen on 2026-10-02, when Open-WebUI personas on a 3B-active
MoE called the :8766 server: all 27 empty results that hour were content
questions that contained the word "capabilities". route_intent sent each one to a
deterministic intent, run_retrieve returned chunks=[] without ever querying
Chroma, and the response named neither the trigger word nor a next tool — so the
model rephrased, kept the word, and fanned out ten unscoped calls.

See docs/plans/mcp-small-model-contract.md.
"""
from __future__ import annotations

import pytest

# Ordinary content questions a deterministic intent used to swallow (the intent is
# what route_intent returned on 2026-10-02). Each is answerable from indexed text.
# Some no longer route there at all; the ones that still do ("history of changes
# to ...") must still reach retrieval over MCP.
HIJACKED_CONTENT_QUESTIONS = [
    ("README image indexing capabilities", "CAPABILITIES"),
    ("what are the camera's low-light capabilities", "CAPABILITIES"),
    ("what did the docs from 2024 say about sourdough", "FILE_LIST"),
    ("history of changes to the intent ledger", "TIMELINE"),
    ("evolution of my thinking on food tracking in 2025", "TIMELINE"),
    ("what's in my pantry inventory", "STRUCTURE"),
    ("what language did I study in 2019", "CODE_LANGUAGE"),
    ("what document types does the DMV need", "METADATA_ONLY"),
]


@pytest.fixture()
def retrieval_calls(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Record whether run_retrieve reached the Chroma phase, without Chroma."""
    import query.retrieve_locked as retrieve_locked

    calls: list[dict] = []

    def _fake_phase(**kw):
        calls.append(kw)
        return {"query": kw["query"], "intent": kw["intent"], "chunks": []}

    monkeypatch.setattr(retrieve_locked, "execute_retrieve_chroma_phase", _fake_phase)
    db = tmp_path / "db"
    db.mkdir()
    return calls, str(db)


def test_fixture_detects_a_retrieval(retrieval_calls):
    """Guard the guard: an unhijacked question must register a call, or every
    reaches_retrieval failure below would be vacuous."""
    from query.core import run_retrieve

    calls, db = retrieval_calls
    run_retrieve(query="what kind of car did my dad get", silo="dad-new-car-d6e890e2", n_results=5, db_path=db)
    assert len(calls) == 1


@pytest.mark.parametrize("query,hijacking_intent", HIJACKED_CONTENT_QUESTIONS)
def test_scoped_content_question_reaches_retrieval(retrieval_calls, query, hijacking_intent):
    """silo= says "search this corpus". Over MCP no deterministic handler runs, so
    short-circuiting to chunks=[] can never be the right answer for a scoped call."""
    from query.core import run_retrieve

    calls, db = retrieval_calls
    run_retrieve(query=query, silo="llmlibrarian-46ad0cbe", n_results=5, db_path=db)
    assert calls, f"{query!r} was routed to {hijacking_intent} and never queried the index"


@pytest.mark.parametrize("query,hijacking_intent", HIJACKED_CONTENT_QUESTIONS)
def test_unscoped_content_question_reaches_retrieval(retrieval_calls, query, hijacking_intent):
    """MCP has no deterministic answer to give, so an unscoped call that would get
    nothing from the deterministic path must fall through as well."""
    from query.core import run_retrieve

    calls, db = retrieval_calls
    run_retrieve(query=query, silo=None, n_results=5, db_path=db)
    assert calls, f"{query!r} was routed to {hijacking_intent} and never queried the index"


def _stub_server(monkeypatch, tmp_path, chunks: list[dict]):
    """mcp_server pointed at a tmp DB, with the Chroma phase returning ``chunks``."""
    import mcp_server
    import query.retrieve_locked as retrieve_locked

    db = tmp_path / "db"
    db.mkdir(exist_ok=True)
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    monkeypatch.setattr(mcp_server, "_release_chroma", lambda: None)
    monkeypatch.setattr(
        retrieve_locked,
        "execute_retrieve_chroma_phase",
        lambda **kw: {"query": kw["query"], "intent": kw["intent"], "chunks": [dict(c) for c in chunks]},
    )
    return mcp_server


def test_empty_deterministic_result_tells_the_model_what_to_do(monkeypatch, tmp_path):
    """A genuinely inventory-style ask ("what file types are supported") that
    retrieves nothing must name the tool that can answer it. "Try rephrasing"
    without naming the trigger word is what produced the rephrase loop."""
    mcp_server = _stub_server(monkeypatch, tmp_path, [])

    res = mcp_server.query_personal_knowledge("what file types are supported", n_results=5)

    assert res["chunks"] == []
    assert res.get("recommended_action"), (
        "empty deterministic result has no recommended_action; got keys "
        f"{sorted(res)} and note={res.get('note')!r}"
    )
    assert res["recommended_action"]["tool"] == "capabilities"


def test_scoped_empty_result_suggests_dropping_the_scope(monkeypatch, tmp_path):
    mcp_server = _stub_server(monkeypatch, tmp_path, [])

    res = mcp_server.query_personal_knowledge("dad's car color", silo="recipes-def", n_results=5)

    action = res["recommended_action"]
    assert action["tool"] == "query_personal_knowledge"
    assert "silo" not in action["args"]
    assert "Do not repeat" in action["reason"]


def test_unscoped_empty_result_says_stop_rephrasing(monkeypatch, tmp_path):
    mcp_server = _stub_server(monkeypatch, tmp_path, [])

    res = mcp_server.query_personal_knowledge("dad's car color", n_results=5)

    assert res["recommended_action"]["tool"] == "find_files"
    assert "same result" in res["recommended_action"]["reason"]


def test_hijacking_word_with_hits_gets_alternative_not_action(monkeypatch, tmp_path):
    """When the chunks answer it, the inventory reading is offered on the side,
    not as the next step."""
    chunk = {"text": "Image indexing uses OCR and a vision model.", "score": 0.6, "source": "/x/README.md", "silo": "s"}
    mcp_server = _stub_server(monkeypatch, tmp_path, [chunk])

    res = mcp_server.query_personal_knowledge("what file types are supported", silo="s", n_results=5)

    assert res["chunks"]
    assert "recommended_action" not in res
    assert res["alternative_tool"]["tool"] == "capabilities"


def test_lite_empty_result_names_only_lite_tools(monkeypatch, tmp_path):
    mcp_server = _stub_server(monkeypatch, tmp_path, [])
    monkeypatch.setattr(mcp_server, "_mcp_chroma_lock", lambda *a, **k: __import__("contextlib").nullcontext())
    from state import update_silo

    update_silo(mcp_server._DB_PATH, "recipes-def", str(tmp_path / "Recipes"), 1, 1, "2026-01-01T00:00:00+00:00")

    res = mcp_server.retrieve_knowledge("what file types are supported", silo="recipes-def")

    assert res["chunks"] == []
    assert res["recommended_action"]["tool"] == "silo_roster"


def test_multi_query_with_errors_does_not_paper_over_them(monkeypatch, tmp_path):
    import mcp_contract

    out = mcp_contract.apply_guidance({"chunks": [], "errors": ["'q': RuntimeError: boom"]}, query="q", silo=None)
    assert "recommended_action" not in out


def test_lite_profile_keeps_the_reason_for_an_empty_result():
    """retrieve_knowledge projects run_retrieve's output down to a few fields; it
    must keep whatever explains an empty result."""
    import mcp_server

    shared = {
        "query": "README image indexing capabilities",
        "note": "something the caller should read",
        "chunks": [],
    }
    out = mcp_server._compact_lite_retrieval(shared, silo="llmlibrarian-46ad0cbe")

    assert out["chunks"] == []
    assert out.get("note") or out.get("recommended_action"), out
