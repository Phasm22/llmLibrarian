"""
Document processor abstraction for llmLibrarian ingestion.
Each processor handles extraction for a specific file type.
"""
import io
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

# ChunkTuple imported from ingest to avoid circular; use forward reference in type hints.
ChunkTuple = tuple[str, str, dict[str, Any]]


class DocumentProcessor(Protocol):
    """Protocol for document processors. Each handles one file type."""

    def extract(self, data: bytes, source_path: str) -> str | list[tuple[str, int]] | None:
        """Extract text from file bytes. Returns:
        - str for plain text content
        - list[(page_text, page_num)] for paged documents (PDF)
        - None if extraction failed or library unavailable
        """
        ...


class DocumentExtractionError(Exception):
    """Base error for document extraction failures."""


class PDFExtractionError(DocumentExtractionError):
    """Raised when PDF extraction fails."""


class DOCXExtractionError(DocumentExtractionError):
    """Raised when DOCX extraction fails."""


class XLSXExtractionError(DocumentExtractionError):
    """Raised when XLSX extraction fails."""


class PPTXExtractionError(DocumentExtractionError):
    """Raised when PPTX extraction fails."""


class TextExtractionError(DocumentExtractionError):
    """Raised when text extraction fails."""

    @property
    def format_label(self) -> str:
        """Human-readable format name for metadata-only fallback."""
        ...

    @property
    def install_hint(self) -> str:
        """Package name hint when extractor is unavailable."""
        ...


_PDF_STRUCTURED_MAX_CHARS = 6000
_NUMERIC_VALUE_PATTERN = re.compile(r"^\$?\s*-?\d[\d,]*(?:\.\d+)?\.?$")
_LINE_TOKEN_PATTERN = re.compile(r"^(?:line\s*)?(\d{1,3}[a-z]?)$", re.IGNORECASE)
_MAX_HINT_LINE_NUMBER = 80
_OCR_LABEL = "OCR text (scan fallback):"
_PADDLE_OCR_ENGINE: Any | None = None
_PADDLE_OCR_INIT_ATTEMPTED = False


def _log_processor_event(level: str, message: str, **fields: Any) -> None:
    """Write structured processor events in the same JSON-lines style as ingest logs."""
    payload = {"ts": datetime.now(timezone.utc).isoformat(), "level": level, "message": message}
    if fields:
        payload.update(fields)
    try:
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    except Exception:
        pass


def _pdf_tables_enabled() -> bool:
    # Default OFF: table extraction via pdfplumber is expensive and noisy on malformed/scanned PDFs.
    val = os.environ.get("LLMLIBRARIAN_PDF_TABLES", "0").strip().lower()
    return val not in ("0", "false", "no")


def _paddleocr_available() -> bool:
    """True when paddleocr is importable in the current environment."""
    try:
        return importlib.util.find_spec("paddleocr") is not None
    except Exception:
        return False


def _tesseract_available() -> bool:
    """True when the tesseract binary is available on PATH."""
    return bool(shutil.which("tesseract"))


def _available_ocr_backends() -> list[str]:
    """Return available OCR backends in deterministic priority order."""
    out: list[str] = []
    if _paddleocr_available():
        out.append("paddleocr")
    if _tesseract_available():
        out.append("tesseract")
    return out


def _get_paddle_ocr_engine() -> Any | None:
    """Lazy-init PaddleOCR engine once per process; returns None when unavailable."""
    global _PADDLE_OCR_ENGINE, _PADDLE_OCR_INIT_ATTEMPTED
    if _PADDLE_OCR_INIT_ATTEMPTED:
        return _PADDLE_OCR_ENGINE
    _PADDLE_OCR_INIT_ATTEMPTED = True
    if not _paddleocr_available():
        return None
    try:
        from paddleocr import PaddleOCR

        _PADDLE_OCR_ENGINE = PaddleOCR(use_angle_cls=False, lang="en", show_log=False)
    except Exception as e:
        _PADDLE_OCR_ENGINE = None
        _log_processor_event(
            "WARN",
            "PaddleOCR initialization failed",
            error=str(e),
            extractor="paddleocr",
        )
    return _PADDLE_OCR_ENGINE


def _extract_paddleocr_text(result: Any) -> str:
    """Extract OCR text fragments from PaddleOCR result payload."""
    parts: list[str] = []

    def _walk(node: Any) -> None:
        if not isinstance(node, (list, tuple)):
            return
        if len(node) >= 2 and isinstance(node[1], (list, tuple)) and node[1]:
            text_candidate = node[1][0]
            if isinstance(text_candidate, str):
                cleaned = text_candidate.strip()
                if cleaned:
                    parts.append(cleaned)
        for child in node:
            _walk(child)

    _walk(result)
    seen: set[str] = set()
    deduped: list[str] = []
    for part in parts:
        if part in seen:
            continue
        seen.add(part)
        deduped.append(part)
    return " ".join(deduped).strip()


def _ocr_with_paddle(image_png: bytes) -> str | None:
    """Run OCR with PaddleOCR when available. Returns extracted text or None."""
    engine = _get_paddle_ocr_engine()
    if engine is None:
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            image_path = Path(td) / "page.png"
            image_path.write_bytes(image_png)
            result = engine.ocr(str(image_path), cls=False)
        text = _extract_paddleocr_text(result)
        return text or None
    except Exception as e:
        _log_processor_event(
            "WARN",
            "PaddleOCR inference failed",
            error=str(e),
            extractor="paddleocr",
        )
        return None


def _ocr_with_tesseract(image_png: bytes) -> str | None:
    """Run OCR with tesseract CLI when available. Returns extracted text or None."""
    if not _tesseract_available():
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            image_path = Path(td) / "page.png"
            output_base = Path(td) / "ocr_out"
            image_path.write_bytes(image_png)
            cmd = [
                "tesseract",
                str(image_path),
                str(output_base),
                "-l",
                "eng",
                "--psm",
                "6",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                _log_processor_event(
                    "WARN",
                    "tesseract OCR failed",
                    error=(proc.stderr or "").strip()[:240],
                    extractor="tesseract",
                )
                return None
            out_path = output_base.with_suffix(".txt")
            if not out_path.exists():
                return None
            text = out_path.read_text(encoding="utf-8", errors="replace").strip()
            return text or None
    except Exception as e:
        _log_processor_event(
            "WARN",
            "tesseract OCR invocation failed",
            error=str(e),
            extractor="tesseract",
        )
        return None


def _ocr_pdf_page_text(page: Any, source_path: str) -> tuple[str | None, str | None]:
    """OCR fallback for image-only PDF pages. Returns (text, backend) or (None, None)."""
    try:
        image_png = page.get_pixmap(dpi=300, alpha=False).tobytes("png")
    except Exception as e:
        _log_processor_event(
            "WARN",
            "PDF OCR render failed",
            path=source_path,
            page=(getattr(page, "number", 0) + 1),
            error=str(e),
        )
        return None, None

    paddle_text: str | None = None
    if _paddleocr_available():
        paddle_text = _ocr_with_paddle(image_png)
    tess_text: str | None = None
    if _tesseract_available():
        tess_text = _ocr_with_tesseract(image_png)
    # Pick the better result: prefer the one with more alphanumeric content.
    # PaddleOCR can produce garbled output on structured forms; tesseract with psm 6 often does better.
    def _alnum_ratio(s: str) -> float:
        if not s:
            return 0.0
        alnum = sum(1 for c in s if c.isalnum() or c in " $.,")
        return alnum / len(s)
    if paddle_text and tess_text:
        pr = _alnum_ratio(paddle_text)
        tr = _alnum_ratio(tess_text)
        if tr > pr + 0.15:  # tesseract is meaningfully cleaner
            return tess_text, "tesseract"
        return paddle_text, "paddleocr"
    if paddle_text:
        return paddle_text, "paddleocr"
    if tess_text:
        return tess_text, "tesseract"
    return None, None


def _normalize_table_rows(rows: list[list[str | None]]) -> list[list[str]]:
    """Normalize table rows: strip/compact whitespace, trim trailing empty cells, and drop empty rows."""
    out: list[list[str]] = []
    for row in rows or []:
        normalized = [re.sub(r"\s+", " ", (cell or "").strip()) for cell in (row or [])]
        first_non_empty = -1
        last_non_empty = -1
        for i, cell in enumerate(normalized):
            if cell:
                if first_non_empty < 0:
                    first_non_empty = i
                last_non_empty = i
        if first_non_empty >= 0 and last_non_empty >= 0:
            out.append(normalized[first_non_empty : last_non_empty + 1])
    return out


def _table_to_markdown(rows: list[list[str]]) -> str:
    """Convert normalized rows to markdown table text."""
    if not rows:
        return ""
    col_count = max(len(r) for r in rows)

    def _pad(row: list[str]) -> list[str]:
        cells = row + ([""] * (col_count - len(row)))
        return [c.replace("|", "\\|") for c in cells]

    header = _pad(rows[0])
    body = [_pad(r) for r in rows[1:]]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * col_count) + " |",
    ]
    lines.extend("| " + " | ".join(r) + " |" for r in body)
    return "\n".join(lines)


def _extract_line_value_hints(rows: list[list[str]]) -> list[str]:
    """Extract compact line/value hints from table-like rows (e.g., line 9 -> 7,522.)."""
    def _is_high_signal_numeric(value: str) -> bool:
        if not _NUMERIC_VALUE_PATTERN.match(value):
            return False
        digits_only = re.sub(r"\D", "", value)
        # Ignore tiny integers that are usually row/column identifiers.
        if len(digits_only) <= 2 and "$" not in value and "," not in value and "." not in value:
            return False
        return True

    hints: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not row:
            continue
        for idx, cell in enumerate(row):
            token = cell.strip().lower()
            if not token:
                continue
            m = _LINE_TOKEN_PATTERN.match(token)
            if not m:
                continue
            line_label = m.group(1)
            num_match = re.match(r"(\d+)", line_label)
            if num_match and int(num_match.group(1)) > _MAX_HINT_LINE_NUMBER:
                break
            value = ""
            for j in range(idx + 1, len(row)):
                candidate = row[j].strip()
                if candidate and _is_high_signal_numeric(candidate):
                    value = candidate
                    break
            if value:
                hint = f"line {line_label}: {value}"
                if hint not in seen:
                    seen.add(hint)
                    hints.append(hint)
            break
    return hints


def _merge_pdf_page_content(
    raw_text: str,
    table_md: str,
    hints: list[str],
    ocr_text: str | None = None,
    max_structured_chars: int = _PDF_STRUCTURED_MAX_CHARS,
) -> str:
    """Merge structured table context ahead of raw text with a hard cap on structured size."""
    sections: list[str] = []
    if hints:
        sections.append("Structured values:\n" + "\n".join(hints))
    if table_md:
        sections.append("Extracted tables:\n" + table_md)
    if ocr_text:
        sections.append(f"{_OCR_LABEL}\n{ocr_text.strip()}")
    structured = "\n\n".join(s for s in sections if s).strip()
    if structured and len(structured) > max_structured_chars:
        structured = structured[:max_structured_chars].rstrip()
    raw = (raw_text or "").strip()
    if structured and raw:
        return f"{structured}\n\n{raw}"
    return structured or raw


def _extract_pdf_tables_by_page(data: bytes) -> list[list[list[str | None]]]:
    """Return page-indexed tables from pdfplumber. Empty list means unavailable/no tables."""
    try:
        import pdfplumber
    except ImportError:
        return []
    # pdfplumber/pdfminer can emit high-volume parser warnings on broken PDFs; keep stderr usable.
    logging.getLogger("pdfminer").setLevel(logging.ERROR)

    tables_by_page: list[list[list[str | None]]] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            page_tables = page.extract_tables() or []
            tables_by_page.append(page_tables)
    return tables_by_page


class PDFProcessor:
    format_label = "PDF"
    install_hint = "pymupdf"

    def extract(self, data: bytes, source_path: str) -> list[tuple[str, int]]:
        try:
            import fitz
            tables_by_page: list[list[list[str | None]]] = []
            if _pdf_tables_enabled():
                try:
                    tables_by_page = _extract_pdf_tables_by_page(data)
                except Exception as e:
                    _log_processor_event(
                        "WARN",
                        "PDF table extraction failed",
                        path=source_path,
                        error=str(e),
                        extractor="pdfplumber",
                    )
            with fitz.open(stream=data, filetype="pdf") as doc:
                out: list[tuple[str, int]] = []
                for page in doc:
                    raw_text = page.get_text()
                    ocr_text: str | None = None
                    try:
                        for w in page.widgets():
                            name = getattr(w, "field_name", None) or ""
                            val = getattr(w, "field_value", None)
                            if name and val is not None and str(val).strip():
                                raw_text += f"\n{name}: {val}"
                    except Exception:
                        pass
                    if not raw_text.strip():
                        ocr_text, backend = _ocr_pdf_page_text(page, source_path)
                        if ocr_text:
                            _log_processor_event(
                                "INFO",
                                "PDF OCR fallback applied",
                                path=source_path,
                                page=page.number + 1,
                                backend=backend,
                            )
                        else:
                            _log_processor_event(
                                "WARN",
                                "PDF page has no extractable text and OCR produced no text",
                                path=source_path,
                                page=page.number + 1,
                                available_ocr_backends=_available_ocr_backends(),
                            )
                    page_tables = tables_by_page[page.number] if page.number < len(tables_by_page) else []
                    hints: list[str] = []
                    md_tables: list[str] = []
                    for table in page_tables:
                        normalized = _normalize_table_rows(table)
                        if not normalized:
                            continue
                        hints.extend(_extract_line_value_hints(normalized))
                        md = _table_to_markdown(normalized)
                        if md:
                            md_tables.append(md)
                    text = _merge_pdf_page_content(raw_text, "\n\n".join(md_tables), hints, ocr_text=ocr_text)
                    out.append((text, page.number + 1))
                return out
        except Exception as e:
            raise PDFExtractionError(str(e)) from e


class DOCXProcessor:
    format_label = "Document"
    install_hint = "python-docx"

    def extract(self, data: bytes, source_path: str) -> str | None:
        try:
            import docx
            doc = docx.Document(io.BytesIO(data))
            return "\n".join(para.text for para in doc.paragraphs)
        except ImportError:
            return None
        except Exception as e:
            raise DOCXExtractionError(str(e)) from e


class XLSXProcessor:
    format_label = "Spreadsheet"
    install_hint = "openpyxl"

    def extract(self, data: bytes, source_path: str) -> str | None:
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            parts: list[str] = []
            for sheet in wb.worksheets:
                parts.append(f"Sheet: {sheet.title}")
                for row in sheet.iter_rows(values_only=True):
                    line = "\t".join(str(c) if c is not None else "" for c in row)
                    if line.strip():
                        parts.append(line)
            wb.close()
            text = "\n".join(parts)
            return text.strip() or None
        except ImportError:
            return None
        except Exception:
            raise XLSXExtractionError("Failed to extract XLSX content.")


class PPTXProcessor:
    format_label = "Presentation"
    install_hint = "python-pptx"

    def extract(self, data: bytes, source_path: str) -> str | None:
        try:
            from pptx import Presentation
            prs = Presentation(io.BytesIO(data))
            parts: list[str] = []
            for i, slide in enumerate(prs.slides):
                parts.append(f"Slide {i + 1}")
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text:
                        parts.append(shape.text)
            text = "\n".join(parts)
            return text.strip() or None
        except ImportError:
            return None
        except Exception:
            raise PPTXExtractionError("Failed to extract PPTX content.")


class TextProcessor:
    format_label = "Text"
    install_hint = ""

    def extract(self, data: bytes, source_path: str) -> str:
        try:
            return data.decode("utf-8", errors="replace")
        except Exception as e:
            raise TextExtractionError(str(e)) from e


# Registry: suffix -> processor instance
PROCESSORS: dict[str, DocumentProcessor] = {
    ".pdf": PDFProcessor(),  # type: ignore[dict-item]
    ".docx": DOCXProcessor(),  # type: ignore[dict-item]
    ".xlsx": XLSXProcessor(),  # type: ignore[dict-item]
    ".pptx": PPTXProcessor(),  # type: ignore[dict-item]
}

# Default processor for code/text files
DEFAULT_PROCESSOR = TextProcessor()
