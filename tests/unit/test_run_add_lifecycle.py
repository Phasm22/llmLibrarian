import json
from pathlib import Path

import pytest

from ingest import CloudSyncPathError, _file_manifest_path, run_add
from file_registry import _file_registry_path
from processors import ImageExtractionError
from image_embeddings import ImageEmbeddingError


class _FakeCollection:
    def __init__(self):
        self.delete_calls = []
        self.add_calls = []
        self._metadatas = []

    def add(self, **kwargs):
        self.add_calls.append(kwargs)
        self._metadatas.extend(kwargs.get("metadatas") or [])

    def delete(self, where):
        self.delete_calls.append(where)

    def get(self, **_kwargs):
        return {"metadatas": list(self._metadatas)}


class _FakeClient:
    def __init__(self, coll):
        self.coll = coll

    def get_or_create_collection(self, **_kwargs):
        return self.coll


def _patch_runtime(monkeypatch, coll):
    monkeypatch.setattr("ingest.get_embedding_function", lambda: None)
    monkeypatch.setattr("ingest.chromadb.PersistentClient", lambda *a, **k: _FakeClient(coll))
    monkeypatch.setattr("ingest.load_config", lambda *a, **k: {"limits": {}})


def test_run_add_raises_for_non_directory(tmp_path):
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        run_add(file_path, db_path=tmp_path / "db")


def test_run_add_refuses_symlink_root_by_default(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        run_add(link, db_path=tmp_path / "db", follow_symlinks=False)


def test_run_add_blocks_cloud_path_without_override(monkeypatch, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    monkeypatch.setattr("ingest.is_cloud_sync_path", lambda _path: "OneDrive")
    with pytest.raises(CloudSyncPathError):
        run_add(root, db_path=tmp_path / "db", allow_cloud=False)


def test_run_add_images_default_to_ocr_only_without_vision_model(monkeypatch, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    image_path = root / "dog.jpg"
    image_path.write_bytes(b"fake-image")
    monkeypatch.setattr("ingest.collect_files", lambda *a, **k: [(image_path, "code")])
    called = {"image_embeddings": 0, "vision_model": 0}

    def _fake_embeddings():
        called["image_embeddings"] += 1
        return object()

    def _fake_vision_model():
        called["vision_model"] += 1
        raise AssertionError("vision model should stay off by default")

    monkeypatch.setattr("ingest.ensure_vision_model_ready", _fake_vision_model)
    monkeypatch.setattr("ingest.ensure_image_embedding_adapter_ready", _fake_embeddings)
    monkeypatch.setattr("ingest.process_one_file", lambda *a, **k: [])

    files_indexed, failures = run_add(root, db_path=tmp_path / "db", allow_cloud=True)
    assert files_indexed == 0
    assert failures == 0
    assert called["image_embeddings"] == 1
    assert called["vision_model"] == 0


def test_run_add_hard_fails_when_vision_model_missing_for_images_if_enabled(monkeypatch, tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    image_path = root / "dog.jpg"
    image_path.write_bytes(b"fake-image")
    monkeypatch.setattr("ingest.collect_files", lambda *a, **k: [(image_path, "code")])
    monkeypatch.setattr("ingest.ensure_vision_model_ready", lambda: (_ for _ in ()).throw(ImageExtractionError("missing model")))
    with pytest.raises(ImageExtractionError):
        run_add(root, db_path=tmp_path / "db", allow_cloud=True, image_vision_enabled=True)


def test_run_add_warns_and_skips_when_image_embedding_backend_missing(monkeypatch, tmp_path, capsys):
    """Image embedding backend missing is a soft warning; indexing completes for non-image content."""
    root = tmp_path / "photos"
    root.mkdir()
    image_path = root / "dog.jpg"
    image_path.write_bytes(b"fake-image")
    monkeypatch.setattr("ingest.collect_files", lambda *a, **k: [(image_path, "code")])
    monkeypatch.setattr("ingest.ensure_vision_model_ready", lambda: "llava:test")
    monkeypatch.setattr(
        "ingest.ensure_image_embedding_adapter_ready",
        lambda: (_ for _ in ()).throw(ImageEmbeddingError("missing image backend")),
    )
    # Should NOT raise — image files are skipped gracefully
    run_add(root, db_path=tmp_path / "db", allow_cloud=True)
    captured = capsys.readouterr()
    assert "Image embedding unavailable" in captured.out or "image files will be skipped" in captured.out


def test_run_add_persists_image_vision_enabled(monkeypatch, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.txt").write_text("hello world", encoding="utf-8")
    coll = _FakeCollection()
    _patch_runtime(monkeypatch, coll)

    run_add(root, db_path=tmp_path / "db", allow_cloud=True, image_vision_enabled=True)

    from state import get_silo_image_vision_enabled, resolve_silo_by_path

    slug = resolve_silo_by_path(tmp_path / "db", root)
    assert slug is not None
    assert get_silo_image_vision_enabled(tmp_path / "db", slug) is True


def test_run_add_writes_status_file(monkeypatch, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.txt").write_text("hello world", encoding="utf-8")
    status_file = tmp_path / "status.json"
    coll = _FakeCollection()
    _patch_runtime(monkeypatch, coll)
    monkeypatch.setenv("LLMLIBRARIAN_STATUS_FILE", str(status_file))

    files_indexed, failures = run_add(root, db_path=tmp_path / "db", allow_cloud=True)
    payload = json.loads(status_file.read_text(encoding="utf-8"))
    assert files_indexed >= 1
    assert failures == 0
    assert payload["path"] == str(root.resolve())
    assert payload["files_indexed"] >= 1
    assert payload["failures"] == 0


def test_run_add_reuses_cross_silo_duplicates(monkeypatch, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    f = root / "a.txt"
    f.write_text("hello", encoding="utf-8")
    coll = _FakeCollection()
    _patch_runtime(monkeypatch, coll)
    called = {"process": 0}

    monkeypatch.setattr("ingest.collect_files", lambda *a, **k: [(f, "code")])
    monkeypatch.setattr("ingest.get_file_hash", lambda _p: "hash-1")
    monkeypatch.setattr(
        "ingest._file_registry_get",
        lambda _db, _h: [{"silo": "other", "path": str(f.resolve())}],
    )
    monkeypatch.setattr(
        "ingest._clone_chunks_from_existing_silo",
        lambda **_kwargs: [
            (
                "id-1",
                "hello",
                {
                    "source": str(f.resolve()),
                    "source_path": str(f.resolve()),
                    "mtime": f.resolve().stat().st_mtime,
                    "chunk_hash": "h",
                    "file_id": "a.txt",
                    "line_start": 1,
                    "is_local": 1,
                    "doc_type": "other",
                    "content_extracted": 1,
                    "silo": "docs",
                },
            )
        ],
    )
    monkeypatch.setattr(
        "ingest.process_one_file",
        lambda *a, **k: called.__setitem__("process", called["process"] + 1) or [],
    )

    files_indexed, failures = run_add(root, db_path=tmp_path / "db", allow_cloud=True)
    assert files_indexed == 1
    assert failures == 0
    assert called["process"] == 0


def test_run_add_full_reindex_deletes_existing_silo_before_add(monkeypatch, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    coll = _FakeCollection()
    _patch_runtime(monkeypatch, coll)
    removed = {}

    monkeypatch.setattr("state.resolve_silo_by_path", lambda _db, _path: None)
    monkeypatch.setattr("state.slugify", lambda _name, _path=None: "silo-fixed")
    monkeypatch.setattr("ingest.collect_files", lambda *a, **k: [])
    monkeypatch.setattr("ingest._file_registry_remove_silo", lambda _db, slug: removed.setdefault("slug", slug))

    run_add(root, db_path=tmp_path / "db", allow_cloud=True, incremental=False)
    assert {"silo": "silo-fixed"} in coll.delete_calls
    assert removed["slug"] == "silo-fixed"


def test_run_add_incremental_skips_unchanged_files(monkeypatch, tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    f = root / "a.txt"
    f.write_text("hello", encoding="utf-8")
    stat = f.resolve().stat()

    db_path = tmp_path / "db"
    manifest_path = _file_manifest_path(db_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "silos": {
                    "silo-fixed": {
                        "path": str(root.resolve()),
                        "files": {
                            str(f.resolve()): {
                                "mtime": stat.st_mtime,
                                "size": stat.st_size,
                                "hash": "h1",
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    registry_path = _file_registry_path(db_path)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {
                "by_hash": {
                    "h1": [{"silo": "silo-fixed", "path": str(f.resolve())}],
                }
            }
        ),
        encoding="utf-8",
    )

    coll = _FakeCollection()
    _patch_runtime(monkeypatch, coll)
    called = {"process": 0}
    monkeypatch.setattr("state.resolve_silo_by_path", lambda _db, _path: None)
    monkeypatch.setattr("state.slugify", lambda _name, _path=None: "silo-fixed")
    monkeypatch.setattr("ingest.collect_files", lambda *a, **k: [(f, "code")])
    monkeypatch.setattr("ingest.get_file_hash", lambda _p: "h1")
    monkeypatch.setattr("ingest.process_one_file", lambda *a, **k: called.__setitem__("process", 1) or [])

    files_indexed, failures = run_add(root, db_path=db_path, allow_cloud=True, incremental=True)
    assert files_indexed == 0
    assert failures == 0
    assert called["process"] == 0


def test_run_add_prints_effective_worker_settings(monkeypatch, tmp_path, capsys):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.txt").write_text("hello world", encoding="utf-8")
    coll = _FakeCollection()
    _patch_runtime(monkeypatch, coll)
    monkeypatch.setenv("LLMLIBRARIAN_MAX_WORKERS", "3")
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_WORKERS", "5")
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_DEVICE", "cpu")  # avoid MPS cap
    monkeypatch.delenv("LLMLIBRARIAN_QUIET", raising=False)

    files_indexed, failures = run_add(root, db_path=tmp_path / "db", allow_cloud=True, no_color=True)
    captured = capsys.readouterr()

    assert files_indexed >= 1
    assert failures == 0
    assert "Workers: file=3, embedding=5" in captured.out


def test_run_add_cli_worker_overrides_beat_env(monkeypatch, tmp_path, capsys):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.txt").write_text("hello world", encoding="utf-8")
    coll = _FakeCollection()
    _patch_runtime(monkeypatch, coll)
    monkeypatch.setenv("LLMLIBRARIAN_MAX_WORKERS", "3")
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_WORKERS", "5")
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_DEVICE", "cpu")  # avoid MPS cap
    monkeypatch.delenv("LLMLIBRARIAN_QUIET", raising=False)

    files_indexed, failures = run_add(
        root,
        db_path=tmp_path / "db",
        allow_cloud=True,
        no_color=True,
        workers=7,
        embedding_workers=2,
    )
    captured = capsys.readouterr()

    assert files_indexed >= 1
    assert failures == 0
    assert "Workers: file=7, embedding=2" in captured.out


def test_run_add_prints_image_preflight_and_progress(monkeypatch, tmp_path, capsys):
    root = tmp_path / "photos"
    root.mkdir()
    image_path = root / "cat.jpg"
    image_path.write_bytes(b"fake-image")
    (root / "clip.mp4").write_bytes(b"fake-video")

    coll = _FakeCollection()
    _patch_runtime(monkeypatch, coll)
    monkeypatch.setattr("ingest.ensure_vision_model_ready", lambda: "llava:test")
    monkeypatch.setattr("ingest.ensure_image_embedding_adapter_ready", lambda: object())
    monkeypatch.setattr("ingest._batch_add_image_vectors", lambda *a, **k: None)

    def _fake_process(path, kind, *args, **kwargs):
        assert kind == "image"
        resolved = path.resolve()
        return [
            (
                "id-1",
                "Image summary: Photo image with deferred visual summary; no reliable OCR text.",
                {
                    "source": str(resolved),
                    "source_path": str(resolved),
                    "mtime": resolved.stat().st_mtime,
                    "chunk_hash": "h",
                    "file_id": resolved.name,
                    "is_local": 1,
                    "doc_type": "other",
                    "content_extracted": 1,
                    "source_modality": "image",
                    "record_type": "image_summary",
                    "parent_image_id": "img-1",
                    "summary_status": "deferred",
                    "needs_vision_enrichment": True,
                },
            )
        ]

    monkeypatch.setattr("ingest.process_one_file", _fake_process)
    monkeypatch.delenv("LLMLIBRARIAN_QUIET", raising=False)

    files_indexed, failures = run_add(root, db_path=tmp_path / "db", allow_cloud=True, no_color=True)
    captured = capsys.readouterr()

    assert files_indexed == 1
    assert failures == 0
    assert "Preflight: 1 image | 1 mp4 skipped" in captured.out
    assert "Image progress: 1/1" in captured.out
    assert "deferred summaries=1" in captured.out
    assert "image embeddings complete=1" in captured.out
