import zipfile
from pathlib import Path
import json

import ingest
from ingest import process_one_file, process_zip_to_chunks
from processors import ExtractedImage, ImageRegion


def test_process_one_file_csv_emits_row_chunks(tmp_path: Path):
    p = tmp_path / "rankings.csv"
    p.write_text(
        '"Rank","Restaurant","Sales"\n'
        '"1","Carmine\'s (Times Square)",39080335\n'
        '"2","The Boathouse Orlando",35218364\n',
        encoding="utf-8",
    )

    chunks = process_one_file(p, "code")
    assert len(chunks) == 2
    docs = [c[1] for c in chunks]
    metas = [c[2] for c in chunks]
    assert docs[0].startswith("CSV row 1:")
    assert "Rank=1" in docs[0]
    assert "Restaurant=Carmine's (Times Square)" in docs[0]
    assert metas[0]["row_number"] == 1
    assert metas[0]["line_start"] == 2


def test_process_zip_to_chunks_csv_uses_row_chunking(tmp_path: Path):
    zpath = tmp_path / "bundle.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(
            "data/Top250.csv",
            '"Rank","Restaurant"\n'
            '"1","Aster Grill"\n'
            '"2","Blue Harbor"\n',
        )

    chunks = process_zip_to_chunks(
        zip_path=zpath,
        include=["*.csv"],
        exclude=[],
        max_archive_bytes=10 * 1024 * 1024,
        max_file_bytes=10 * 1024 * 1024,
        max_files_per_zip=100,
        max_extracted_per_zip=10 * 1024 * 1024,
    )
    assert len(chunks) == 2
    docs = [c[1] for c in chunks]
    assert docs[0].startswith("CSV row 1:")
    assert "Rank=1" in docs[0]
    assert "Restaurant=Aster Grill" in docs[0]


def test_process_one_file_pdf_uses_single_extraction_pass(monkeypatch, tmp_path: Path):
    p = tmp_path / "single-pass.pdf"
    p.write_bytes(b"fake-pdf")

    calls = {"extract": 0}

    def _fake_extract(self, data, source_path):
        assert data == b"fake-pdf"
        assert source_path == str(p.resolve())
        calls["extract"] += 1
        return [("Synthetic PDF page text", 1)]

    monkeypatch.setattr(ingest.PDFProcessor, "extract", _fake_extract)

    chunks = process_one_file(p, "pdf")
    assert calls["extract"] == 1
    assert len(chunks) == 1
    assert chunks[0][1] == "Synthetic PDF page text"
    assert chunks[0][2]["page"] == 1


def test_process_one_file_transcript_pdf_emits_course_row_chunks(monkeypatch, tmp_path: Path):
    p = tmp_path / "Uccs_Transcript.pdf"
    p.write_bytes(b"fake-transcript")

    def _fake_extract(self, data, source_path):
        assert data == b"fake-transcript"
        assert source_path == str(p.resolve())
        return [
            (
                "Fall 2023\n"
                "CS 2060 UG C Programming A 4.000\n"
                "CS 4500 UG Operating Systems B+ 3.000\n",
                1,
            )
        ]

    monkeypatch.setattr(ingest.PDFProcessor, "extract", _fake_extract)
    chunks = process_one_file(p, "pdf")
    assert len(chunks) == 2
    docs = [c[1] for c in chunks]
    metas = [c[2] for c in chunks]
    assert docs[0].startswith("Course row: CS 2060")
    assert docs[1].startswith("Course row: CS 4500")
    assert metas[0]["record_type"] == "transcript_row"
    assert metas[0]["course_code"] == "CS 2060"
    assert metas[0]["course_term"] == "Fall 2023"
    assert metas[0]["course_grade"] == "A"
    assert metas[0]["course_credits"] == "4.000"
    assert metas[0]["course_school"] == "University of Colorado Colorado Springs"
    assert metas[0]["course_status"] == "attempted"
    assert metas[0]["student_name"] == ""
    assert metas[1]["course_code"] == "CS 4500"


def test_process_one_file_transcript_pdf_parses_title_first_rows(monkeypatch, tmp_path: Path):
    p = tmp_path / "Uccs_Transcript.pdf"
    p.write_bytes(b"fake-transcript")

    def _fake_extract(self, data, source_path):
        assert data == b"fake-transcript"
        assert source_path == str(p.resolve())
        return [
            (
                "NAME: Jenkins, Tandon Kelvon\n"
                "Fall 2023 CU Colo Springs\n"
                "Programming with C\n"
                "CS 2060\n"
                "3.0\n"
                "B\n"
                "9.00\n"
                "Operating Systems I\n"
                "CS 4500\n"
                "3.0\n"
                "W\n",
                1,
            )
        ]

    monkeypatch.setattr(ingest.PDFProcessor, "extract", _fake_extract)
    chunks = process_one_file(p, "pdf")
    assert len(chunks) == 2
    metas = [c[2] for c in chunks]
    assert metas[0]["course_code"] == "CS 2060"
    assert metas[0]["course_title"] == "Programming with C"
    assert metas[0]["course_grade"] == "B"
    assert metas[0]["course_credits"] == "3.0"
    assert metas[0]["student_name"] == "Tandon Kelvon Jenkins"
    assert metas[0]["course_school"] == "University of Colorado Colorado Springs"
    assert metas[0]["course_status"] == "attempted"
    assert metas[1]["course_code"] == "CS 4500"
    assert metas[1]["course_status"] == "attempted_not_completed"


def test_process_one_file_transcript_pdf_falls_back_to_page_chunk_when_no_rows(monkeypatch, tmp_path: Path):
    p = tmp_path / "Uccs_Transcript.pdf"
    p.write_bytes(b"fake-transcript")

    def _fake_extract(self, data, source_path):
        assert data == b"fake-transcript"
        assert source_path == str(p.resolve())
        return [("Unofficial Transcript Header Only", 1)]

    monkeypatch.setattr(ingest.PDFProcessor, "extract", _fake_extract)
    chunks = process_one_file(p, "pdf")
    assert len(chunks) == 1
    assert chunks[0][1] == "Unofficial Transcript Header Only"
    assert "record_type" not in chunks[0][2]


def test_process_one_file_non_transcript_pdf_does_not_emit_transcript_rows(monkeypatch, tmp_path: Path):
    p = tmp_path / "CS 4930 5930 001 Syllabus Spring 2024-3.pdf"
    p.write_bytes(b"fake-syllabus")

    def _fake_extract(self, data, source_path):
        assert data == b"fake-syllabus"
        assert source_path == str(p.resolve())
        return [("CS 4930 5930: Privacy and Censorship\nCSC 160 - Computer Science Grade: I", 1)]

    monkeypatch.setattr(ingest.PDFProcessor, "extract", _fake_extract)
    chunks = process_one_file(p, "pdf")
    assert len(chunks) == 1
    assert chunks[0][2].get("record_type") is None


def test_process_one_file_image_emits_ocr_metadata(monkeypatch, tmp_path: Path):
    p = tmp_path / "w2.png"
    p.write_bytes(b"fake-image")

    def _fake_extract(self, data, source_path, *, enable_multimodal=True):
        assert data == b"fake-image"
        assert source_path == str(p.resolve())
        assert enable_multimodal is True
        return ExtractedImage(
            summary="Image summary: W-2 wage statement",
            visible_text="Form W-2 Wage and Tax Statement\nEmployer: YMCA\nBox 1 of W-2: 4,626.76",
            regions=(
                ImageRegion(
                    text="Employer: YMCA\nBox 1 of W-2: 4,626.76",
                    role="ocr_block",
                    x=0.1,
                    y=0.2,
                    w=0.3,
                    h=0.2,
                ),
            ),
            meta={"ocr_backend": "vision", "ocr_mode": "image_file", "source_modality": "image", "vision_model": "llava:test"},
        )

    monkeypatch.setattr(ingest.ImageProcessor, "extract", _fake_extract)

    chunks = process_one_file(p, "image")
    assert len(chunks) == 2
    assert chunks[0][2]["record_type"] == "image_summary"
    assert chunks[0][2]["vision_model"] == "llava:test"
    assert chunks[1][2]["record_type"] == "image_region"
    assert chunks[1][2]["ocr_backend"] == "vision"
    assert chunks[1][2]["ocr_mode"] == "image_file"
    assert chunks[1][2]["source_modality"] == "image"
    assert chunks[1][2]["region_role"] == "ocr_block"
    assert "4,626.76" in chunks[1][1]


def test_process_zip_to_chunks_image_uses_ocr_processor(monkeypatch, tmp_path: Path):
    zpath = tmp_path / "bundle.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("images/paystub.png", b"fake-image")

    def _fake_extract(self, data, source_path, *, enable_multimodal=True):
        assert data == b"fake-image"
        assert source_path.endswith("bundle.zip > images/paystub.png")
        assert enable_multimodal is False
        return ExtractedImage(
            summary="Image summary: paystub snippet",
            visible_text="Gross Pay $4,626.76",
            regions=(ImageRegion(text="Gross Pay $4,626.76", role="ocr_block", x=0.0, y=0.0, w=1.0, h=1.0),),
            meta={"ocr_backend": "vision", "ocr_mode": "image_file", "source_modality": "image"},
        )

    monkeypatch.setattr(ingest.ImageProcessor, "extract", _fake_extract)

    chunks = process_zip_to_chunks(
        zip_path=zpath,
        include=["*.png"],
        exclude=[],
        max_archive_bytes=10 * 1024 * 1024,
        max_file_bytes=10 * 1024 * 1024,
        max_files_per_zip=100,
        max_extracted_per_zip=10 * 1024 * 1024,
    )
    assert len(chunks) == 2
    assert chunks[1][2]["ocr_backend"] == "vision"
    assert chunks[1][2]["ocr_mode"] == "image_file"
    assert chunks[1][2]["record_type"] == "image_region"


def test_capabilities_reports_image_ocr_and_extensions(monkeypatch):
    monkeypatch.setattr("processors.ocr_backend_chain_for_capabilities", lambda: ["vision", "paddleocr", "tesseract"])
    monkeypatch.setattr("ingest.image_embedding_backend_name", lambda: "open_clip")
    out = ingest.get_capabilities_text()
    assert ".png" in out
    assert ".heic" in out
    assert "Image OCR: yes (vision -> paddleocr -> tesseract)" in out
    assert "Image summaries: requires LLMLIBRARIAN_VISION_MODEL" in out
    assert "Image embeddings: yes (open_clip -> llmli_image)" in out


def test_chunks_from_image_result_writes_artifact(tmp_path: Path):
    result = ExtractedImage(
        summary="Image summary: dog photo",
        visible_text="",
        regions=(ImageRegion(text="A blurry black dog", role="full_frame_summary", x=0.0, y=0.0, w=1.0, h=1.0, needs_vision_enrichment=True),),
        meta={"source_modality": "image", "vision_model": "llava:test"},
        artifact={"summary": "dog photo"},
    )
    chunks = ingest._chunks_from_image_result(
        "dog.jpg",
        result,
        "/tmp/dog.jpg",
        123.0,
        db_path=tmp_path,
        file_hash="abc123",
    )
    assert len(chunks) == 2
    artifact_path = tmp_path / "image_artifacts" / "abc123.json"
    assert artifact_path.exists()
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["summary"] == "dog photo"
    assert chunks[0][2]["image_artifact_relpath"] == "image_artifacts/abc123.json"
    assert chunks[1][2]["parent_image_id"] == "abc123"


def test_image_vector_from_chunks_uses_summary_metadata():
    chunks = [
        (
            "id-1",
            "Image summary: black dog",
            {
                "source": "/tmp/dog.jpg",
                "source_path": "/tmp/dog.jpg",
                "record_type": "image_summary",
                "source_modality": "image",
                "parent_image_id": "abc123",
                "doc_type": "other",
                "file_id": "dog.jpg",
                "mtime": 123.0,
                "image_artifact_relpath": "image_artifacts/abc123.json",
                "vision_model": "llava:test",
                "needs_vision_enrichment": True,
            },
        )
    ]
    row = ingest._image_vector_from_chunks(source_path="/tmp/dog.jpg", chunks=chunks)
    assert row is not None
    row_id, source_path, doc, meta = row
    assert row_id
    assert source_path == "/tmp/dog.jpg"
    assert "black dog" in doc
    assert meta["record_type"] == "image_vector"
    assert meta["parent_image_id"] == "abc123"
