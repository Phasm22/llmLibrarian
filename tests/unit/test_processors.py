import builtins
import io

import pytest

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
