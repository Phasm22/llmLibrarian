import json
from pathlib import Path

import pytest

from ingest import CloudSyncPathError, _file_manifest_path, run_add
from file_registry import _file_registry_path


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
