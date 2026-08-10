"""The file manifest is the single source of truth for indexed files.

``llmli_file_registry.json`` used to hold the same facts in an inverted shape,
written under a separate lock in a separate call. Nothing reconciled the two and
they drifted. The hash index is now derived from the manifest in memory, so the
two stores cannot disagree — there is only one store.
"""

from __future__ import annotations

import json

import pytest

from file_registry import (
    _file_registry_get,
    _legacy_registry_path,
    _read_file_registry,
    _registry_from_manifest,
    _write_file_manifest,
    get_paths_by_silo,
)
from silo_audit import load_file_registry


def _seed_manifest(db_path, silos):
    db_path.mkdir(parents=True, exist_ok=True)
    _write_file_manifest(db_path, {"silos": silos})


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def test_file_registry_get_is_derived_from_manifest(tmp_path):
    db = tmp_path / "db"
    _seed_manifest(
        db,
        {
            "docs": {
                "files": {
                    "/x/a.md": {"mtime": 1.0, "size": 10, "hash": "h1"},
                    "/x/b.md": {"mtime": 2.0, "size": 20, "hash": "h2"},
                }
            },
            "notes": {
                "files": {
                    "/y/a.md": {"mtime": 3.0, "size": 30, "hash": "h1"},
                }
            },
        },
    )

    assert _file_registry_get(db, "h1") == [
        {"silo": "docs", "path": "/x/a.md"},
        {"silo": "notes", "path": "/y/a.md"},
    ]
    assert _file_registry_get(db, "missing") == []


def test_get_paths_by_silo_reads_manifest_directly(tmp_path):
    db = tmp_path / "db"
    _seed_manifest(
        db,
        {
            "docs": {"files": {"/x/a.md": {"hash": "h1"}, "/x/b.md": {"hash": "h2"}}},
            "empty": {"files": {}},
        },
    )

    assert get_paths_by_silo(db) == {
        "docs": {"/x/a.md", "/x/b.md"},
        "empty": set(),
    }


def test_lookup_order_is_stable_across_calls(tmp_path):
    """The old on-disk list had insertion order; the derived one has manifest
    iteration order. Callers index into the result, so it must not shuffle."""
    db = tmp_path / "db"
    _seed_manifest(
        db,
        {
            "a": {"files": {"/1": {"hash": "h"}}},
            "b": {"files": {"/2": {"hash": "h"}}},
            "c": {"files": {"/3": {"hash": "h"}}},
        },
    )
    first = _file_registry_get(db, "h")
    assert len(first) == 3
    for _ in range(5):
        assert _file_registry_get(db, "h") == first


def test_malformed_manifest_shapes_do_not_raise(tmp_path):
    db = tmp_path / "db"
    _seed_manifest(
        db,
        {
            "ok": {"files": {"/x": {"hash": "h1"}}},
            "silo_not_a_dict": "junk",
            "files_not_a_dict": {"files": "junk"},
            "entry_not_a_dict": {"files": {"/y": "junk"}},
            "no_hash": {"files": {"/z": {"mtime": 1.0}}},
        },
    )
    assert _file_registry_get(db, "h1") == [{"silo": "ok", "path": "/x"}]
    assert _registry_from_manifest({}) == {"by_hash": {}}
    assert _registry_from_manifest({"silos": "junk"}) == {"by_hash": {}}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_derived_registry_cache_tracks_manifest_writes(tmp_path):
    db = tmp_path / "db"
    _seed_manifest(db, {"docs": {"files": {"/x/a.md": {"hash": "old"}}}})

    assert _file_registry_get(db, "old") == [{"silo": "docs", "path": "/x/a.md"}]

    _write_file_manifest(db, {"silos": {"docs": {"files": {"/x/a.md": {"hash": "new"}}}}})

    assert _file_registry_get(db, "old") == []
    assert _file_registry_get(db, "new") == [{"silo": "docs", "path": "/x/a.md"}]


def test_cache_invalidates_on_out_of_band_manifest_change(tmp_path):
    """Another process writing the manifest must not leave us on a stale index.

    In-process invalidation cannot see that write, so the cache key is
    (mtime_ns, size) of the manifest file itself.
    """
    from file_registry import _file_manifest_path

    db = tmp_path / "db"
    _seed_manifest(db, {"docs": {"files": {"/x/a.md": {"hash": "old"}}}})
    assert _file_registry_get(db, "old")

    # Write straight to the file, bypassing every helper — as a second process would.
    manifest_path = _file_manifest_path(db)
    payload = {"silos": {"docs": {"files": {"/x/a.md": {"hash": "external"}}}}}
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    assert _file_registry_get(db, "old") == []
    assert _file_registry_get(db, "external") == [{"silo": "docs", "path": "/x/a.md"}]


def test_missing_manifest_yields_empty_index(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    assert _read_file_registry(db) == {"by_hash": {}}
    assert get_paths_by_silo(db) == {}


# ---------------------------------------------------------------------------
# The retired on-disk registry
# ---------------------------------------------------------------------------


def test_legacy_registry_file_is_removed_on_manifest_write(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    legacy = _legacy_registry_path(db)
    legacy.write_text(
        json.dumps({"by_hash": {"stale-hash": [{"silo": "stale", "path": "/old"}]}}),
        encoding="utf-8",
    )

    _write_file_manifest(db, {"silos": {"docs": {"files": {"/x/a.md": {"hash": "h1"}}}}})

    assert not legacy.exists()
    assert _file_registry_get(db, "stale-hash") == []
    assert _file_registry_get(db, "h1") == [{"silo": "docs", "path": "/x/a.md"}]


def test_manifest_write_succeeds_when_no_legacy_file_exists(tmp_path):
    db = tmp_path / "db"
    _seed_manifest(db, {"docs": {"files": {"/x/a.md": {"hash": "h1"}}}})
    assert not _legacy_registry_path(db).exists()
    assert _file_registry_get(db, "h1")


def test_unlink_failure_does_not_fail_the_manifest_write(tmp_path, monkeypatch):
    """An unwritable DB directory is not a reason to fail an ingest over a file
    nothing reads."""
    db = tmp_path / "db"
    db.mkdir()

    def _boom(self, missing_ok=False):
        raise OSError("read-only file system")

    monkeypatch.setattr("pathlib.Path.unlink", _boom)

    _write_file_manifest(db, {"silos": {"docs": {"files": {"/x/a.md": {"hash": "h1"}}}}})
    assert _file_registry_get(db, "h1") == [{"silo": "docs", "path": "/x/a.md"}]


def test_stale_legacy_file_is_never_read(tmp_path):
    """Even before it is cleaned up, the legacy file must not influence lookups."""
    db = tmp_path / "db"
    _seed_manifest(db, {"docs": {"files": {"/x/a.md": {"hash": "manifest-hash"}}}})
    _legacy_registry_path(db).write_text(
        json.dumps({"by_hash": {"stale-hash": [{"silo": "stale", "path": "/old"}]}}),
        encoding="utf-8",
    )

    assert _file_registry_get(db, "stale-hash") == []
    assert _read_file_registry(db) == {
        "by_hash": {"manifest-hash": [{"silo": "docs", "path": "/x/a.md"}]}
    }
    assert load_file_registry(db) == _read_file_registry(db)


def test_registry_write_helpers_are_gone():
    """They were no-ops after the derive change; leaving them invited callers to
    believe state was being written."""
    import file_registry

    for name in (
        "_write_file_registry",
        "_update_file_registry",
        "_file_registry_add",
        "_file_registry_remove_path",
        "_file_registry_remove_silo",
        "_file_registry_path",
    ):
        assert not hasattr(file_registry, name), name


def test_silo_audit_has_no_second_registry_path_helper():
    import silo_audit

    assert not hasattr(silo_audit, "_file_registry_path")
