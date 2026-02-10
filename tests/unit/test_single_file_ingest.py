from pathlib import Path

from ingest import update_single_file, remove_single_file, _file_manifest_path


class _DummyClient:
    def __init__(self, collection):
        self._collection = collection

    def get_or_create_collection(self, **_kwargs):
        return self._collection


def _patch_ingest_runtime(monkeypatch, mock_collection):
    monkeypatch.setattr("ingest.get_embedding_function", lambda: None)
    monkeypatch.setattr("ingest.chromadb.PersistentClient", lambda *a, **k: _DummyClient(mock_collection))


def test_update_single_file_adds_and_manifest(monkeypatch, mock_collection, db_path: Path, tmp_path: Path):
    _patch_ingest_runtime(monkeypatch, mock_collection)
    target = tmp_path / "note.txt"
    target.write_text("hello world", encoding="utf-8")

    status, path_str = update_single_file(target, db_path=db_path, silo_slug="__self__", allow_cloud=True)
    assert status == "updated"
    assert str(target.resolve()) == path_str

    manifest_path = _file_manifest_path(db_path)
    assert manifest_path.exists()
    data = manifest_path.read_text(encoding="utf-8")
    assert str(target.resolve()) in data
    assert any(name == "add" for name, _kwargs in mock_collection.calls)


def test_update_single_file_skips_when_unchanged(monkeypatch, mock_collection, db_path: Path, tmp_path: Path):
    _patch_ingest_runtime(monkeypatch, mock_collection)
    target = tmp_path / "note.txt"
    target.write_text("hello world", encoding="utf-8")

    status, _ = update_single_file(target, db_path=db_path, silo_slug="__self__", allow_cloud=True)
    assert status == "updated"
    status, _ = update_single_file(target, db_path=db_path, silo_slug="__self__", allow_cloud=True)
    assert status == "unchanged"

    add_calls = [c for c in mock_collection.calls if c[0] == "add"]
    assert len(add_calls) == 1


def test_remove_single_file_deletes(monkeypatch, mock_collection, db_path: Path, tmp_path: Path):
    _patch_ingest_runtime(monkeypatch, mock_collection)
    target = tmp_path / "note.txt"
    target.write_text("hello world", encoding="utf-8")

    status, _ = update_single_file(target, db_path=db_path, silo_slug="__self__", allow_cloud=True)
    assert status == "updated"
    status, _ = remove_single_file(target, db_path=db_path, silo_slug="__self__")
    assert status == "removed"

    delete_calls = [c for c in mock_collection.calls if c[0] == "delete"]
    assert delete_calls
