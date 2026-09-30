"""End-to-end: private silos stay out of every unscoped MCP tool, full and lite.

test_silo_privacy_retrieval.py proves run_retrieve honors the flag. This goes one
layer up, through the tool functions a client actually calls, because the leak
that motivated it was observed there: an unscoped query_personal_knowledge on
the resident server returned tax and chat-archive chunks. That server was
running a branch whose tools never consulted the flag, so a run_retrieve-only
test would not have caught a tool that bypasses it, or a merge that drops it.

Only the `silo` field of each chunk is asserted on, plus one marker string that
exists solely in the fixture, so a failure never prints private text.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ingest import run_add
from query.core import run_retrieve
from state import resolve_silo_by_path, set_silo_private

_SECRET = (
    "Adjusted gross income for the tax year was reported on the return. "
    "Account number and taxpayer identifier appear on every page of this filing. "
    "Withholding and W-2 wage figures are itemized below. PRIVATE-MARKER-7f3a."
)
_PUBLIC = (
    "Sourdough recipe: combine flour, water, and starter. "
    "Bulk ferment for four hours, then shape and proof overnight."
)
_PROBE = "adjusted gross income account number taxpayer identifier withholding"


@pytest.fixture()
def indexed(tmp_path: Path, monkeypatch):
    """Index one private and one public silo, and point mcp_server at them."""
    db = tmp_path / "db"
    secret_dir = tmp_path / "Tax"
    secret_dir.mkdir()
    (secret_dir / "return.txt").write_text(_SECRET, encoding="utf-8")
    public_dir = tmp_path / "Recipes"
    public_dir.mkdir()
    (public_dir / "bread.txt").write_text(_PUBLIC, encoding="utf-8")

    run_add(secret_dir, db_path=db, incremental=False)
    run_add(public_dir, db_path=db, incremental=False)
    private_slug = resolve_silo_by_path(str(db), secret_dir)
    public_slug = resolve_silo_by_path(str(db), public_dir)
    assert private_slug and public_slug

    # Guard the guard: before the flag, the probe must reach the private silo,
    # or every exclusion assertion below would pass vacuously.
    before = run_retrieve(query=_PROBE, n_results=10, db_path=str(db))
    assert private_slug in {c.get("silo") for c in before.get("chunks", [])}

    set_silo_private(str(db), private_slug, True)

    import mcp_server

    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    monkeypatch.setattr(mcp_server, "_release_chroma", lambda: None)
    monkeypatch.setattr(mcp_server, "_emit_query_audit", lambda **_k: None)
    return mcp_server, str(db), private_slug, public_slug


def _assert_no_private(chunks: list[dict], private_slug: str) -> None:
    silos = {c.get("silo", "") for c in chunks}
    assert not {s for s in silos if s == private_slug or s.startswith(f"{private_slug}-")}, (
        f"private silo leaked into unscoped retrieval: {sorted(silos)}"
    )
    assert not any("PRIVATE-MARKER-7f3a" in (c.get("text") or "") for c in chunks)


def test_full_query_personal_knowledge_unscoped(indexed) -> None:
    mcp, _db, private_slug, _ = indexed
    out = mcp.query_personal_knowledge(_PROBE, n_results=20)
    assert "error" not in out, out.get("error")
    _assert_no_private(out.get("chunks", []), private_slug)
    assert private_slug in out.get("excluded_private_silos", [])


def test_full_multi_query_knowledge_unscoped(indexed) -> None:
    mcp, _db, private_slug, _ = indexed
    out = mcp.multi_query_knowledge([_PROBE, "tax return W-2 wages"], n_results=10)
    assert "error" not in out, out.get("error")
    _assert_no_private(out.get("chunks", []), private_slug)


def test_full_find_files_unscoped(indexed) -> None:
    mcp, _db, private_slug, public_slug = indexed
    out = mcp.find_files()
    silos = {f.get("silo") for f in out.get("files", [])}
    assert private_slug not in silos
    assert public_slug in silos


def test_full_exact_slug_still_reaches_private(indexed) -> None:
    mcp, _db, private_slug, _ = indexed
    out = mcp.query_personal_knowledge(_PROBE, silo=private_slug, n_results=5)
    assert {c.get("silo") for c in out.get("chunks", [])} == {private_slug}


def test_display_name_does_not_open_private_silo(indexed) -> None:
    mcp, _db, private_slug, _ = indexed
    out = mcp.query_personal_knowledge(_PROBE, silo="Tax", n_results=5)
    assert out.get("chunks") == []
    assert "exact slug" in (out.get("error") or "")


def test_lite_silo_roster_hides_private(indexed) -> None:
    mcp, _db, private_slug, public_slug = indexed
    out = mcp.silo_roster()
    slugs = {row["slug"] for row in out["silos"]}
    assert private_slug not in slugs
    assert public_slug in slugs
    assert out["private_silos_hidden"] == 1
    assert private_slug not in str(out)


def test_lite_retrieve_knowledge_public_and_exact_private(indexed) -> None:
    mcp, _db, private_slug, public_slug = indexed
    public = mcp.retrieve_knowledge(_PROBE, silo=public_slug, n_results=8)
    assert "error" not in public, public.get("error")
    assert not any("PRIVATE-MARKER-7f3a" in c.get("text", "") for c in public["chunks"])

    exact = mcp.retrieve_knowledge(_PROBE, silo=private_slug, n_results=3)
    assert exact.get("chunks"), "exact slug is the consent signal; it must still return chunks"

    by_name = mcp.retrieve_knowledge(_PROBE, silo="Tax", n_results=3)
    assert by_name.get("chunks") == [] and by_name.get("error")


def test_private_artifact_stream_is_excluded(indexed) -> None:
    """Derived chunks under "<slug>-artifacts" must not slip past a $nin on the parent."""
    mcp, db, private_slug, _ = indexed
    from chroma_client import get_client
    from constants import LLMLI_COLLECTION
    from embeddings import get_embedding_function

    coll = get_client(db).get_or_create_collection(
        name=LLMLI_COLLECTION, embedding_function=get_embedding_function(batch_size=1)
    )
    coll.add(
        ids=["artifact-leak-probe"],
        documents=[_SECRET],
        metadatas=[{"silo": f"{private_slug}-artifacts", "source": "artifact.md", "doc_type": "artifact"}],
    )
    out = mcp.query_personal_knowledge(_PROBE, n_results=20)
    _assert_no_private(out.get("chunks", []), private_slug)
