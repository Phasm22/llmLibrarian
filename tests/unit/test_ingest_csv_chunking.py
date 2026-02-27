import zipfile
from pathlib import Path

import ingest
from ingest import process_one_file, process_zip_to_chunks


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
    assert metas[1]["course_code"] == "CS 4500"


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
