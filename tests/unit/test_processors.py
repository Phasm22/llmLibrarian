import builtins
import io

import pytest

import processors
from processors import DOCXProcessor, PDFProcessor, PPTXProcessor, TextProcessor, XLSXProcessor


def test_text_processor_extracts_utf8_text():
    proc = TextProcessor()
    out = proc.extract(b"hello", "a.txt")
    assert out == "hello"


def test_pdf_processor_extracts_pages_with_numbers():
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "hello pdf")
    data = doc.tobytes()
    doc.close()

    proc = PDFProcessor()
    pages = proc.extract(data, "a.pdf")
    assert pages
    text, page_num = pages[0]
    assert page_num == 1
    assert "hello pdf" in text


def test_docx_processor_extracts_text():
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_paragraph("hello docx")
    buf = io.BytesIO()
    d.save(buf)

    proc = DOCXProcessor()
    out = proc.extract(buf.getvalue(), "a.docx")
    assert out is not None
    assert "hello docx" in out


def test_xlsx_processor_extracts_cells():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws["A1"] = "hello"
    ws["B1"] = 42
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()

    proc = XLSXProcessor()
    out = proc.extract(buf.getvalue(), "a.xlsx")
    assert out is not None
    assert "Sheet: Data" in out
    assert "hello" in out


def test_pptx_processor_extracts_slide_text():
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    box = slide.shapes.add_textbox(100, 100, 300, 100)
    box.text = "hello pptx"
    buf = io.BytesIO()
    prs.save(buf)

    proc = PPTXProcessor()
    out = proc.extract(buf.getvalue(), "a.pptx")
    assert out is not None
    assert "Slide 1" in out
    assert "hello pptx" in out


def test_docx_processor_returns_none_when_library_missing(monkeypatch):
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "docx":
            raise ImportError("missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    proc = DOCXProcessor()
    assert proc.extract(b"irrelevant", "a.docx") is None


def test_xlsx_processor_returns_none_when_library_missing(monkeypatch):
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openpyxl":
            raise ImportError("missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    proc = XLSXProcessor()
    assert proc.extract(b"irrelevant", "a.xlsx") is None


def test_pptx_processor_returns_none_when_library_missing(monkeypatch):
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pptx":
            raise ImportError("missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    proc = PPTXProcessor()
    assert proc.extract(b"irrelevant", "a.pptx") is None


def test_pdf_processor_enriches_with_table_hints(monkeypatch):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Form 1040 sample")
    data = doc.tobytes()
    doc.close()

    monkeypatch.setenv("LLMLIBRARIAN_PDF_TABLES", "1")
    monkeypatch.setattr(
        processors,
        "_extract_pdf_tables_by_page",
        lambda _data: [[[["", "9", "7,522."], ["", "11", "7,522."]]]],
    )

    proc = PDFProcessor()
    pages = proc.extract(data, "a.pdf")
    text, _page_num = pages[0]
    assert "line 9: 7,522." in text
    assert "| 9 | 7,522. |" in text
    assert "Form 1040 sample" in text


def test_pdf_processor_falls_back_when_table_extraction_raises(monkeypatch):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "raw only")
    data = doc.tobytes()
    doc.close()

    monkeypatch.setenv("LLMLIBRARIAN_PDF_TABLES", "1")

    def _boom(_data):
        raise RuntimeError("pdfplumber boom")

    monkeypatch.setattr(processors, "_extract_pdf_tables_by_page", _boom)
    proc = PDFProcessor()
    pages = proc.extract(data, "a.pdf")
    text, _page_num = pages[0]
    assert "raw only" in text
    assert "Structured values:" not in text


def test_pdf_processor_skips_table_enrichment_when_disabled(monkeypatch):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "raw only")
    data = doc.tobytes()
    doc.close()

    monkeypatch.setenv("LLMLIBRARIAN_PDF_TABLES", "0")

    def _boom(_data):
        raise AssertionError("table extractor should not be called when disabled")

    monkeypatch.setattr(processors, "_extract_pdf_tables_by_page", _boom)
    proc = PDFProcessor()
    pages = proc.extract(data, "a.pdf")
    text, _page_num = pages[0]
    assert "raw only" in text
    assert "Structured values:" not in text


def test_pdf_processor_uses_paddle_ocr_when_page_text_is_empty(monkeypatch):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()

    monkeypatch.setattr(processors, "_paddleocr_available", lambda: True)
    monkeypatch.setattr(processors, "_tesseract_available", lambda: True)
    monkeypatch.setattr(
        processors,
        "_ocr_with_paddle",
        lambda _img: "Form W-2 Wage and Tax Statement Employer YMCA Box 1: 4,626.76",
    )
    monkeypatch.setattr(
        processors,
        "_ocr_with_tesseract",
        lambda _img: (_ for _ in ()).throw(AssertionError("tesseract should not run when paddle succeeds")),
    )

    proc = PDFProcessor()
    pages = proc.extract(data, "scan.pdf")
    text, page_num = pages[0]
    assert page_num == 1
    assert "OCR text (scan fallback):" in text
    assert "4,626.76" in text


def test_pdf_processor_falls_back_to_tesseract_when_paddle_unavailable(monkeypatch):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()

    monkeypatch.setattr(processors, "_paddleocr_available", lambda: False)
    monkeypatch.setattr(processors, "_tesseract_available", lambda: True)
    monkeypatch.setattr(
        processors,
        "_ocr_with_tesseract",
        lambda _img: "Gross Pay $4,626.76",
    )

    proc = PDFProcessor()
    pages = proc.extract(data, "scan.pdf")
    text, _page_num = pages[0]
    assert "OCR text (scan fallback):" in text
    assert "4,626.76" in text


def test_pdf_processor_keeps_empty_text_when_no_ocr_backend(monkeypatch):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()

    events: list[dict] = []

    def _capture_event(level, message, **fields):
        payload = {"level": level, "message": message}
        payload.update(fields)
        events.append(payload)

    monkeypatch.setattr(processors, "_paddleocr_available", lambda: False)
    monkeypatch.setattr(processors, "_tesseract_available", lambda: False)
    monkeypatch.setattr(processors, "_log_processor_event", _capture_event)

    proc = PDFProcessor()
    pages = proc.extract(data, "scan.pdf")
    text, _page_num = pages[0]
    assert text == ""
    assert any("OCR produced no text" in e.get("message", "") for e in events)
