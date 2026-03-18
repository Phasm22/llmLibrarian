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


class _MultiClient:
    def __init__(self, collections):
        self._collections = collections

    def get_or_create_collection(self, **kwargs):
        return self._collections[kwargs["name"]]


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


def test_update_single_file_returns_error_without_deleting_existing_state(monkeypatch, mock_collection, db_path: Path, tmp_path: Path):
    _patch_ingest_runtime(monkeypatch, mock_collection)
    target = tmp_path / "note.txt"
    target.write_text("hello world", encoding="utf-8")

    status, _ = update_single_file(target, db_path=db_path, silo_slug="__self__", allow_cloud=True)
    assert status == "updated"
    manifest_before = _file_manifest_path(db_path).read_text(encoding="utf-8")
    delete_count_before = len([c for c in mock_collection.calls if c[0] == "delete"])

    target.write_text("changed content", encoding="utf-8")
    monkeypatch.setattr("ingest.process_one_file", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ollama down")))

    status, path_str = update_single_file(target, db_path=db_path, silo_slug="__self__", allow_cloud=True)
    assert status == "error"
    assert path_str == str(target.resolve())
    assert _file_manifest_path(db_path).read_text(encoding="utf-8") == manifest_before
    delete_count_after = len([c for c in mock_collection.calls if c[0] == "delete"])
    assert delete_count_after == delete_count_before


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


def test_update_single_file_reuses_cross_silo_duplicate(monkeypatch, mock_collection, db_path: Path, tmp_path: Path):
    _patch_ingest_runtime(monkeypatch, mock_collection)
    target = tmp_path / "note.txt"
    target.write_text("hello world", encoding="utf-8")

    monkeypatch.setattr(
        "ingest._file_registry_get",
        lambda _db, _h: [{"silo": "documents", "path": str(target.resolve())}],
    )
    monkeypatch.setattr(
        "ingest._clone_chunks_from_existing_silo",
        lambda **_kwargs: [
            (
                "id-1",
                "hello world",
                {
                    "source": str(target.resolve()),
                    "source_path": str(target.resolve()),
                    "mtime": target.resolve().stat().st_mtime,
                    "chunk_hash": "h",
                    "file_id": "note.txt",
                    "line_start": 1,
                    "is_local": 1,
                    "doc_type": "other",
                    "content_extracted": 1,
                    "silo": "__self__",
                },
            )
        ],
    )
    monkeypatch.setattr(
        "ingest.process_one_file",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not re-extract when clone succeeds")),
    )

    status, _ = update_single_file(target, db_path=db_path, silo_slug="__self__", allow_cloud=True)
    assert status == "updated"
    assert any(name == "add" for name, _kwargs in mock_collection.calls)


def test_update_single_file_image_adds_image_vector(monkeypatch, mock_collection, db_path: Path, tmp_path: Path):
    image_collection = type(mock_collection)()
    monkeypatch.setattr(
        "ingest.chromadb.PersistentClient",
        lambda *a, **k: _MultiClient({"llmli": mock_collection, "llmli_image": image_collection}),
    )
    monkeypatch.setattr("ingest.get_embedding_function", lambda: None)
    monkeypatch.setattr("ingest.ensure_vision_model_ready", lambda: "llava:test")

    class _FakeAdapter:
        def embed_image_paths(self, image_paths):
            assert len(image_paths) == 1
            return [[0.1, 0.2, 0.3]]

    monkeypatch.setattr("ingest.ensure_image_embedding_adapter_ready", lambda: _FakeAdapter())

    target = tmp_path / "dog.jpg"
    target.write_bytes(b"fake-image")

    def _fake_process(*_args, **_kwargs):
        return [
            (
                "summary-id",
                "Image summary: black dog",
                {
                    "source": str(target.resolve()),
                    "source_path": str(target.resolve()),
                    "mtime": target.resolve().stat().st_mtime,
                    "file_id": "dog.jpg",
                    "record_type": "image_summary",
                    "source_modality": "image",
                    "parent_image_id": "abc123",
                    "doc_type": "other",
                    "content_extracted": 1,
                    "is_local": 1,
                },
            )
        ]

    monkeypatch.setattr("ingest.process_one_file", _fake_process)

    status, path_str = update_single_file(target, db_path=db_path, silo_slug="photos", allow_cloud=True)
    assert status == "updated"
    assert path_str == str(target.resolve())
    assert any(name == "add" for name, _kwargs in mock_collection.calls)
    image_add_calls = [kwargs for name, kwargs in image_collection.calls if name == "add"]
    assert image_add_calls
    assert image_add_calls[0]["metadatas"][0]["record_type"] == "image_vector"


def test_update_single_file_returns_error_when_image_model_unavailable(monkeypatch, mock_collection, db_path: Path, tmp_path: Path):
    _patch_ingest_runtime(monkeypatch, mock_collection)
    target = tmp_path / "dog.jpg"
    target.write_bytes(b"fake-image")
    monkeypatch.setattr("ingest.ensure_vision_model_ready", lambda: (_ for _ in ()).throw(RuntimeError("missing model")))

    status, path_str = update_single_file(
        target,
        db_path=db_path,
        silo_slug="photos",
        allow_cloud=True,
        image_vision_enabled=True,
    )
    assert status == "error"
    assert path_str == str(target.resolve())
