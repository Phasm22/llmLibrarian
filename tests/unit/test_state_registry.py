import json

from ingest import _file_manifest_path
from state import (
    get_last_failures,
    list_silos,
    remove_manifest_silo,
    remove_silo,
    resolve_silo_by_path,
    resolve_silo_prefix,
    resolve_silo_to_slug,
    set_last_failures,
    update_silo,
)


def test_list_silos_empty_registry(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    assert list_silos(db) == []


def test_update_silo_and_list_silos_roundtrip(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    update_silo(db, "tax-1234abcd", "/tmp/tax", 10, 120, "2026-02-09T00:00:00+00:00", display_name="Tax")
    rows = list_silos(db)
    assert len(rows) == 1
    assert rows[0]["slug"] == "tax-1234abcd"
    assert rows[0]["display_name"] == "Tax"
    assert rows[0]["files_indexed"] == 10


def test_update_silo_persists_language_stats(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    stats = {"by_ext": {".py": 4}, "sample_paths": {".py": ["a.py"]}}
    update_silo(
        db,
        "code-11111111",
        "/tmp/code",
        4,
        40,
        "2026-02-09T00:00:00+00:00",
        display_name="Code",
        language_stats=stats,
    )
    row = list_silos(db)[0]
    assert row["language_stats"]["by_ext"][".py"] == 4


def test_resolve_silo_to_slug_by_slug_and_display_name(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    update_silo(db, "stuff-aaaaaaaa", "/tmp/stuff", 3, 30, "2026-02-09T00:00:00+00:00", display_name="Stuff")
    assert resolve_silo_to_slug(db, "stuff-aaaaaaaa") == "stuff-aaaaaaaa"
    assert resolve_silo_to_slug(db, "Stuff") == "stuff-aaaaaaaa"


def test_resolve_silo_prefix_unique_and_ambiguous(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    update_silo(db, "alpha-11111111", "/tmp/a", 1, 1, "2026-02-09T00:00:00+00:00", display_name="Alpha")
    update_silo(db, "beta-22222222", "/tmp/b", 1, 1, "2026-02-09T00:00:00+00:00", display_name="Beta")
    assert resolve_silo_prefix(db, "alpha-") == "alpha-11111111"
    assert resolve_silo_prefix(db, "a") == "alpha-11111111"
    assert resolve_silo_prefix(db, "") is None


def test_resolve_silo_by_path_matches_resolved_path(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    source = tmp_path / "my docs"
    source.mkdir()
    update_silo(
        db,
        "docs-aaaaaaaa",
        str(source.resolve()),
        2,
        9,
        "2026-02-09T00:00:00+00:00",
        display_name="Docs",
    )
    assert resolve_silo_by_path(db, source) == "docs-aaaaaaaa"


def test_remove_silo_by_display_name(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    update_silo(db, "tax-aaaaaaaa", "/tmp/tax", 1, 1, "2026-02-09T00:00:00+00:00", display_name="Tax")
    removed = remove_silo(db, "Tax")
    assert removed == "tax-aaaaaaaa"
    assert list_silos(db) == []


def test_remove_silo_unknown_returns_none(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    assert remove_silo(db, "missing") is None


def test_set_and_get_last_failures_roundtrip(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    failures = [{"path": "/tmp/a.txt", "error": "boom"}]
    set_last_failures(db, failures)
    assert get_last_failures(db) == failures


def test_get_last_failures_missing_file_returns_empty(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    assert get_last_failures(db) == []


def test_remove_manifest_silo_no_file_is_safe(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    remove_manifest_silo(db, "missing")
    assert True


def test_remove_manifest_silo_removes_only_target_slug(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    manifest_path = _file_manifest_path(db)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "silos": {
                    "alpha-1": {"path": "/tmp/a", "files": {}},
                    "beta-2": {"path": "/tmp/b", "files": {}},
                }
            }
        ),
        encoding="utf-8",
    )
    remove_manifest_silo(db, "alpha-1")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "alpha-1" not in data["silos"]
    assert "beta-2" in data["silos"]
