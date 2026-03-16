from pathlib import Path

import chromadb
import pytest
from chromadb.config import Settings

import ingest
import processors


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sample_images"


def _collection(db_path: Path):
    client = chromadb.PersistentClient(path=str(db_path), settings=Settings(anonymized_telemetry=False))
    return client.get_or_create_collection(name="llmli")


def _image_collection(db_path: Path):
    client = chromadb.PersistentClient(path=str(db_path), settings=Settings(anonymized_telemetry=False))
    return client.get_or_create_collection(name="llmli_image")


@pytest.fixture(autouse=True)
def _patch_multimodal(monkeypatch):
    monkeypatch.setattr("ingest.get_embedding_function", lambda: None)
    monkeypatch.setattr(ingest, "ensure_vision_model_ready", lambda: "llava:test")
    monkeypatch.setattr(processors, "ensure_vision_model_ready", lambda: "llava:test")

    class _FakeAdapter:
        def embed_image_paths(self, image_paths):
            out = []
            for raw in image_paths:
                name = Path(raw).name
                if "8C991D97" in name:
                    out.append([1.0, 0.0, 0.0])
                elif "D39EDAD5" in name:
                    out.append([0.0, 1.0, 0.0])
                else:
                    out.append([0.0, 0.0, 1.0])
            return out

        def embed_texts(self, texts):
            out = []
            for text in texts:
                lowered = (text or "").lower()
                if "dog" in lowered:
                    out.append([1.0, 0.0, 0.0])
                elif "laptop" in lowered or "screen" in lowered or "thousand eyes" in lowered:
                    out.append([0.0, 1.0, 0.0])
                else:
                    out.append([0.0, 0.0, 1.0])
            return out

    monkeypatch.setattr(ingest, "ensure_image_embedding_adapter_ready", lambda: _FakeAdapter())

    def _summary(_image_bytes: bytes, source_path: str, visible_text: str):
        name = Path(source_path).name
        if "8C991D97" in name:
            return ("A blurry black dog being held indoors.", "llava:test")
        if "D39EDAD5" in name:
            return ("A photo of a laptop screen showing a networking article and UI chrome.", "llava:test")
        return ("A social media story overlay with visible caption text.", "llava:test")

    monkeypatch.setattr(processors, "_summarize_image_with_vision_model", _summary)


def test_overlay_fixture_indexes_visible_text_regions(tmp_path):
    db = tmp_path / "db"
    path = FIXTURES / "2022-08-26_DD04BC2B-93B8-45CA-B106-8F57F19A2D70-overlay.png"
    status, _ = ingest.update_single_file(path, db_path=db, silo_slug="photos", allow_cloud=True, update_counts=False)
    assert status == "updated"

    coll = _collection(db)
    rows = coll.get(
        where={"source": str(path.resolve())},
        where_document={"$contains": "MANDALAY BAY"},
        include=["documents", "metadatas"],
    )
    docs = rows.get("documents") or []
    metas = rows.get("metadatas") or []
    assert docs
    assert any((m or {}).get("record_type") == "image_region" for m in metas)


def test_laptop_fixture_indexes_multiple_regions(tmp_path):
    db = tmp_path / "db"
    path = FIXTURES / "2022-10-07_D39EDAD5-DE75-4B31-8D46-20741ABB6257-main.jpg"
    status, _ = ingest.update_single_file(path, db_path=db, silo_slug="photos", allow_cloud=True, update_counts=False)
    assert status == "updated"

    coll = _collection(db)
    rows = coll.get(where={"source": str(path.resolve())}, include=["documents", "metadatas"])
    docs = rows.get("documents") or []
    metas = rows.get("metadatas") or []
    assert len(docs) >= 3
    assert any((m or {}).get("record_type") == "image_summary" for m in metas)
    assert any("thousand eyes" in (doc or "").lower() for doc in docs)


def test_dog_fixture_drops_gibberish_and_keeps_caption(tmp_path):
    db = tmp_path / "db"
    path = FIXTURES / "2022-08-23_8C991D97-B8DD-4960-8850-BDF55B99DE2D-main.jpg"
    status, _ = ingest.update_single_file(path, db_path=db, silo_slug="photos", allow_cloud=True, update_counts=False)
    assert status == "updated"

    coll = _collection(db)
    rows = coll.get(where={"source": str(path.resolve())}, include=["documents", "metadatas"])
    docs = rows.get("documents") or []
    metas = rows.get("metadatas") or []
    assert docs
    assert any("blurry black dog" in (doc or "").lower() for doc in docs)
    assert any((m or {}).get("record_type") == "image_summary" for m in metas)
    assert not any("ehicsy" in (doc or "").lower() for doc in docs)


def test_dog_fixture_populates_image_vector_collection(tmp_path):
    db = tmp_path / "db"
    path = FIXTURES / "2022-08-23_8C991D97-B8DD-4960-8850-BDF55B99DE2D-main.jpg"
    status, _ = ingest.update_single_file(path, db_path=db, silo_slug="photos", allow_cloud=True, update_counts=False)
    assert status == "updated"

    coll = _image_collection(db)
    rows = coll.query(query_embeddings=[[1.0, 0.0, 0.0]], n_results=1, include=["documents", "metadatas", "distances"])
    docs = (rows.get("documents") or [[]])[0] or []
    metas = (rows.get("metadatas") or [[]])[0] or []
    assert docs
    assert "black dog" in docs[0].lower()
    assert metas[0]["record_type"] == "image_vector"
    assert metas[0]["source"] == str(path.resolve())
