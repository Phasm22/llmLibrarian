"""Persisted silo excludes must be merged and enforced at every ingest entry point."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from exclude_enforcement import (
    effective_excludes,
    find_exclude_violations,
    persistable_excludes,
    purge_exclude_violations,
)
from file_registry import _write_file_manifest
from scan_patterns import ADD_DEFAULT_EXCLUDE, exclude_matches, merge_exclude_patterns
from state import get_silo_exclude_patterns, update_silo


def _seed_silo(db: Path, root: Path, slug: str = "recipes-demo", *, excludes: list[str]) -> str:
    db.mkdir(parents=True, exist_ok=True)
    update_silo(
        db,
        slug,
        str(root.resolve()),
        0,
        0,
        "2026-01-01T00:00:00+00:00",
        display_name="Recipes",
        exclude_patterns=excludes,
    )
    return slug


def test_merge_empty_requested_does_not_drop_persisted():
    merged = merge_exclude_patterns(["*.pdf", "pipeline/"], [], None)
    assert "*.pdf" in merged
    assert "pipeline/" in merged


def test_exclude_matches_glob_pdf():
    path = "/Users/me/Documents/Recipes/01-general-cookbooks/Boston.pdf"
    assert exclude_matches(path, "*.pdf")
    assert not exclude_matches(path.replace(".pdf", ".md"), "*.pdf")


def test_effective_excludes_merges_defaults_persisted_and_requested(tmp_path):
    root = tmp_path / "Recipes"
    root.mkdir()
    db = tmp_path / "db"
    slug = _seed_silo(db, root, excludes=["*.pdf", "06-recipes-source/"])
    eff = effective_excludes(db, slug, ["pipeline/"])
    assert "*.pdf" in eff
    assert "06-recipes-source/" in eff
    assert "pipeline/" in eff
    assert "/much thinks/cortex/" in eff  # default
    # Empty requested keeps persisted
    assert "*.pdf" in effective_excludes(db, slug, [])
    assert "*.pdf" in effective_excludes(db, slug, None)


def test_update_silo_empty_exclude_list_does_not_wipe(tmp_path):
    root = tmp_path / "Recipes"
    root.mkdir()
    db = tmp_path / "db"
    slug = _seed_silo(db, root, excludes=["*.pdf"])
    update_silo(
        db,
        slug,
        str(root.resolve()),
        1,
        1,
        "2026-01-02T00:00:00+00:00",
        exclude_patterns=[],
    )
    assert "*.pdf" in get_silo_exclude_patterns(db, slug)


def test_run_add_omitting_exclude_keeps_persisted_pdf_glob(monkeypatch, tmp_path):
    from ingest import run_add

    root = tmp_path / "Recipes"
    root.mkdir()
    md = root / "keep.md"
    md.write_text("# ok", encoding="utf-8")
    pdf = root / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    db = tmp_path / "db"
    slug = _seed_silo(db, root, excludes=["*.pdf"])

    captured: dict[str, list[str]] = {}

    class _FakeCollection:
        def count(self):
            return 1

        def add(self, **kwargs):
            pass

        def delete(self, where):
            pass

        def get(self, **kwargs):
            return {"metadatas": [], "ids": []}

    class _FakeClient:
        def get_or_create_collection(self, **_kwargs):
            return _FakeCollection()

    from contextlib import contextmanager

    @contextmanager
    def _writer(_path):
        yield _FakeClient()

    monkeypatch.setattr("ingest.writer_client", _writer)
    monkeypatch.setattr("ingest.get_embedding_function", lambda **_kw: None)
    monkeypatch.setattr("ingest.validate_embedding_dimension", lambda *a, **k: None)
    monkeypatch.setattr("ingest.get_file_hash", lambda _p: "h1")
    monkeypatch.setattr("ingest.process_one_file", lambda *a, **k: [])
    monkeypatch.setattr("ingest._get_image_collection", lambda *_a, **_k: None)
    monkeypatch.setattr("ingest._wait_until_queryable", lambda *_a, **_k: None)
    monkeypatch.setattr("state.resolve_silo_by_path", lambda _db, _path: slug)
    monkeypatch.setattr("ingest_journal.check_pending", lambda *_a, **_k: set())
    monkeypatch.setattr("ingest_journal.clear_pending", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "exclude_enforcement.purge_exclude_violations",
        lambda *_a, **_k: {"purged": [], "errors": [], "violations": []},
    )

    collected: list[str] = []

    def _fake_collect(root_path, include, exclude, *a, **k):
        captured["exclude"] = list(exclude)
        # Mimic real collect: drop anything matching excludes
        from watch_scan import should_index

        out = []
        for p in Path(root_path).iterdir():
            if p.is_file() and should_index(str(p), include, exclude):
                collected.append(p.name)
                out.append((p.resolve(), "code"))
        return out

    monkeypatch.setattr("ingest.collect_files", _fake_collect)

    run_add(root, db_path=db, allow_cloud=True, exclude_patterns=None)

    assert "*.pdf" in captured["exclude"]
    assert "*.pdf" in get_silo_exclude_patterns(db, slug)
    assert "book.pdf" not in collected
    assert "keep.md" in collected


def test_update_single_file_rejects_pdf_when_glob_persisted(monkeypatch, tmp_path):
    from ingest import update_single_file

    root = tmp_path / "Recipes"
    root.mkdir()
    pdf = root / "Boston.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    db = tmp_path / "db"
    slug = _seed_silo(db, root, excludes=["*.pdf"])

    removed: list[str] = []

    def _fake_remove(path, db_path=None, silo_slug="__self__", update_counts=True):
        removed.append(str(Path(path).resolve()))
        return ("removed", str(Path(path).resolve()))

    monkeypatch.setattr("ingest.remove_single_file", _fake_remove)

    status, path_str = update_single_file(pdf, db_path=db, silo_slug=slug, allow_cloud=True)
    assert status == "removed"
    assert path_str == str(pdf.resolve())
    assert removed == [str(pdf.resolve())]


def test_mcp_add_silo_empty_excludes_keeps_persisted(monkeypatch, tmp_path):
    import mcp_server

    root = tmp_path / "Recipes"
    root.mkdir()
    (root / "a.md").write_text("x", encoding="utf-8")
    db = tmp_path / "db"
    slug = _seed_silo(db, root, excludes=["*.pdf"])
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    monkeypatch.setattr(mcp_server, "_mcp_write_lock_disabled", lambda: True)
    monkeypatch.setattr(mcp_server, "_reindex_lock", MagicMock())
    monkeypatch.setattr(mcp_server, "_release_chroma", lambda: None)
    monkeypatch.setattr(mcp_server, "_mark_background_job_started", lambda *a, **k: None)
    monkeypatch.setattr(mcp_server, "_mark_background_job_finished", lambda *a, **k: None)

    seen: dict = {}

    def _fake_run_ingest(request):
        seen["exclude_patterns"] = request.exclude_patterns
        # Simulate choke point merge the real run_add would do
        seen["effective"] = effective_excludes(db, slug, request.exclude_patterns)
        from orchestration.ingest import IngestResult

        return IngestResult(files_indexed=0, failures=0, silo_slug=slug)

    monkeypatch.setattr("orchestration.ingest.run_ingest", _fake_run_ingest)
    monkeypatch.setattr("state.resolve_silo_by_path", lambda *_a, **_k: slug)

    # Watcher used to pass []; must not wipe or drop *.pdf from effective set.
    out = mcp_server.add_silo(path=str(root), exclude_patterns=[], confirm=True)
    assert out["status"] == "started"

    import time

    for _ in range(50):
        if "effective" in seen:
            break
        time.sleep(0.02)
    assert "*.pdf" in seen["effective"]
    assert seen["exclude_patterns"] is None  # [] normalized to None


def test_pal_watch_initial_add_silo_omits_empty_exclude_list(monkeypatch, tmp_path):
    """Watcher must not send exclude_patterns=[] (that used to widen silos)."""
    import pal

    root = tmp_path / "Recipes"
    root.mkdir()
    db = tmp_path / "db"
    slug = _seed_silo(db, root, excludes=["*.pdf"])

    calls: list[dict] = []

    monkeypatch.setattr(pal, "_mcp_healthcheck_wait", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr(pal, "Observer", MagicMock)

    def _fake_mcp(tool, **kwargs):
        calls.append({"tool": tool, **kwargs})
        return {"status": "started"}

    monkeypatch.setattr(pal, "_mcp_call_sync", _fake_mcp)
    monkeypatch.setattr(pal, "_wait_for_llmli_silo_by_path", lambda *_a, **_k: slug)
    monkeypatch.setattr(pal, "_run_watcher", lambda *_a, **_k: 0)
    monkeypatch.setattr(pal, "_set_process_title", lambda *_a, **_k: None)

    code = pal._pull_watch_path_mode(root, interval=60.0, debounce=1.0, allow_cloud=True, follow_symlinks=False)
    assert code == 0
    add_calls = [c for c in calls if c.get("tool") == "add_silo"]
    assert add_calls
    assert add_calls[0].get("exclude_patterns") is None


def test_find_and_purge_exclude_violations(monkeypatch, tmp_path):
    root = tmp_path / "Recipes"
    root.mkdir()
    pdf = root / "Boston.pdf"
    pdf.write_bytes(b"%PDF")
    md = root / "ok.md"
    md.write_text("hi", encoding="utf-8")
    db = tmp_path / "db"
    slug = _seed_silo(db, root, excludes=["*.pdf"])
    _write_file_manifest(
        db,
        {
            "silos": {
                slug: {
                    "path": str(root.resolve()),
                    "files": {
                        str(pdf.resolve()): {"mtime": 1.0, "size": 4, "hash": "a"},
                        str(md.resolve()): {"mtime": 1.0, "size": 2, "hash": "b"},
                    },
                }
            }
        },
    )

    violations = find_exclude_violations(db, slug)
    assert len(violations) == 1
    assert violations[0]["matched_pattern"] == "*.pdf"
    assert violations[0]["source"].endswith("Boston.pdf")

    removed: list[str] = []

    def _fake_remove(path, db_path=None, silo_slug="__self__", update_counts=True):
        removed.append(str(Path(path).resolve()))
        return ("removed", str(Path(path).resolve()))

    monkeypatch.setattr("ingest.remove_single_file", _fake_remove)
    result = purge_exclude_violations(db, slug)
    assert result["violations_found"] == 1
    assert removed == [str(pdf.resolve())]


def test_op_verify_silo_reports_violations(tmp_path):
    from operations import op_verify_silo

    root = tmp_path / "Recipes"
    root.mkdir()
    pdf = root / "x.pdf"
    pdf.write_bytes(b"%PDF")
    db = tmp_path / "db"
    slug = _seed_silo(db, root, excludes=["*.pdf"])
    _write_file_manifest(
        db,
        {
            "silos": {
                slug: {
                    "path": str(root.resolve()),
                    "files": {str(pdf.resolve()): {"mtime": 1.0, "size": 4, "hash": "a"}},
                }
            }
        },
    )
    out = op_verify_silo(str(db), slug)
    assert out["status"] == "violations"
    assert out["violation_count"] == 1


def test_persistable_excludes_includes_defaults_and_custom(tmp_path):
    root = tmp_path / "Recipes"
    root.mkdir()
    db = tmp_path / "db"
    slug = _seed_silo(db, root, excludes=["*.pdf"])
    pats = persistable_excludes(db, slug, None)
    assert "*.pdf" in pats
    assert any(p in pats for p in ADD_DEFAULT_EXCLUDE)
