import chromadb
from chromadb.config import Settings

from ingest import run_add
from query.core import run_ask
from state import resolve_silo_by_path


def test_income_year_query_returns_2024_line9(tmp_path):
    root = tmp_path / "Tax"
    (root / "2021").mkdir(parents=True)
    (root / "2024").mkdir(parents=True)
    (root / "2021" / "2021_TaxReturn.txt").write_text("Form 1040\nline 9: 99,999.\n", encoding="utf-8")
    (root / "2024" / "2024 Federal Income Tax Return.txt").write_text("Form 1040\nline 9: 7,522.\n", encoding="utf-8")

    db_path = tmp_path / "db"
    files_indexed, failures = run_add(root, db_path=db_path, allow_cloud=True)
    assert files_indexed == 2
    assert failures == 0
    slug = resolve_silo_by_path(db_path, root)
    assert slug is not None

    # Verify retrieval only uses 2024 content and returns the correct value.
    out = run_ask(
        archetype_id=None,
        query="what was my income in 2024",
        db_path=db_path,
        silo=slug,
        no_color=True,
        use_reranker=False,
    )
    assert "Form 1040 line 9 (2024): 7,522." in out
    assert "99,999" not in out

    # Sanity: 2024 chunk exists in collection
    client = chromadb.PersistentClient(path=str(db_path), settings=Settings(anonymized_telemetry=False))
    coll = client.get_or_create_collection(name="llmli")
    result = coll.get(where={"silo": slug}, include=["metadatas"])
    assert any("2024" in (m or {}).get("source", "") for m in result.get("metadatas") or [])
