import chromadb
from chromadb.config import Settings

from ingest import run_add
from state import slugify
from constants import LLMLI_COLLECTION


def _count_chunks(db_path, silo_slug):
    client = chromadb.PersistentClient(path=str(db_path), settings=Settings(anonymized_telemetry=False))
    coll = client.get_or_create_collection(name=LLMLI_COLLECTION)
    result = coll.get(where={"silo": silo_slug}, include=["metadatas"])
    return len(result.get("metadatas") or [])


def test_incremental_add_skips_unchanged_and_removes_deleted(tmp_path):
    data_dir = tmp_path / "silo"
    data_dir.mkdir()
    f = data_dir / "a.txt"
    f.write_text("hello", encoding="utf-8")

    db_path = tmp_path / "db"
    run_add(data_dir, db_path=db_path, incremental=True)
    slug = slugify(data_dir.name)
    count1 = _count_chunks(db_path, slug)

    # No changes: should not increase chunk count.
    run_add(data_dir, db_path=db_path, incremental=True)
    count2 = _count_chunks(db_path, slug)
    assert count2 == count1

    # Change file: still one chunk, but should not duplicate.
    f.write_text("hello world", encoding="utf-8")
    run_add(data_dir, db_path=db_path, incremental=True)
    count3 = _count_chunks(db_path, slug)
    assert count3 == count1

    # Remove file: chunks should be deleted.
    f.unlink()
    run_add(data_dir, db_path=db_path, incremental=True)
    count4 = _count_chunks(db_path, slug)
    assert count4 == 0
