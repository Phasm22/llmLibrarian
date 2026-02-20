import builtins
import io
import sys
import types
from pathlib import Path

import pytest

import processors
from processors import DOCXProcessor, PDFProcessor, PPTXProcessor, TextProcessor, XLSXProcessor


def _reset_paddle_cache() -> None:
    processors._PADDLE_OCR_ENGINE = None
    processors._PADDLE_OCR_INIT_ATTEMPTED = False
    processors._PADDLE_OCR_USE_ANGLE = None


def test_log_processor_event_default_warn_level_suppresses_info(monkeypatch, capsys):
    monkeypatch.delenv("LLMLIBRARIAN_PROCESSOR_LOG_LEVEL", raising=False)
    processors._log_processor_event("INFO", "info msg")
    processors._log_processor_event("WARN", "warn msg")
    err = capsys.readouterr().err
    assert "info msg" not in err
    assert "warn msg" in err


def test_log_processor_event_info_level_emits_info(monkeypatch, capsys):
    monkeypatch.setenv("LLMLIBRARIAN_PROCESSOR_LOG_LEVEL", "INFO")
    processors._log_processor_event("INFO", "info msg")
    err = capsys.readouterr().err
    assert "info msg" in err


def test_paddleocr_available_requires_paddle(monkeypatch):
    def _fake_find_spec(name):
        if name == "paddleocr":
            return object()
        if name == "paddle":
            return None
        return None

    monkeypatch.setattr(processors.importlib.util, "find_spec", _fake_find_spec)
    assert processors._paddleocr_available() is False


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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("1", True),
        ("true", True),
        ("yes", True),
    ],
)
def test_ocr_preprocess_enabled_flag(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("LLMLIBRARIAN_OCR_PREPROCESS", raising=False)
    else:
        monkeypatch.setenv("LLMLIBRARIAN_OCR_PREPROCESS", raw)
    assert processors._ocr_preprocess_enabled() is expected


@pytest.mark.parametrize(("env_value", "expected_dpi"), [("0", 300), ("1", 400)])
def test_ocr_pdf_page_text_uses_expected_dpi(monkeypatch, env_value, expected_dpi):
    calls: list[int] = []

    class _FakePixmap:
        def tobytes(self, fmt):
            assert fmt == "png"
            return b"png-bytes"

    class _FakePage:
        number = 0

        def get_pixmap(self, dpi, alpha):
            assert alpha is False
            calls.append(dpi)
            return _FakePixmap()

    monkeypatch.setenv("LLMLIBRARIAN_OCR_PREPROCESS", env_value)
    monkeypatch.setattr(processors, "_paddleocr_available", lambda: False)
    monkeypatch.setattr(processors, "_tesseract_available", lambda: False)
    text, backend = processors._ocr_pdf_page_text(_FakePage(), "scan.pdf")
    assert calls == [expected_dpi]
    assert text is None
    assert backend is None


def test_get_paddle_ocr_engine_uses_angle_mode_from_env(monkeypatch):
    calls: list[dict] = []

    class _FakePaddleOCR:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    fake_module = types.SimpleNamespace(PaddleOCR=_FakePaddleOCR)
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)
    monkeypatch.setattr(processors, "_paddleocr_available", lambda: True)

    _reset_paddle_cache()
    monkeypatch.setenv("LLMLIBRARIAN_OCR_PREPROCESS", "0")
    processors._get_paddle_ocr_engine()
    monkeypatch.setenv("LLMLIBRARIAN_OCR_PREPROCESS", "1")
    processors._get_paddle_ocr_engine()

    assert calls[0]["use_angle_cls"] is False
    assert calls[1]["use_angle_cls"] is True


def test_get_paddle_ocr_engine_falls_back_when_show_log_is_unsupported(monkeypatch):
    calls: list[dict] = []

    class _FakePaddleOCR:
        def __init__(self, **kwargs):
            calls.append(kwargs)
            if "show_log" in kwargs:
                raise TypeError("Unknown argument: show_log")

    fake_module = types.SimpleNamespace(PaddleOCR=_FakePaddleOCR)
    monkeypatch.setitem(sys.modules, "paddleocr", fake_module)
    monkeypatch.setattr(processors, "_paddleocr_available", lambda: True)
    _reset_paddle_cache()
    monkeypatch.setenv("LLMLIBRARIAN_OCR_PREPROCESS", "0")
    engine = processors._get_paddle_ocr_engine()

    assert engine is not None
    assert len(calls) == 2
    assert "show_log" in calls[0]
    assert "show_log" not in calls[1]


def test_ocr_with_paddle_passes_cls_flag_from_env(monkeypatch):
    seen_cls: list[bool] = []

    class _FakeEngine:
        def ocr(self, _path, cls):
            seen_cls.append(cls)
            return [[None, ("hello", 0.99)]]

    monkeypatch.setattr(processors, "_get_paddle_ocr_engine", lambda: _FakeEngine())
    monkeypatch.setenv("LLMLIBRARIAN_OCR_PREPROCESS", "1")
    out = processors._ocr_with_paddle(b"image")
    assert out == "hello"
    assert seen_cls == [True]


def test_ocr_with_tesseract_uses_psm6_when_preprocess_disabled(monkeypatch):
    psms: list[str] = []

    class _Proc:
        returncode = 0
        stderr = ""

    def _fake_run(cmd, capture_output, text):
        assert capture_output is True
        assert text is True
        psm = cmd[-1]
        psms.append(psm)
        out_path = Path(cmd[2]).with_suffix(".txt")
        out_path.write_text(f"psm-{psm}", encoding="utf-8")
        return _Proc()

    monkeypatch.setenv("LLMLIBRARIAN_OCR_PREPROCESS", "0")
    monkeypatch.setattr(processors, "_tesseract_available", lambda: True)
    monkeypatch.setattr(processors.subprocess, "run", _fake_run)

    out = processors._ocr_with_tesseract(b"image")
    assert out == "psm-6"
    assert psms == ["6"]


def test_ocr_with_tesseract_competes_psm6_vs_psm11_when_preprocess_enabled(monkeypatch):
    psms: list[str] = []
    events: list[dict] = []

    class _Proc:
        returncode = 0
        stderr = ""

    def _capture_event(level, message, **fields):
        payload = {"level": level, "message": message}
        payload.update(fields)
        events.append(payload)

    def _fake_run(cmd, capture_output, text):
        assert capture_output is True
        assert text is True
        psm = cmd[-1]
        psms.append(psm)
        out_path = Path(cmd[2]).with_suffix(".txt")
        if psm == "6":
            out_path.write_text("%%%###", encoding="utf-8")
        else:
            out_path.write_text("Gross Pay $4,626.76", encoding="utf-8")
        return _Proc()

    monkeypatch.setenv("LLMLIBRARIAN_OCR_PREPROCESS", "1")
    monkeypatch.setattr(processors, "_tesseract_available", lambda: True)
    monkeypatch.setattr(processors, "_log_processor_event", _capture_event)
    monkeypatch.setattr(processors.subprocess, "run", _fake_run)

    out = processors._ocr_with_tesseract(b"image")
    assert out == "Gross Pay $4,626.76"
    assert psms == ["6", "11"]
    assert any(
        e.get("message") == "tesseract OCR PSM comparison" and e.get("chosen_psm") == "11"
        for e in events
    )


def test_preprocess_image_for_ocr_returns_original_when_opencv_unavailable(monkeypatch):
    original_import = builtins.__import__
    source = b"source-bytes"

    def _fake_import(name, *args, **kwargs):
        if name == "cv2":
            raise ImportError("cv2 missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    monkeypatch.setattr(processors, "_OCR_PREPROCESS_WARNED", set())
    assert processors._preprocess_image_for_ocr(source) == source


def test_preprocess_image_for_ocr_warns_and_returns_original_on_failure(monkeypatch):
    np = pytest.importorskip("numpy")
    events: list[dict] = []

    class _FakeCV2:
        IMREAD_COLOR = 1
        COLOR_BGR2GRAY = 2

        @staticmethod
        def imdecode(_arr, _mode):
            return np.zeros((4, 4, 3), dtype=np.uint8)

        @staticmethod
        def cvtColor(_image, _code):
            raise RuntimeError("boom")

    def _capture_event(level, message, **fields):
        payload = {"level": level, "message": message}
        payload.update(fields)
        events.append(payload)

    monkeypatch.setitem(sys.modules, "cv2", _FakeCV2)
    monkeypatch.setattr(processors, "_OCR_PREPROCESS_WARNED", set())
    monkeypatch.setattr(processors, "_log_processor_event", _capture_event)
    source = b"source-bytes"
    out = processors._preprocess_image_for_ocr(source)
    assert out == source
    assert any("OCR preprocessing failed" in e.get("message", "") for e in events)


def test_preprocess_image_for_ocr_returns_processed_bytes(monkeypatch):
    np = pytest.importorskip("numpy")
    events: list[dict] = []

    class _FakeCV2:
        IMREAD_COLOR = 1
        COLOR_BGR2GRAY = 2
        THRESH_BINARY = 4
        THRESH_OTSU = 8
        INTER_CUBIC = 16
        BORDER_REPLICATE = 32

        @staticmethod
        def imdecode(_arr, _mode):
            return np.zeros((4, 4, 3), dtype=np.uint8)

        @staticmethod
        def cvtColor(image, _code):
            return image[:, :, 0]

        @staticmethod
        def fastNlMeansDenoising(gray, _dst, _h, _template_window, _search_window):
            return gray

        @staticmethod
        def GaussianBlur(gray, _kernel, _sigma):
            return gray

        @staticmethod
        def threshold(gray, _thresh, _maxv, _mode):
            return 0.0, gray

        @staticmethod
        def minAreaRect(_coords):
            return ((0.0, 0.0), (10.0, 10.0), -2.5)

        @staticmethod
        def getRotationMatrix2D(_center, _angle, _scale):
            return np.zeros((2, 3), dtype=float)

        @staticmethod
        def warpAffine(src, _matrix, _shape, flags, borderMode):
            assert flags == _FakeCV2.INTER_CUBIC
            assert borderMode == _FakeCV2.BORDER_REPLICATE
            return src

        @staticmethod
        def imencode(_suffix, _img):
            return True, np.array([1, 2, 3, 4], dtype=np.uint8)

    def _capture_event(level, message, **fields):
        payload = {"level": level, "message": message}
        payload.update(fields)
        events.append(payload)

    monkeypatch.setitem(sys.modules, "cv2", _FakeCV2)
    monkeypatch.setattr(processors, "_OCR_PREPROCESS_WARNED", set())
    monkeypatch.setattr(processors, "_log_processor_event", _capture_event)

    out = processors._preprocess_image_for_ocr(b"input")
    assert out == b"\x01\x02\x03\x04"
    assert any("OCR preprocessing applied" in e.get("message", "") for e in events)
