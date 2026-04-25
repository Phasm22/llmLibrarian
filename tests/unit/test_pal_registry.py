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
