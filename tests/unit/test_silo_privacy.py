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
    assert private_filter_clause(db) == {"silo": {"$nin": ["tax-abc"]}}
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
    assert where == {"silo": {"$nin": ["tax-abc"]}}, where


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
