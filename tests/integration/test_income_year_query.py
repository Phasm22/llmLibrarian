import chromadb
from chromadb.config import Settings
import pytest

import processors
from processors import ExtractedText
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
    assert "7,522" in out
    assert "99,999" not in out

    # Sanity: 2024 chunk exists in collection
    client = chromadb.PersistentClient(path=str(db_path), settings=Settings(anonymized_telemetry=False))
    coll = client.get_or_create_collection(name="llmli")
    result = coll.get(where={"silo": slug}, include=["metadatas"])
    assert any("2024" in (m or {}).get("source", "") for m in result.get("metadatas") or [])


def test_income_year_query_uses_ocr_for_scanned_employer_w2(tmp_path, monkeypatch):
    fitz = pytest.importorskip("fitz")

    root = tmp_path / "Tax"
    (root / "2025").mkdir(parents=True)
    pdf_path = root / "2025" / "ymca-w2-2025.pdf"

    # Image-only page (no extractable text) to exercise OCR fallback path.
    doc = fitz.open()
    doc.new_page()
    pdf_path.write_bytes(doc.tobytes())
    doc.close()

    monkeypatch.setattr(
        processors,
        "_ocr_pdf_page_text",
        lambda _page, _source: (
            "Form W-2 Wage and Tax Statement\nEmployer: YMCA\nBox 1 of W-2: 4,626.76",
            "mock",
        ),
    )

    db_path = tmp_path / "db"
    files_indexed, failures = run_add(root, db_path=db_path, allow_cloud=True)
    assert files_indexed == 1
    assert failures == 0
    slug = resolve_silo_by_path(db_path, root)
    assert slug is not None

    out = run_ask(
        archetype_id=None,
        query="how much did i make in 2025 at ymca",
        db_path=db_path,
        silo=slug,
        no_color=True,
        use_reranker=False,
    )
    assert "4,626.76" in out
    assert "YMCA" in out or "ymca" in out.lower()
    assert "Sources: 1 source" in out


def test_income_year_query_uses_ocr_for_standalone_image(tmp_path, monkeypatch):
    root = tmp_path / "Tax"
    (root / "2025").mkdir(parents=True)
    image_path = root / "2025" / "ymca-w2-2025.png"
    image_path.write_bytes(b"fake-image")

    monkeypatch.setattr(
        processors.ImageProcessor,
        "extract",
        lambda self, _data, _source, **kwargs: ExtractedText(
            "Form W-2 Wage and Tax Statement\nEmployer: YMCA\nBox 1 of W-2: 4,626.76",
            {"ocr_backend": "vision", "ocr_mode": "image_file"},
        ),
    )

    db_path = tmp_path / "db"
    files_indexed, failures = run_add(root, db_path=db_path, allow_cloud=True)
    assert files_indexed == 1
    assert failures == 0
    slug = resolve_silo_by_path(db_path, root)
    assert slug is not None

    out = run_ask(
        archetype_id=None,
        query="how much did i make in 2025 at ymca",
        db_path=db_path,
        silo=slug,
        no_color=True,
        use_reranker=False,
    )
    assert "4,626.76" in out
    assert "YMCA" in out or "ymca" in out.lower()
    assert "Sources: 1 source" in out
