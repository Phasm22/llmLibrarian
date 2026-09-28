from __future__ import annotations

import json
from pathlib import Path

from pal_registry import cleanup_stale_registry_entries


def test_cleanup_stale_registry_entries_removes_shorter_duplicate_path_slug(tmp_path: Path):
    source = tmp_path / "journalLinker"
    source.mkdir()
    registry_path = tmp_path / "llmli_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "journallinker": {"path": str(source), "display_name": "journalLinker"},
                "journallinker-397f11d4": {"path": str(source), "display_name": "journalLinker"},
                "notes-aabbccdd": {"path": str(tmp_path / "notes"), "display_name": "notes"},
            },
        ),
        encoding="utf-8",
    )

    changed = cleanup_stale_registry_entries(registry_path)
    cleaned = json.loads(registry_path.read_text(encoding="utf-8"))

    assert changed is True
    assert "journallinker" not in cleaned
    assert "journallinker-397f11d4" in cleaned
    assert "notes-aabbccdd" in cleaned


def test_cleanup_stale_registry_entries_noops_without_duplicates(tmp_path: Path):
    registry_path = tmp_path / "llmli_registry.json"
    registry_path.write_text(
        json.dumps({"notes-aabbccdd": {"path": str(tmp_path / "notes"), "display_name": "notes"}}),
        encoding="utf-8",
    )

    changed = cleanup_stale_registry_entries(registry_path)

    assert changed is False


def test_cleanup_keeps_two_real_silos_that_share_a_path(tmp_path: Path):
    """The 2026-09-14 data loss, as a regression test.

    Both slugs carry a hash suffix, so both are real silos — this is a duplicate
    for a human to resolve with `llmli rm`, not something to delete silently.
    Before the guard, canonicalizing silo paths through symlinks made these two
    report one path, and cleanup deleted `llmlibrarian-46ad0cbe`, orphaning 2467
    chunks in Chroma with nothing pointing at them.
    """
    shared = str(tmp_path / "llmLibrarian")
    registry_path = tmp_path / "llmli_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "llmlibrarian-322853c4": {"path": shared, "chunks_count": 2509},
                "llmlibrarian-46ad0cbe": {"path": shared, "chunks_count": 2467},
            },
        ),
        encoding="utf-8",
    )

    changed = cleanup_stale_registry_entries(registry_path)
    after = json.loads(registry_path.read_text(encoding="utf-8"))

    assert changed is False
    assert set(after) == {"llmlibrarian-322853c4", "llmlibrarian-46ad0cbe"}


def test_cleanup_leaves_legacy_only_duplicates_alone(tmp_path: Path):
    """With nothing hashed, no entry supersedes another — deleting would guess."""
    shared = str(tmp_path / "notes")
    registry_path = tmp_path / "llmli_registry.json"
    registry_path.write_text(
        json.dumps({"notes": {"path": shared}, "notes-old": {"path": shared}}),
        encoding="utf-8",
    )

    assert cleanup_stale_registry_entries(registry_path) is False
    assert set(json.loads(registry_path.read_text(encoding="utf-8"))) == {"notes", "notes-old"}


def test_reading_the_registry_never_deletes_entries(tmp_path: Path):
    """A read must be a read. `pal._read_llmli_registry` used to call cleanup."""
    import pal

    shared = str(tmp_path / "llmLibrarian")
    registry_path = tmp_path / "llmli_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "journallinker": {"path": shared},
                "journallinker-397f11d4": {"path": shared},
            },
        ),
        encoding="utf-8",
    )

    got = pal._read_llmli_registry(tmp_path)

    assert set(got) == {"journallinker", "journallinker-397f11d4"}
    assert set(json.loads(registry_path.read_text(encoding="utf-8"))) == {
        "journallinker",
        "journallinker-397f11d4",
    }, "reading the registry mutated it"


def test_cleanup_still_available_explicitly(tmp_path: Path):
    """Opt-in cleanup still performs the legacy migration."""
    import pal

    shared = str(tmp_path / "llmLibrarian")
    registry_path = tmp_path / "llmli_registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "journallinker": {"path": shared},
                "journallinker-397f11d4": {"path": shared},
            },
        ),
        encoding="utf-8",
    )

    got = pal._read_llmli_registry(tmp_path, cleanup=True)

    assert set(got) == {"journallinker-397f11d4"}
