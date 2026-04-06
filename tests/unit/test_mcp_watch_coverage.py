from __future__ import annotations

import json
from pathlib import Path

import jobs_runtime as jobsrt
import pytest

from operations import op_watch_coverage


def _write_llmli_registry(db_dir: Path, entries: dict) -> None:
    db_dir.mkdir(parents=True, exist_ok=True)
    (db_dir / "llmli_registry.json").write_text(json.dumps(entries, indent=2), encoding="utf-8")


@pytest.fixture
def sample_trees(tmp_path: Path):
    """Indexed dir, missing dir path, unindexed dir, extra silo path (no bookmark)."""
    indexed = tmp_path / "indexed"
    indexed.mkdir()
    missing = tmp_path / "missing"
    unindexed = tmp_path / "unindexed"
    unindexed.mkdir()
    orphan = tmp_path / "orphan_silo"
    orphan.mkdir()
    return {"indexed": indexed, "missing": missing, "unindexed": unindexed, "orphan": orphan}


def test_watch_coverage_jobs_and_warnings(tmp_path: Path, sample_trees: dict) -> None:
    pal_home = tmp_path / ".pal"
    pal_home.mkdir()
    db_dir = tmp_path / "my_brain_db"

    slug = "docs-aaaa1111"
    orphan_slug = "orphan-bbbb2222"
    _write_llmli_registry(
        db_dir,
        {
            slug: {
                "slug": slug,
                "display_name": "Docs",
                "path": str(sample_trees["indexed"].resolve()),
                "files_indexed": 1,
                "chunks_count": 1,
                "updated": "2026-01-01T00:00:00+00:00",
            },
            orphan_slug: {
                "slug": orphan_slug,
                "display_name": "Orphan",
                "path": str(sample_trees["orphan"].resolve()),
                "files_indexed": 1,
                "chunks_count": 1,
                "updated": "2026-01-01T00:00:00+00:00",
            },
        },
    )

    reg = {
        "bookmarks": [
            {"path": str(sample_trees["indexed"]), "name": "indexed"},
            {"path": str(sample_trees["missing"]), "name": "missing"},
            {"path": str(sample_trees["unindexed"]), "name": "unindexed"},
        ]
    }
    (pal_home / "registry.json").write_text(json.dumps(reg), encoding="utf-8")

    out = op_watch_coverage(db_dir, pal_home=pal_home)

    assert out["pal_home"] == str(pal_home.resolve())
    assert out["db_path"] == str(db_dir.resolve())
    assert out["daemon"]["installed"] is False
    assert len(out["warnings"]) == 2
    assert [j["slug"] for j in out["watch_jobs"]] == [slug]
    assert out["watch_jobs"][0]["service_name"] == f"io.llmlibrarian.watch.{slug}"

    by_resolved = {b["resolved_path"]: b for b in out["bookmarks"] if b["resolved_path"]}
    assert by_resolved[str(sample_trees["indexed"].resolve())]["would_watch"] is True
    assert by_resolved[str(sample_trees["indexed"].resolve())]["silo_slug"] == slug
    assert by_resolved[str(sample_trees["unindexed"].resolve())]["indexed"] is False

    not_bm = out["indexed_not_bookmarked"]
    assert len(not_bm) == 1
    assert not_bm[0]["slug"] == orphan_slug
    assert out["service_files"] == []


def test_watch_coverage_service_files_when_daemon_metadata_present(
    tmp_path: Path, sample_trees: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    pal_home = tmp_path / ".pal"
    pal_home.mkdir()
    db_dir = tmp_path / "my_brain_db"
    slug = "docs-aaaa1111"
    _write_llmli_registry(
        db_dir,
        {
            slug: {
                "slug": slug,
                "display_name": "Docs",
                "path": str(sample_trees["indexed"].resolve()),
                "files_indexed": 1,
                "chunks_count": 1,
                "updated": "2026-01-01T00:00:00+00:00",
            },
        },
    )
    (pal_home / "registry.json").write_text(
        json.dumps({"bookmarks": [{"path": str(sample_trees["indexed"]), "name": "i"}]}),
        encoding="utf-8",
    )

    manager = jobsrt.supported_service_manager() or "launchd"
    meta = {
        "manager": manager,
        "python_executable": "/tmp/python",
        "pal_path": "/tmp/pal.py",
        "workdir": "/tmp",
        "db_path": str(db_dir.resolve()),
    }
    jobsrt.write_daemon_metadata(pal_home, meta)

    pm = jobsrt.PlatformManager(manager, home=tmp_path)
    unit = pm.desired_path(slug)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("stub", encoding="utf-8")

    real_pm = jobsrt.PlatformManager

    def _pm_fixed_home(m: str, home=None):
        return real_pm(m, home=tmp_path)

    monkeypatch.setattr(jobsrt, "PlatformManager", _pm_fixed_home)

    out = op_watch_coverage(db_dir, pal_home=pal_home)
    assert out["daemon"]["installed"] is True
    assert out["daemon"]["manager"] == manager
    assert len(out["service_files"]) == 1
    assert out["service_files"][0]["slug"] == slug
    assert out["service_files"][0]["unit_file_exists"] is True


def test_watch_coverage_legacy_sources_key(tmp_path: Path, sample_trees: dict) -> None:
    pal_home = tmp_path / ".pal"
    pal_home.mkdir()
    db_dir = tmp_path / "my_brain_db"
    slug = "docs-aaaa1111"
    _write_llmli_registry(
        db_dir,
        {
            slug: {
                "slug": slug,
                "display_name": "Docs",
                "path": str(sample_trees["indexed"].resolve()),
                "files_indexed": 1,
                "chunks_count": 1,
                "updated": "2026-01-01T00:00:00+00:00",
            },
        },
    )
    legacy = {
        "sources": [
            {"path": str(sample_trees["indexed"]), "name": "x", "extra": "ignored"},
        ]
    }
    (pal_home / "registry.json").write_text(json.dumps(legacy), encoding="utf-8")

    out = op_watch_coverage(db_dir, pal_home=pal_home)
    assert len(out["bookmarks"]) == 1
    assert out["bookmarks"][0]["path"] == str(sample_trees["indexed"])
    assert out["watch_jobs"][0]["slug"] == slug
