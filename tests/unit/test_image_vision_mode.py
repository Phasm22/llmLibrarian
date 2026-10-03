"""Turning image vision on must re-index a silo's images, and adding a photo
folder with vision off must say so.

2026-10-02: dad-new-car (9 photos) was added with vision off. "Enable image
vision" later flipped the registry flag at 05:47:47Z, but all 22 chunks kept
indexed_at 05:07:58Z and "vision disabled" text: incremental ingest skipped every
unchanged image.
"""
from __future__ import annotations

from ingest import _unchanged_since_manifest
from watch_scan import image_share, vision_off_warning

PREV = {"mtime": 1.0, "size": 10, "hash": "abc"}


def _skip(kind: str, changed: bool, **over) -> bool:
    args = {"mtime": 1.0, "size": 10, "file_hash": "abc", "kind": kind, "image_vision_mode_changed": changed}
    args.update(over)
    return _unchanged_since_manifest(PREV, **args)


def test_unchanged_files_are_skipped():
    assert _skip("image", False)
    assert _skip("text", True)  # vision mode only concerns images


def test_vision_mode_change_reprocesses_images():
    assert not _skip("image", True)


def test_content_changes_are_not_skipped():
    assert not _skip("text", False, file_hash="zzz")
    assert not _skip("text", False, mtime=2.0)
    assert not _unchanged_since_manifest(None, mtime=1.0, size=10, file_hash="abc", kind="text", image_vision_mode_changed=False)


def test_photo_folder_warning(tmp_path):
    for i in range(9):
        (tmp_path / f"DSCN27{i}.jpg").write_bytes(b"x")
    (tmp_path / "notes.md").write_text("x")
    images, total = image_share(tmp_path)
    assert (images, total) == (9, 10)
    assert "9 of 10 files are images" in vision_off_warning(images, total)


def test_mostly_text_folder_has_no_warning():
    assert vision_off_warning(2, 2) is None  # too few to matter
    assert vision_off_warning(10, 40) is None


def test_add_silo_returns_the_warning(monkeypatch, tmp_path):
    import mcp_server

    photos = tmp_path / "Dad New Car"
    photos.mkdir()
    for i in range(4):
        (photos / f"DSCN{i}.jpg").write_bytes(b"x")
    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    monkeypatch.setattr("threading.Thread.start", lambda self: None)  # do not ingest

    out = mcp_server.add_silo(str(photos), confirm=True)
    assert out["status"] == "started"
    assert "image vision is off" in out["warnings"][0]

    assert "warnings" not in mcp_server.add_silo(str(photos), image_vision=True, confirm=True)
