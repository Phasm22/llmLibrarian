"""Private silos must be unreachable from unscoped retrieval.

These cover the regression that motivated the flag: an unscoped
multi_query_knowledge returned tax and lab-result chunks nobody asked for.
Caller discipline is not a control, so the filter is asserted here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from file_registry import read_visible_manifest
from state import (
    canonical_folder_path,
    current_host,
    is_silo_private,
    list_silos,
    list_visible_silos,
    private_filter_clause,
    private_silo_slugs,
    resolve_silo_by_path,
    set_silo_private,
    update_silo,
)


@pytest.fixture()
def db(tmp_path: Path) -> str:
    root = tmp_path / "db"
    root.mkdir()
    update_silo(str(root), "tax-abc", str(tmp_path / "Tax"), 79, 647, "2026-01-01T00:00:00+00:00", display_name="Tax")
    update_silo(str(root), "recipes-def", str(tmp_path / "Recipes"), 1, 120, "2026-01-01T00:00:00+00:00", display_name="Recipes")
    return str(root)


def test_no_private_silos_means_no_filter(db: str) -> None:
    assert private_silo_slugs(db) == []
    assert private_filter_clause(db) is None


def test_flag_round_trips(db: str) -> None:
    assert set_silo_private(db, "tax-abc", True) is True
    assert is_silo_private(db, "tax-abc") is True
    assert set_silo_private(db, "tax-abc", False) is True
    assert is_silo_private(db, "tax-abc") is False


def test_flag_on_missing_silo_reports_failure(db: str) -> None:
    assert set_silo_private(db, "nope-000", True) is False


def test_filter_clause_excludes_only_private(db: str) -> None:
    set_silo_private(db, "tax-abc", True)
    # Artifact compilation writes "<slug>-artifacts"; a $nin on the parent alone
    # lets those derived chunks through.
    assert private_filter_clause(db) == {"silo": {"$nin": ["tax-abc", "tax-abc-artifacts"]}}
    assert [s["slug"] for s in list_visible_silos(db)] == ["recipes-def"]
    # The full roster still shows it: knowing the corpus exists is allowed.
    assert {s["slug"] for s in list_silos(db)} == {"tax-abc", "recipes-def"}


def test_flag_survives_reindex(db: str) -> None:
    """A reindex must not silently un-private a silo."""
    set_silo_private(db, "tax-abc", True)
    update_silo(db, "tax-abc", "/tmp/Tax", 80, 700, "2026-02-01T00:00:00+00:00", display_name="Tax")
    assert is_silo_private(db, "tax-abc") is True


def test_visible_manifest_drops_private_silo(db: str, tmp_path: Path) -> None:
    manifest = Path(db) / "llmli_file_manifest.json"
    manifest.write_text(json.dumps({"silos": {"tax-abc": {"files": {"a.pdf": {}}}, "recipes-def": {"files": {}}}}))
    set_silo_private(db, "tax-abc", True)

    assert set(read_visible_manifest(db)["silos"]) == {"recipes-def"}
    # Naming the silo is the consent signal — then the full manifest is returned.
    assert set(read_visible_manifest(db, silo="tax-abc")["silos"]) == {"tax-abc", "recipes-def"}


def test_registry_stamps_host(db: str) -> None:
    rows = {s["slug"]: s for s in list_silos(db)}
    assert rows["tax-abc"]["host"] == current_host()


def test_symlinked_path_resolves_to_one_silo(tmp_path: Path) -> None:
    """~/llmLibrarian and the checkout it points at are one folder, so one slug."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert canonical_folder_path(link) == canonical_folder_path(real)

    db = tmp_path / "db"
    db.mkdir()
    update_silo(str(db), "proj-1", str(link), 1, 1, "2026-01-01T00:00:00+00:00")
    # Registered via the symlink, found via the real path — no second silo.
    assert resolve_silo_by_path(str(db), real) == "proj-1"
    assert resolve_silo_by_path(str(db), link) == "proj-1"
    assert list_silos(str(db))[0]["path"] == canonical_folder_path(real)


def test_unscoped_retrieval_where_clause_excludes_private(db: str, monkeypatch: object) -> None:
    """The exclusion must reach the Chroma query, not just the registry helpers."""
    from query import retrieve_locked

    set_silo_private(db, "tax-abc", True)
    captured: list[dict] = []

    class _Coll:
        def query(self, **kw: object) -> dict:
            captured.append(dict(kw))
            return {"documents": [[]], "metadatas": [[]], "distances": [[]], "ids": [[]]}

        def get(self, **kw: object) -> dict:
            return {"documents": [], "metadatas": [], "ids": []}

    class _Client:
        def get_or_create_collection(self, **kw: object) -> _Coll:
            return _Coll()

    monkeypatch.setattr(retrieve_locked, "get_embedding_function", lambda **_: None)
    retrieve_locked.execute_retrieve_chroma_phase(
        db=db,
        intent="LOOKUP",
        query="anything",
        query_for_retrieval="anything",
        silo_slug=None,
        n_stage1=10,
        n_results=5,
        section=None,
        doc_type=None,
        db_path=db,
        get_chroma_client=lambda _db: _Client(),
    )
    assert captured, "expected a Chroma query"
    where = captured[0].get("where")
    assert where == {"silo": {"$nin": ["tax-abc", "tax-abc-artifacts"]}}, where


def test_explicit_silo_still_reaches_private(db: str, monkeypatch: object) -> None:
    """Naming the silo is consent — the $nin must not also apply."""
    from query import retrieve_locked

    set_silo_private(db, "tax-abc", True)
    captured: list[dict] = []

    class _Coll:
        def query(self, **kw: object) -> dict:
            captured.append(dict(kw))
            return {"documents": [[]], "metadatas": [[]], "distances": [[]], "ids": [[]]}

        def get(self, **kw: object) -> dict:
            return {"documents": [], "metadatas": [], "ids": []}

    class _Client:
        def get_or_create_collection(self, **kw: object) -> _Coll:
            return _Coll()

    monkeypatch.setattr(retrieve_locked, "get_embedding_function", lambda **_: None)
    retrieve_locked.execute_retrieve_chroma_phase(
        db=db,
        intent="LOOKUP",
        query="anything",
        query_for_retrieval="anything",
        silo_slug="tax-abc",
        n_stage1=10,
        n_results=5,
        section=None,
        doc_type=None,
        db_path=db,
        get_chroma_client=lambda _db: _Client(),
    )
    assert captured[0].get("where") == {"silo": "tax-abc"}


def test_ask_image_resolves_private_image_only_when_named(db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ask_image reads pixels off disk, so resolving a private silo's photo from an
    # unscoped call would leak it even though no chunk text is returned.
    import mcp_server

    img = tmp_path / "Tax" / "W2_scan.jpeg"
    manifest = {"silos": {"tax-abc": {"files": {str(img): {}}}}}
    monkeypatch.setattr("file_registry._read_file_manifest", lambda _db: manifest)
    monkeypatch.setattr(mcp_server, "_DB_PATH", db)
    set_silo_private(db, "tax-abc", True)

    path, _candidates, err = mcp_server._resolve_indexed_image("W2_scan.jpeg", None)
    assert path is None and "no indexed image" in err
    path, _candidates, err = mcp_server._resolve_indexed_image("W2_scan.jpeg", "tax-abc")
    assert err is None and path == str(img)


def _empty_chroma_client(captured: list[dict]):
    class _Coll:
        def query(self, **kw: object) -> dict:
            captured.append(dict(kw))
            return {"documents": [[]], "metadatas": [[]], "distances": [[]], "ids": [[]]}

        def get(self, **kw: object) -> dict:
            return {"documents": [], "metadatas": [], "ids": []}

    class _Client:
        def get_or_create_collection(self, **kw: object) -> _Coll:
            return _Coll()

    return _Client()


def test_display_name_does_not_open_a_private_silo(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """silo="Tax" is what a model produces by guessing; only the exact slug is
    consent. a30aebf added this refusal; the 2026-10-02 rebase of dev dropped it once."""
    from query import retrieve_locked
    from query.core import run_retrieve

    set_silo_private(db, "tax-abc", True)
    captured: list[dict] = []
    monkeypatch.setattr(retrieve_locked, "get_embedding_function", lambda **_: None)

    res = run_retrieve(
        query="anything",
        silo="Tax",
        n_results=5,
        db_path=db,
        get_chroma_client=lambda _db: _empty_chroma_client(captured),
    )
    assert not captured, "a display name reached the private silo's Chroma rows"
    assert res.get("error") and res.get("chunks") == []


def test_lite_roster_omits_private_slugs(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """silo_roster is the lite profile's only discovery tool and retrieve_knowledge
    accepts any listed slug, so listing a private slug hands out the consent key."""
    import mcp_server

    monkeypatch.setattr(mcp_server, "_DB_PATH", db)
    set_silo_private(db, "tax-abc", True)

    slugs = {row["slug"] for row in mcp_server.silo_roster()["silos"]}
    assert "tax-abc" not in slugs
    assert "recipes-def" in slugs


def _write_ledger(db: str) -> None:
    from tax.ledger import _write_all_rows

    _write_all_rows(
        db,
        [
            {"silo": "tax-abc", "tax_year": 2024, "form_type": "W-2", "field_label": "fixture", "raw_value": "PRIVATE-MARKER-7f3a"},
            {"silo": "recipes-def", "tax_year": 2024, "form_type": "other", "field_label": "fixture", "raw_value": "public"},
        ],
    )


def _tax_query_phase(db: str, silo_slug: str | None, monkeypatch: pytest.MonkeyPatch) -> dict:
    from query import retrieve_locked

    monkeypatch.setattr(retrieve_locked, "get_embedding_function", lambda **_: None)
    return retrieve_locked.execute_retrieve_chroma_phase(
        db=db,
        intent="TAX_QUERY",
        # A non-tax question: "sell" + a year is enough for route_intent to say TAX_QUERY.
        query="how much did my dad sell his old car for in 2024",
        query_for_retrieval="how much did my dad sell his old car for in 2024",
        silo_slug=silo_slug,
        n_stage1=10,
        n_results=5,
        section=None,
        doc_type=None,
        db_path=db,
        get_chroma_client=lambda _db: _empty_chroma_client([]),
    )


def test_unscoped_tax_query_does_not_attach_private_ledger_rows(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The chunk where-clause carries the $nin, but tax_ledger is read from
    tax_ledger.json with silo=None and no privacy filter. On the live DB that file
    holds 296 rows from the private tax silo, attached to any unscoped TAX_QUERY."""
    from query.intent import INTENT_TAX_QUERY, route_intent

    assert route_intent("how much did my dad sell his old car for in 2024") == INTENT_TAX_QUERY
    set_silo_private(db, "tax-abc", True)
    _write_ledger(db)

    res = _tax_query_phase(db, None, monkeypatch)
    leaked = [r for r in res.get("tax_ledger") or [] if "PRIVATE-MARKER" in str(r.get("raw_value"))]
    assert not leaked, f"{len(leaked)} private ledger row(s) attached to an unscoped query"


def test_explicit_silo_still_gets_its_ledger_rows(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Naming the silo is consent: the fix must not strip the ledger from scoped calls."""
    set_silo_private(db, "tax-abc", True)
    _write_ledger(db)

    res = _tax_query_phase(db, "tax-abc", monkeypatch)
    assert any("PRIVATE-MARKER" in str(r.get("raw_value")) for r in res.get("tax_ledger") or [])


def test_ledger_choke_point_drops_private_rows_unless_named(db: str) -> None:
    """load_tax_ledger_rows serves both MCP retrieval and the CLI tax resolver, so
    the filter lives there rather than in each caller."""
    from tax.ledger import load_tax_ledger_rows

    set_silo_private(db, "tax-abc", True)
    _write_ledger(db)

    assert {r["silo"] for r in load_tax_ledger_rows(db, tax_year=2024)} == {"recipes-def"}
    assert {r["silo"] for r in load_tax_ledger_rows(db, silo="tax-abc", tax_year=2024)} == {"tax-abc"}


def test_ask_image_path_is_not_consent_for_a_private_silo(db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_indexed_image also matches silo= against a silo's folder path. A path
    is not the exact slug, so it must not open a private silo's images."""
    import mcp_server

    tax_dir = tmp_path / "Tax"
    img = tax_dir / "W2_scan.jpeg"
    manifest = {"silos": {"tax-abc": {"path": str(tax_dir), "files": {str(img): {}}}}}
    monkeypatch.setattr("file_registry._read_file_manifest", lambda _db: manifest)
    monkeypatch.setattr(mcp_server, "_DB_PATH", db)
    set_silo_private(db, "tax-abc", True)

    path, _candidates, err = mcp_server._resolve_indexed_image("W2_scan.jpeg", str(tax_dir))
    assert path is None and "no indexed image" in err


# --- LLMLIBRARIAN_MCP_PRIVATE_READS=none: an endpoint that never reads private ---


@pytest.fixture()
def none_policy(db: str, monkeypatch: pytest.MonkeyPatch):
    import mcp_server

    set_silo_private(db, "tax-abc", True)
    monkeypatch.setenv("LLMLIBRARIAN_MCP_PRIVATE_READS", "none")
    monkeypatch.setattr(mcp_server, "_DB_PATH", db)
    return mcp_server


def test_default_policy_is_named(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp_server

    monkeypatch.delenv("LLMLIBRARIAN_MCP_PRIVATE_READS", raising=False)
    monkeypatch.setattr(mcp_server, "_DB_PATH", db)
    set_silo_private(db, "tax-abc", True)
    assert mcp_server._private_reads_policy() == "named"
    assert mcp_server._private_read_refusal("tax-abc") is None


@pytest.mark.parametrize("name", ["tax-abc", "Tax"])
def test_none_policy_refuses_private_reads_by_slug_or_name(none_policy, name: str) -> None:
    mcp = none_policy
    assert mcp._private_read_refusal(name)
    assert mcp.query_personal_knowledge("agi", silo=name)["error"]
    assert mcp.multi_query_knowledge(["agi"], silo=name)["error"]
    assert mcp.explain_retrieval("agi", silo=name)["error"]
    assert mcp.inspect_silo(name)["error"]
    assert mcp.find_files(silos=[name])["error"]
    assert mcp._private_read_refusal("recipes-def") is None


def test_none_policy_rosters_give_a_count_not_slugs(none_policy) -> None:
    mcp = none_policy
    roster = mcp.list_silos()
    assert [s["slug"] for s in roster["silos"]] == ["recipes-def"]
    assert roster["private_silo_count"] == 1
    assert "tax-abc" not in str(roster)

    note = mcp._private_scope_note(None)
    assert note["excluded_private_silo_count"] == 1 and "tax-abc" not in str(note)

    assert "tax-abc" not in str(mcp.silo_roster())


def test_none_policy_resolver_never_opens_private_files(none_policy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mcp = none_policy
    img = tmp_path / "Tax" / "W2_scan.jpeg"
    monkeypatch.setattr("file_registry._read_file_manifest", lambda _db: {"silos": {"tax-abc": {"files": {str(img): {}}}}})
    path, _slug, _c, err = mcp._resolve_indexed_file("W2_scan.jpeg", "tax-abc")
    assert path is None and err


def test_endpoint_policy_overrides_process_policy(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lite mount sets its own policy per call; the process default stays named."""
    import mcp_server

    monkeypatch.delenv("LLMLIBRARIAN_MCP_PRIVATE_READS", raising=False)
    token = mcp_server._PRIVATE_READS.set("none")
    try:
        assert mcp_server._private_reads_policy() == "none"
    finally:
        mcp_server._PRIVATE_READS.reset(token)
    assert mcp_server._private_reads_policy() == "named"
