import zipfile
from pathlib import Path

import ingest
from ingest import process_one_file, process_zip_to_chunks
from processors import ExtractedText


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

    def _fake_extract(self, data, source_path):
        assert data == b"fake-image"
        assert source_path == str(p.resolve())
        return ExtractedText(
            "Form W-2 Wage and Tax Statement\nEmployer: YMCA\nBox 1 of W-2: 4,626.76",
            {"ocr_backend": "vision", "ocr_mode": "image_file"},
        )

    monkeypatch.setattr(ingest.ImageProcessor, "extract", _fake_extract)

    chunks = process_one_file(p, "image")
    assert len(chunks) == 1
    assert "4,626.76" in chunks[0][1]
    assert chunks[0][2]["ocr_backend"] == "vision"
    assert chunks[0][2]["ocr_mode"] == "image_file"


def test_process_zip_to_chunks_image_uses_ocr_processor(monkeypatch, tmp_path: Path):
    zpath = tmp_path / "bundle.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("images/paystub.png", b"fake-image")

    def _fake_extract(self, data, source_path):
        assert data == b"fake-image"
        assert source_path.endswith("bundle.zip > images/paystub.png")
        return ExtractedText("Gross Pay $4,626.76", {"ocr_backend": "vision", "ocr_mode": "image_file"})

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
    assert len(chunks) == 1
    assert "4,626.76" in chunks[0][1]
    assert chunks[0][2]["ocr_backend"] == "vision"
    assert chunks[0][2]["ocr_mode"] == "image_file"


def test_capabilities_reports_image_ocr_and_extensions(monkeypatch):
    monkeypatch.setattr("processors.ocr_backend_chain_for_capabilities", lambda: ["vision", "paddleocr", "tesseract"])
    out = ingest.get_capabilities_text()
    assert ".png" in out
    assert ".heic" in out
    assert "Image OCR: yes (vision -> paddleocr -> tesseract)" in out
