import builtins
import io
import sys
import types
from pathlib import Path

import pytest

import processors
from processors import DOCXProcessor, ExtractedImage, ImageProcessor, PDFProcessor, PPTXProcessor, TextProcessor, XLSXProcessor


def _reset_paddle_cache() -> None:
    processors._PADDLE_OCR_ENGINE = None
    processors._PADDLE_OCR_INIT_ATTEMPTED = False
    processors._PADDLE_OCR_USE_ANGLE = None


def _reset_vision_cache() -> None:
    processors._VISION_HELPER_BINARY = None
    processors._VISION_HELPER_READY = None
    processors._VISION_MODEL_READY = {}


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
    text = pages[0].text
    page_num = pages[0].page_num
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
    text = pages[0].text
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
    text = pages[0].text
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
    text = pages[0].text
    assert "raw only" in text
    assert "Structured values:" not in text


def test_pdf_processor_uses_paddle_ocr_when_page_text_is_empty(monkeypatch):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()

    monkeypatch.setattr(processors, "_vision_ocr_available", lambda: False)
    monkeypatch.setattr(processors, "_paddleocr_available", lambda: True)
    monkeypatch.setattr(processors, "_tesseract_available", lambda: True)
    monkeypatch.setattr(
        processors,
        "_ocr_with_paddle_path",
        lambda _img: "Form W-2 Wage and Tax Statement Employer YMCA Box 1: 4,626.76",
    )
    monkeypatch.setattr(
        processors,
        "_ocr_with_tesseract_path",
        lambda _img: (_ for _ in ()).throw(AssertionError("tesseract should not run when paddle succeeds")),
    )

    proc = PDFProcessor()
    pages = proc.extract(data, "scan.pdf")
    text = pages[0].text
    page_num = pages[0].page_num
    assert page_num == 1
    assert "OCR text (scan fallback):" in text
    assert "4,626.76" in text
    assert pages[0].meta == {"ocr_backend": "paddleocr", "ocr_mode": "pdf_scan_fallback"}


def test_pdf_processor_falls_back_to_tesseract_when_paddle_unavailable(monkeypatch):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()

    monkeypatch.setattr(processors, "_vision_ocr_available", lambda: False)
    monkeypatch.setattr(processors, "_paddleocr_available", lambda: False)
    monkeypatch.setattr(processors, "_tesseract_available", lambda: True)
    monkeypatch.setattr(
        processors,
        "_ocr_with_tesseract_path",
        lambda _img: "Gross Pay $4,626.76",
    )

    proc = PDFProcessor()
    pages = proc.extract(data, "scan.pdf")
    text = pages[0].text
    assert "OCR text (scan fallback):" in text
    assert "4,626.76" in text
    assert pages[0].meta == {"ocr_backend": "tesseract", "ocr_mode": "pdf_scan_fallback"}


def test_pdf_processor_keeps_empty_text_when_no_ocr_backend(monkeypatch):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    doc.new_page()
    data = doc.tobytes()
    doc.close()

    events: list[dict] = []

    def _capture_event(level, message, **fields):
        payload = {"level": level, "message": message}
        payload.update(fields)
        events.append(payload)

    monkeypatch.setattr(processors, "_vision_ocr_available", lambda: False)
    monkeypatch.setattr(processors, "_paddleocr_available", lambda: False)
    monkeypatch.setattr(processors, "_tesseract_available", lambda: False)
    monkeypatch.setattr(processors, "_log_processor_event", _capture_event)

    proc = PDFProcessor()
    pages = proc.extract(data, "scan.pdf")
    assert len(pages) == 3
    assert all((page.text == "" and page.page_num in (1, 2, 3)) for page in pages)
    warn_events = [e for e in events if "OCR produced no text" in e.get("message", "")]
    assert len(warn_events) == 1
    warning = warn_events[0]
    assert warning.get("message") == "PDF pages have no extractable text and OCR produced no text"
    assert warning.get("path") == "scan.pdf"
    assert warning.get("pages") == [1, 2, 3]
    assert warning.get("page_count") == 3
    assert warning.get("available_ocr_backends") == []


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


def test_available_ocr_backends_prefers_vision_on_darwin(monkeypatch):
    monkeypatch.setattr(processors.sys, "platform", "darwin")
    monkeypatch.setattr(processors, "_vision_ocr_available", lambda: True)
    monkeypatch.setattr(processors, "_paddleocr_available", lambda: True)
    monkeypatch.setattr(processors, "_tesseract_available", lambda: True)
    monkeypatch.delenv("LLMLIBRARIAN_OCR_BACKEND", raising=False)
    assert processors._available_ocr_backends() == ["vision", "paddleocr", "tesseract"]


def test_ocr_backend_none_disables_all_backends(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_OCR_BACKEND", "none")
    assert processors._available_ocr_backends() == []
    assert processors.ocr_backend_chain_for_capabilities() == []


def test_pinned_ocr_backend_does_not_fallback(monkeypatch):
    events: list[dict] = []

    def _capture_event(level, message, **fields):
        payload = {"level": level, "message": message}
        payload.update(fields)
        events.append(payload)

    monkeypatch.setenv("LLMLIBRARIAN_OCR_BACKEND", "paddleocr")
    monkeypatch.setattr(processors, "_paddleocr_available", lambda: True)
    monkeypatch.setattr(processors, "_tesseract_available", lambda: True)
    monkeypatch.setattr(processors, "_ocr_with_paddle_path", lambda _img: None)
    monkeypatch.setattr(
        processors,
        "_ocr_with_tesseract_path",
        lambda _img: (_ for _ in ()).throw(AssertionError("tesseract should not run when backend is pinned")),
    )
    monkeypatch.setattr(processors, "_log_processor_event", _capture_event)

    text, backend = processors._ocr_image_path("/tmp/example.png", "/tmp/example.png", "image_file")
    assert text is None
    assert backend is None
    assert any(e.get("message") == "OCR backend produced no text" and e.get("backend") == "paddleocr" for e in events)


def test_image_processor_returns_structured_image_result(monkeypatch):
    monkeypatch.setattr(
        processors,
        "_ocr_image_file_detailed",
        lambda _data, _source, ocr_mode="image_file": processors._OCRResult(
            text="They really put me to work.\nMANDALAY BAY CONVENTION CENTER",
            backend="vision",
            observations=(
                {"text": "They really put me to work.", "x": 0.1, "y": 0.2, "w": 0.45, "h": 0.08},
                {"text": "MANDALAY BAY CONVENTION CENTER", "x": 0.1, "y": 0.1, "w": 0.55, "h": 0.08},
            ),
            raw_payload={
                "text": "They really put me to work.\nMANDALAY BAY CONVENTION CENTER",
                "observations": [
                    {"text": "They really put me to work.", "x": 0.1, "y": 0.2, "w": 0.45, "h": 0.08},
                    {"text": "MANDALAY BAY CONVENTION CENTER", "x": 0.1, "y": 0.1, "w": 0.55, "h": 0.08},
                ],
            },
        ),
    )
    monkeypatch.setattr(processors, "_summarize_image_with_vision_model", lambda *_args, **_kwargs: ("A short summary", "llava:test"))
    proc = ImageProcessor()
    out = proc.extract(b"image-bytes", "receipt.png")
    assert out is not None
    assert isinstance(out, ExtractedImage)
    assert out.summary.startswith("Image summary: A short summary")
    assert "MANDALAY BAY" in out.visible_text
    assert out.meta == {
        "ocr_backend": "vision",
        "ocr_mode": "image_file",
        "source_modality": "image",
        "summary_status": "eager",
        "needs_vision_enrichment": False,
        "vision_model": "llava:test",
    }
    assert out.regions[0].role == "ocr_block"


def test_image_ocr_signal_assessment_prefers_text_heavy_images():
    signal = processors._image_ocr_signal_assessment(
        processors._OCRResult(
            text="They really put me to work.\nMANDALAY BAY CONVENTION CENTER",
            backend="vision",
            observations=(
                {"text": "They really put me to work.", "x": 0.1, "y": 0.8, "w": 0.6, "h": 0.08},
                {"text": "MANDALAY BAY CONVENTION CENTER", "x": 0.1, "y": 0.68, "w": 0.7, "h": 0.09},
            ),
        )
    )
    assert signal["eager_summary"] is True
    assert signal["keep_visible_text"] is True
    assert signal["ocr_signal_score"] >= 0.55
    assert signal["text_structured"] is True


def test_image_processor_defers_natural_photo_and_suppresses_low_signal_ocr(monkeypatch):
    monkeypatch.setattr(
        processors,
        "_ocr_image_file_detailed",
        lambda _data, _source, ocr_mode="image_file": processors._OCRResult(
            text="3 | q Ba oh) (ehicsy | as 3 i | - - C4",
            backend="vision",
            observations=(),
            raw_payload={"text": "3 | q Ba oh) (ehicsy | as 3 i | - - C4", "observations": []},
        ),
    )
    monkeypatch.setattr(
        processors,
        "_summarize_image_with_vision_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("deferred images should not summarize at ingest")),
    )

    out = ImageProcessor().extract(b"image-bytes", "dog.jpg")
    assert out is not None
    assert out.visible_text == ""
    assert out.meta is not None
    assert out.meta["summary_status"] == "deferred"
    assert out.meta["needs_vision_enrichment"] is True
    assert "deferred visual summary" in out.summary.lower()
    assert out.regions[0].role == "full_frame_summary"
    assert out.regions[0].needs_vision_enrichment is True
    assert out.artifact is not None
    assert out.artifact["ocr_signal_score"] < 0.55
    assert out.artifact["visible_text"] == ""


def test_image_ocr_signal_assessment_defers_low_coverage_vision_gibberish():
    signal = processors._image_ocr_signal_assessment(
        processors._OCRResult(
            text="ros\nnOM noltsiasyagA telriasi)",
            backend="vision",
            observations=(
                {"text": "ros", "x": 0.1, "y": 0.4, "w": 0.08, "h": 0.03},
                {"text": "nOM noltsiasyagA telriasi)", "x": 0.1, "y": 0.34, "w": 0.18, "h": 0.035},
            ),
        )
    )
    assert signal["text_structured"] is False
    assert signal["eager_summary"] is False
    assert signal["keep_visible_text"] is True


def test_image_processor_allows_truly_strong_ocr_only_text_to_go_eager(monkeypatch):
    monkeypatch.setattr(
        processors,
        "_ocr_image_file_detailed",
        lambda _data, _source, ocr_mode="image_file": processors._OCRResult(
            text=(
                "Network latency report for branch office users with packet loss summary and "
                "application connectivity timeline from monitoring console dashboard today"
            ),
            backend="tesseract",
            observations=(),
            raw_payload=None,
        ),
    )
    monkeypatch.setattr(processors, "_summarize_image_with_vision_model", lambda *_args, **_kwargs: ("A dashboard screenshot.", "llava:test"))

    out = ImageProcessor().extract(b"image-bytes", "dashboard.jpg")
    assert out is not None
    assert out.meta is not None
    assert out.meta["summary_status"] == "eager"
    assert out.meta["needs_vision_enrichment"] is False


def test_image_processor_disabled_mode_emits_ocr_only_summary(monkeypatch):
    monkeypatch.setattr(
        processors,
        "_ocr_image_file_detailed",
        lambda _data, _source, ocr_mode="image_file": processors._OCRResult(
            text="Gross Pay $4,626.76",
            backend="vision",
            observations=(
                {"text": "Gross Pay $4,626.76", "x": 0.1, "y": 0.2, "w": 0.4, "h": 0.08},
            ),
            raw_payload={"text": "Gross Pay $4,626.76", "observations": [{"text": "Gross Pay $4,626.76", "x": 0.1, "y": 0.2, "w": 0.4, "h": 0.08}]},
        ),
    )
    monkeypatch.setattr(
        processors,
        "_summarize_image_with_vision_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("disabled mode should never call multimodal vision")),
    )

    out = ImageProcessor().extract(b"image-bytes", "receipt.jpg", enable_multimodal=False)
    assert out is not None
    assert "ocr only" in out.summary.lower()
    assert out.meta["summary_status"] == "disabled"
    assert out.meta["needs_vision_enrichment"] is False
    assert out.regions[0].needs_vision_enrichment is False


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


def test_ocr_quality_assessment_rejects_symbol_heavy_gibberish():
    ok, reasons, stats = processors._ocr_quality_assessment("3 | q Ba oh) (ehicsy | as 3 i | - - C4")
    assert ok is False
    assert reasons
    assert stats["token_count"] >= 1


def test_ocr_quality_assessment_rejects_single_char_dense_photo_noise():
    ok, reasons, _stats = processors._ocr_quality_assessment(
        "us A = x ; a y moe d a ww Aa time ia comp Mite A y ae Dib ESS a YD oy be F aa"
    )
    assert ok is False
    assert "too_many_single_char_tokens" in reasons or "low_meaningful_word_ratio" in reasons


def test_ocr_image_path_detailed_drops_low_quality_fallback(monkeypatch):
    events: list[dict] = []

    def _capture_event(level, message, **fields):
        payload = {"level": level, "message": message}
        payload.update(fields)
        events.append(payload)

    monkeypatch.setattr(processors, "_vision_ocr_available", lambda: False)
    monkeypatch.setattr(processors, "_paddleocr_available", lambda: False)
    monkeypatch.setattr(processors, "_tesseract_available", lambda: True)
    monkeypatch.setattr(processors, "_ocr_with_tesseract_detail_path", lambda _img: processors._OCRResult(text="3 | q Ba oh) (ehicsy | as 3 i", backend="tesseract"))
    monkeypatch.setattr(processors, "_log_processor_event", _capture_event)

    out = processors._ocr_image_path_detailed("/tmp/example.png", "/tmp/example.png", "image_file")
    assert out is None
    assert any(e.get("message") == "OCR text dropped by quality gate" for e in events)


def test_merge_observation_rows_groups_adjacent_lines():
    regions = processors._merge_observation_rows(
        [
            {"text": "Hello", "x": 0.1, "y": 0.8, "w": 0.1, "h": 0.04},
            {"text": "world", "x": 0.22, "y": 0.8, "w": 0.12, "h": 0.04},
            {"text": "Second", "x": 0.1, "y": 0.73, "w": 0.12, "h": 0.04},
            {"text": "line", "x": 0.24, "y": 0.73, "w": 0.08, "h": 0.04},
        ]
    )
    assert len(regions) == 1
    assert "Hello world" in regions[0].text
    assert "Second line" in regions[0].text


def test_ensure_vision_model_ready_requires_vision_capability(monkeypatch):
    class _Show:
        capabilities = ["completion"]

    fake = types.SimpleNamespace(show=lambda _model: _Show())
    monkeypatch.setitem(sys.modules, "ollama", fake)
    monkeypatch.setenv("LLMLIBRARIAN_VISION_MODEL", "text-only")
    _reset_vision_cache()
    with pytest.raises(processors.ImageExtractionError):
        processors.ensure_vision_model_ready()
