from __future__ import annotations

from pathlib import Path

import artifacts
from orchestration.ingest import IngestRequest, run_ingest
from state import get_silo_artifact_compile, list_silos, update_silo


def test_compile_artifacts_disabled_without_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("LLMLIBRARIAN_ARTIFACT_SILOS", raising=False)
    result = artifacts.compile_artifacts_for_silo(
        db_path=tmp_path / "db",
        parent_slug="docs-aaaa1111",
        source_path=tmp_path / "docs",
    )
    assert result["status"] == "disabled"


def test_compile_artifacts_writes_registry_fingerprint_and_artifact_silo(tmp_path, monkeypatch):
    db = tmp_path / "db"
    source = tmp_path / "docs"
    source.mkdir(parents=True)
    slug = "docs-aaaa1111"
    update_silo(db, slug, str(source), 1, 1, "2026-05-07T00:00:00+00:00", display_name="Docs")
    monkeypatch.setenv("LLMLIBRARIAN_ARTIFACT_SILOS", slug)
    monkeypatch.setattr(
        artifacts,
        "_load_parent_rows",
        lambda _db, _slug: [
            {
                "id": "a",
                "doc": "Revenue increased to $20.8 billion in the wireless segment.",
                "meta": {"source": str(source / "filing.html"), "line_start": 10},
            }
        ],
    )
    captured = {}

    def _fake_write(_db, artifact_slug, rows):
        captured["slug"] = artifact_slug
        captured["rows"] = rows
        return len(rows)

    monkeypatch.setattr(artifacts, "_write_artifact_rows", _fake_write)
    out = artifacts.compile_artifacts_for_silo(
        db_path=db,
        parent_slug=slug,
        source_path=source,
        display_name="Docs",
    )
    assert out["status"] == "completed"
    assert out["chunks_written"] == 1
    assert captured["slug"] == f"{slug}-artifacts"

    compile_meta = get_silo_artifact_compile(db, slug)
    assert compile_meta is not None
    assert compile_meta.get("fingerprint")
    assert sum(int(v) for v in (compile_meta.get("kind_counts", {}) or {}).values()) == 1
    silos = {row["slug"] for row in list_silos(db)}
    assert f"{slug}-artifacts" in silos


def test_compile_artifacts_skips_when_fingerprint_unchanged(tmp_path, monkeypatch):
    db = tmp_path / "db"
    source = tmp_path / "docs"
    source.mkdir(parents=True)
    slug = "docs-aaaa1111"
    update_silo(db, slug, str(source), 1, 1, "2026-05-07T00:00:00+00:00", display_name="Docs")
    monkeypatch.setenv("LLMLIBRARIAN_ARTIFACT_SILOS", slug)
    rows = [
        {"id": "a", "doc": "Risk factors include macroeconomic volatility.", "meta": {"source": str(source / "filing.html")}}
    ]
    fingerprint = artifacts._fingerprint_rows(rows)
    from state import set_silo_artifact_compile

    set_silo_artifact_compile(db, slug, {"fingerprint": fingerprint, "at": "2026-05-07T00:00:00+00:00", "kind_counts": {}})
    monkeypatch.setattr(artifacts, "_load_parent_rows", lambda _db, _slug: rows)
    monkeypatch.setattr(
        artifacts,
        "_write_artifact_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("write should not be called")),
    )

    out = artifacts.compile_artifacts_for_silo(
        db_path=db,
        parent_slug=slug,
        source_path=source,
    )
    assert out["status"] == "unchanged"


def test_run_ingest_calls_artifact_compile_hook(tmp_path, monkeypatch):
    source = tmp_path / "docs"
    source.mkdir()
    db = tmp_path / "db"
    monkeypatch.setattr("orchestration.ingest.run_add", lambda *_args, **_kwargs: (1, 0))
    monkeypatch.setattr("state.resolve_silo_by_path", lambda *_args, **_kwargs: "docs-aaaa1111")
    monkeypatch.setenv("LLMLIBRARIAN_ARTIFACT_SILOS", "*")
    called = {}

    def _fake_compile(*, db_path, parent_slug, source_path, display_name=None):
        called.update(
            {
                "db_path": db_path,
                "parent_slug": parent_slug,
                "source_path": source_path,
                "display_name": display_name,
            }
        )
        return {"status": "completed"}

    monkeypatch.setattr("artifacts.compile_artifacts_for_silo", _fake_compile)
    result = run_ingest(IngestRequest(path=source, db_path=db))
    assert result.files_indexed == 1
    assert result.failures == 0
    assert result.silo_slug == "docs-aaaa1111"
    assert result.artifact_result == {"status": "completed"}
    assert called["parent_slug"] == "docs-aaaa1111"
