import chromadb
from chromadb.config import Settings
import pytest

import processors
from constants import LLMLI_COLLECTION
from ingest import run_add
from state import list_silos


def test_pdf_table_enrichment_persists_line_value_hints(tmp_path, monkeypatch):
    fitz = pytest.importorskip("fitz")

    silo_dir = tmp_path / "tax"
    silo_dir.mkdir()
    pdf_path = silo_dir / "form.pdf"

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Form 1040 sample text")
    pdf_path.write_bytes(doc.tobytes())
    doc.close()

    monkeypatch.setenv("LLMLIBRARIAN_PDF_TABLES", "1")
    monkeypatch.setattr(
        processors,
        "_extract_pdf_tables_by_page",
        lambda _data: [[[["", "9", "7,522."], ["", "11", "7,522."]]]],
    )

    db_path = tmp_path / "db"
    files_indexed, failures = run_add(silo_dir, db_path=db_path, allow_cloud=True)
    assert files_indexed == 1
    assert failures == 0

    silos = list_silos(db_path)
    assert silos
    slug = silos[0]["slug"]

    client = chromadb.PersistentClient(path=str(db_path), settings=Settings(anonymized_telemetry=False))
    coll = client.get_or_create_collection(name=LLMLI_COLLECTION)
    result = coll.get(where={"silo": slug}, include=["documents"])
    docs = result.get("documents") or []
    assert docs
    joined = "\n".join(docs)
    assert "line 9: 7,522." in joined
    assert "| 9 | 7,522. |" in joined
