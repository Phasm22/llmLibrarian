"""
Archetype-aware ingest: one collection per archetype, rebuild from scratch.
Resource limits, ZIP limits (skip encrypted), include/exclude via should_index.
Metadata: source_path, mtime, chunk_hash, line_start (code), page (PDF).

Flow: collect file list -> read+chunk in ThreadPoolExecutor -> batch add().
ZIPs processed in main thread (limits); regular files in parallel; add in batches.
"""
import fnmatch
import hashlib
import io
import json
import csv
import os
import re
import shutil
import sys
import tempfile
import traceback
import zipfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Chunk tuple: (id, document, metadata)
ChunkTuple = tuple[str, str, dict[str, Any]]
ImageVectorTuple = tuple[str, str, str, dict[str, Any]]

from constants import (
    DB_PATH,
    LLMLI_COLLECTION,
    LLMLI_IMAGE_COLLECTION,
    CHUNK_SIZE,
    CHUNK_OVERLAP,
    ADD_BATCH_SIZE,
    MAX_WORKERS,
)

import chromadb
from chromadb.config import Settings
try:
    from tqdm import tqdm  # type: ignore[import-not-found]
except Exception:
    tqdm = None  # type: ignore[assignment]

from embeddings import get_embedding_function
from image_embeddings import (
    ensure_image_embedding_adapter_ready,
    image_collection_name,
    image_embedding_backend_name,
)
from file_registry import (
    _file_manifest_path,
    _read_file_manifest,
    _write_file_manifest,
    _update_file_manifest,
    _file_registry_path,
    _read_file_registry,
    _file_registry_get,
    _file_registry_add,
    _file_registry_remove_path,
    _file_registry_remove_silo,
    get_paths_by_silo,
)
from load_config import load_config, get_archetype
from style import bold, dim, label_style, success_style, warn_style

# --- Default limits (overridden by config) ---
DEFAULT_MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB
DEFAULT_MAX_DEPTH = 10
DEFAULT_MAX_ARCHIVE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB
DEFAULT_MAX_FILES_PER_ZIP = 500
DEFAULT_MAX_EXTRACTED_BYTES_PER_ZIP = 50 * 1024 * 1024  # 50 MB
# Default include/exclude for llmli add. First-class: text + code + pdf/docx + xlsx/pptx (no silent ignore).
ADD_DEFAULT_INCLUDE = [
    "*.py", "*.ts", "*.tsx", "*.js", "*.go", "*.rs", "*.sh", "*.md", "*.txt",
    "*.yml", "*.yaml", "*.json", "*.csv", "*.xml", "*.html", "*.rst", "*.toml", "*.ini", "*.cfg", "*.sql",
    "*.pdf", "*.docx", "*.xlsx", "*.pptx",
    "*.png", "*.jpg", "*.jpeg", "*.heic", "*.heif", "*.tif", "*.tiff",
]
ADD_DEFAULT_EXCLUDE = [
    "node_modules/", ".venv/", "venv/", "env/", "__pycache__/", "vendor", "dist", "build", ".git",
    "llmLibrarianVenv/", "site-packages/", "Old Firefox Data", "Firefox", ".app/",
    ".env", ".env.*", ".aws/", ".ssh/", "*.pem", "*.key", "secrets.json", "credentials.json", "credentials*.json",
    "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "Pipfile.lock", "poetry.lock",
    "composer.lock", "Gemfile.lock", "Cargo.lock", "uv.lock",
    "my_brain_db/", "*.db", "*.sqlite", "*.sqlite3",
]

# Code-file extensions for language_stats (CODE_LANGUAGE pipeline). Excludes .md, .txt, .pdf, .docx.
ADD_CODE_EXTENSIONS = frozenset(
    {".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".sh", ".bash", ".zsh", ".php", ".kt", ".sql"}
)
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff"})
_PREVIEW_SKIPPED_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".wav", ".mp3", ".aac"})
# Text extensions we decode as UTF-8 when found inside ZIPs (no binary handling).
ZIP_TEXT_EXTENSIONS = frozenset(
    {".txt", ".md", ".json", ".csv", ".xml", ".html", ".rst", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sql", ".py", ".js", ".ts", ".tsx", ".sh", ".go", ".rs"}
)
_INGEST_LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "FATAL": 50}


def get_capabilities_text() -> str:
    """Deterministic report: supported extensions and document extractors. Source of truth for llmli capabilities and ask routing."""
    from processors import ocr_backend_chain_for_capabilities

    exts = sorted({p.replace("*", "") for p in ADD_DEFAULT_INCLUDE if p.startswith("*.")})
    lines = ["Supported file extensions (indexed by llmli add):", "  " + ", ".join(exts), ""]
    lines.append("Document extractors:")
    lines.append("  PDF: yes (pymupdf)")
    ocr_backends = ocr_backend_chain_for_capabilities()
    if ocr_backends:
        chain = " -> ".join(ocr_backends)
        lines.append(f"  PDF OCR fallback: yes ({chain})")
        lines.append(f"  Image OCR: yes ({chain})")
        lines.append("  Image summaries: requires LLMLIBRARIAN_VISION_MODEL (vision-capable Ollama model)")
        backend = image_embedding_backend_name()
        if backend:
            lines.append(f"  Image embeddings: yes ({backend} -> {LLMLI_IMAGE_COLLECTION})")
        else:
            lines.append(f"  Image embeddings: no (run `uv sync` to install the multimodal embedding backend for {LLMLI_IMAGE_COLLECTION})")
    else:
        if sys.platform == "darwin":
            lines.append("  PDF OCR fallback: no (Vision needs the macOS Swift toolchain; otherwise install paddleocr or tesseract)")
            lines.append("  Image OCR: no (Vision needs the macOS Swift toolchain; otherwise install paddleocr or tesseract)")
        else:
            lines.append("  PDF OCR fallback: no (install paddleocr or tesseract for scanned/image PDFs)")
            lines.append("  Image OCR: no (install paddleocr or tesseract for image files)")
    lines.append("  DOCX: yes (python-docx)")
    lines.append("  ZIP: yes (PDF/DOCX/XLSX/PPTX/images + text inside)")
    try:
        import openpyxl  # noqa: F401
        lines.append("  XLSX: yes (openpyxl)")
    except ImportError:
        lines.append("  XLSX: no (install openpyxl for text extraction)")
    try:
        from pptx import Presentation  # noqa: F401
        lines.append("  PPTX: yes (python-pptx)")
    except ImportError:
        lines.append("  PPTX: no (install python-pptx for text extraction)")
    return "\n".join(lines)


def _requires_standalone_image_enrichment(file_list: list[tuple[Path, str]]) -> bool:
    return any(p.suffix.lower() in IMAGE_EXTENSIONS for p, _kind in file_list)


def _doc_type_from_path(path_str: str) -> str:
    """Tag by filename/path for evidence preference (transcript > audit > syllabus etc.). No ML."""
    p = (path_str or "").lower()
    if "transcript" in p:
        return "transcript"
    if "audit" in p or "degree plan" in p or "degree_plan" in p:
        return "audit"
    if "syllabus" in p or "syllabi" in p:
        return "syllabus"
    if "homework" in p or " hw " in p or " hw." in p or "assignment" in p or "hw1" in p or "hw2" in p:
        return "homework"
    if "paper" in p or "essay" in p:
        return "paper"
    return "other"


# Content-first doc_type: override path-based when first 500 chars match (e.g. "Form 1040" → tax_return)
CONTENT_SAMPLE_LEN = 500


def _doc_type_from_content(sample: str) -> str:
    """Categorize from first N chars (e.g. Form 1040 → tax_return). Overrides path when not 'other'."""
    s = (sample or "").replace("\r", " ").replace("\n", " ")[:CONTENT_SAMPLE_LEN]
    if not s.strip():
        return "other"
    # Tax forms / returns
    if re.search(r"\bForm\s+1040\b|Schedule\s+[A-C]\b|W-2|1099|IRS|adjusted\s+gross\s+income\b", s, re.IGNORECASE):
        return "tax_return"
    # Transcript / academic record (use specific transcript anchors; avoid broad course-planning docs)
    if re.search(
        r"\b(unofficial\s+transcript|official\s+transcript|course\s+history|academic\s+standing|term\s+totals?|quality\s+points|last\s+academic\s+standing|transcript)\b",
        s,
        re.IGNORECASE,
    ):
        return "transcript"
    # Syllabus
    if re.search(r"\bsyllabus\b|office\s+hours\b|course\s+objectives\b|prerequisite", s, re.IGNORECASE):
        return "syllabus"
    # Audit / degree plan
    if re.search(r"\baudit\b|degree\s+plan\b|requirements\s+met\b", s, re.IGNORECASE):
        return "audit"
    return "other"


def get_file_hash(path: Path) -> str:
    """Content-first identity: hash of first 8k bytes + file size. Same file via symlink = same hash."""
    hasher = hashlib.md5()
    try:
        with open(path, "rb") as f:
            hasher.update(f.read(8192))
            f.seek(0, 2)
            hasher.update(str(f.tell()).encode())
    except OSError:
        return ""
    return hasher.hexdigest()


def _log_event(level: str, message: str, **fields: Any) -> None:
    """Structured log line for ingest errors. Writes JSON to stderr."""
    normalized_level = str(level or "INFO").upper()
    if normalized_level == "WARNING":
        normalized_level = "WARN"
    min_level = (os.environ.get("LLMLIBRARIAN_INGEST_LOG_LEVEL") or "WARN").strip().upper()
    if min_level == "WARNING":
        min_level = "WARN"
    if min_level not in _INGEST_LOG_LEVELS:
        min_level = "WARN"
    if _INGEST_LOG_LEVELS.get(normalized_level, 20) < _INGEST_LOG_LEVELS[min_level]:
        return
    event = {"ts": datetime.now(timezone.utc).isoformat(), "level": normalized_level, "message": message}
    if fields:
        event.update(fields)
    try:
        print(json.dumps(event, ensure_ascii=False), file=sys.stderr)
    except Exception:
        pass


def _suppress_recoverable_warnings() -> bool:
    return (os.environ.get("LLMLIBRARIAN_SUPPRESS_RECOVERABLE_WARNINGS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


# Path components that indicate a cloud-sync root (OneDrive, iCloud, Dropbox, etc.).
# Ingestion is disabled for these by default; use --allow-cloud to override (e.g. after pinning).
CLOUD_SYNC_NAMES = frozenset(
    {"onedrive", "dropbox", "google drive", "icloud drive", "box", "microsoft onedrive"}
)


class CloudSyncPathError(Exception):
    """Raised when add is run on a cloud-sync folder without --allow-cloud."""
    def __init__(self, path: Path, kind: str):
        self.path = path
        self.kind = kind
        super().__init__(f"Path is under a cloud-sync folder ({kind}). Ingestion may be unreliable (placeholders, offline files). Use --allow-cloud to override.")


def is_cloud_sync_path(path: str | Path) -> str | None:
    """
    If path is under a known cloud-sync root, return the cloud name (e.g. 'OneDrive'); else None.
    Relies on path components only (OneDrive, OneDrive - Org, iCloud Drive, Dropbox, Google Drive, Box).
    """
    resolved = Path(path).resolve()
    parts = [p.lower() for p in resolved.parts]
    for i, part in enumerate(parts):
        if part in CLOUD_SYNC_NAMES:
            return resolved.parts[i]  # return original casing for message
        if part.startswith("onedrive ") or part.startswith("onedrive-"):
            return resolved.parts[i]
        if part == "library" and i + 1 < len(parts) and parts[i + 1] == "mobile documents":
            return "iCloud"
    return None


def _path_matches(path_str: str, pattern: str) -> bool:
    """Match pattern against path. If pattern has no path sep, match against basename only (so *.pdf works)."""
    if "/" in pattern or "\\" in pattern:
        return fnmatch.fnmatch(path_str, pattern)
    return fnmatch.fnmatch(path_str, pattern) or fnmatch.fnmatch(os.path.basename(path_str), pattern)


def should_index(file_path: str | Path, include_patterns: list[str], exclude_patterns: list[str]) -> bool:
    """Check excludes first, then includes. Path can be relative or absolute. Use for FILES only."""
    path_str = str(file_path)
    base = os.path.basename(path_str)
    # Skip Office lock/temp files (e.g. "~$Draft.docx") that are not real documents.
    if base.startswith("~$"):
        return False
    for pattern in exclude_patterns:
        if pattern.rstrip("/") in path_str or _path_matches(path_str, pattern):
            return False
    for pattern in include_patterns:
        if _path_matches(path_str, pattern):
            return True
    return False


def should_descend_into_dir(dir_path: str | Path, exclude_patterns: list[str]) -> bool:
    """Return False if directory is excluded (e.g. node_modules), True otherwise. We descend unless excluded."""
    path_str = str(dir_path).rstrip("/") + "/"
    for pattern in exclude_patterns:
        if pattern.rstrip("/") in path_str or fnmatch.fnmatch(path_str, pattern):
            return False
    return True


def is_safe_path(base: Path, path: str) -> bool:
    """Return True if 'path' stays within 'base' after resolution (prevents traversal)."""
    try:
        base_res = base.resolve()
        target = (base_res / path).resolve()
        return not Path(path).is_absolute() and target.is_relative_to(base_res)
    except Exception:
        return False


def _chunk_params() -> tuple[int, int]:
    """(chunk_size, overlap) from env or defaults. Used so add/index can be tuned without code change."""
    size = CHUNK_SIZE
    overlap = CHUNK_OVERLAP
    try:
        if os.environ.get("LLMLIBRARIAN_CHUNK_SIZE"):
            size = max(100, min(int(os.environ["LLMLIBRARIAN_CHUNK_SIZE"]), 4000))
        if os.environ.get("LLMLIBRARIAN_CHUNK_OVERLAP"):
            overlap = max(0, min(int(os.environ["LLMLIBRARIAN_CHUNK_OVERLAP"]), size // 2))
    except (TypeError, ValueError):
        pass
    return (size, overlap)


def chunk_text(text: str, size: int | None = None, overlap: int | None = None) -> list[tuple[str, int]]:
    """Returns list of (chunk_text, line_start). line_start is 1-based approximate. Uses env LLMLIBRARIAN_CHUNK_* when size/overlap not passed."""
    if size is None or overlap is None:
        psize, poverlap = _chunk_params()
        size = size if size is not None else psize
        overlap = overlap if overlap is not None else poverlap
    if not text.strip():
        return []
    lines = text.split("\n")
    result: list[tuple[str, int]] = []
    current: list[str] = []
    current_len = 0
    line_start = 1
    for i, line in enumerate(lines):
        line_num = i + 1
        current.append(line)
        current_len += len(line) + 1
        if current_len >= size:
            chunk = "\n".join(current)
            result.append((chunk, line_start))
            # overlap: keep last few lines
            overlap_len = 0
            overlap_lines: list[str] = []
            for j in range(len(current) - 1, -1, -1):
                overlap_lines.insert(0, current[j])
                overlap_len += len(current[j]) + 1
                if overlap_len >= overlap and j > 0:
                    line_start = line_num - len(overlap_lines) + 1
                    break
            current = overlap_lines
            current_len = overlap_len
            if not current:
                line_start = line_num + 1
    if current:
        result.append(("\n".join(current), line_start))
    return result


# --- Extraction (delegated to processors.py; wrappers kept for ZIP processing) ---
from processors import (
    PDFProcessor,
    DOCXProcessor,
    XLSXProcessor,
    PPTXProcessor,
    ImageProcessor,
    PROCESSORS,
    DEFAULT_PROCESSOR,
    DocumentExtractionError,
    DocumentProcessor,
    PDFExtractionError,
    ExtractedPage,
    ExtractedImage,
    ImageRegion,
    ExtractedText,
    ensure_vision_model_ready,
)
from tax.ledger import extract_tax_rows_from_chunks, replace_tax_rows_for_sources

_pdf_proc = PDFProcessor()
_docx_proc = DOCXProcessor()
_xlsx_proc = XLSXProcessor()
_pptx_proc = PPTXProcessor()
_img_proc = ImageProcessor()


def get_text_from_pdf_bytes(pdf_bytes: bytes, source_path: str = "") -> list[ExtractedPage]:
    """Returns extracted PDF pages. Raises PDFExtractionError on failure."""
    result = _pdf_proc.extract(pdf_bytes, source_path)
    if not isinstance(result, list):
        raise PDFExtractionError("PDF extractor returned unexpected result.")
    return result


def get_text_from_docx_bytes(docx_bytes: bytes) -> str:
    """Delegates to DOCXProcessor."""
    try:
        result = _docx_proc.extract(docx_bytes, "")
    except DocumentExtractionError as e:
        _log_event("ERROR", "DOCX extraction failed", error=str(e))
        return ""
    return result if isinstance(result, str) else ""


def get_text_from_xlsx_bytes(data: bytes) -> tuple[str | None, bool]:
    """Delegates to XLSXProcessor. Returns (text, True) or (None, False)."""
    try:
        result = _xlsx_proc.extract(data, "")
    except DocumentExtractionError as e:
        _log_event("ERROR", "XLSX extraction failed", error=str(e))
        return (None, False)
    if result is None:
        return (None, False)
    return (result, True)


def get_text_from_pptx_bytes(data: bytes) -> tuple[str | None, bool]:
    """Delegates to PPTXProcessor. Returns (text, True) or (None, False)."""
    try:
        result = _pptx_proc.extract(data, "")
    except DocumentExtractionError as e:
        _log_event("ERROR", "PPTX extraction failed", error=str(e))
        return (None, False)
    if result is None:
        return (None, False)
    return (result, True)


def get_text_from_image_bytes(
    data: bytes,
    source_path: str,
    *,
    enable_multimodal: bool = True,
) -> ExtractedImage | None:
    """Delegates to ImageProcessor."""
    try:
        result = _img_proc.extract(data, source_path, enable_multimodal=enable_multimodal)
    except DocumentExtractionError as e:
        _log_event("ERROR", "Image extraction failed", error=str(e), path=source_path)
        return None
    return result if isinstance(result, ExtractedImage) else None


def _chunk_metadata_only(
    file_id: str,
    source_path: str,
    mtime: float,
    format_label: str,
    install_hint: str,
    file_hash: str | None = None,
) -> list[ChunkTuple]:
    """One chunk with filename/path/mtime only; content_extracted=0 so file is not silently ignored."""
    doc = f"{format_label}: {Path(source_path).name} (content not extracted; install {install_hint} for text extraction.)"
    cid = _stable_chunk_id(source_path, mtime, 0)
    meta: dict = {
        "source": source_path,
        "source_path": source_path,
        "mtime": mtime,
        "chunk_hash": _chunk_hash(doc),
        "file_id": file_id,
        "line_start": 0,
        "is_local": _is_local(source_path),
        "doc_type": "other",
        "content_extracted": 0,
    }
    if file_hash:
        meta["file_hash"] = file_hash
    return [(cid, doc, meta)]


def _image_artifact_relpath(file_hash: str) -> str:
    return f"image_artifacts/{file_hash}.json"


def _write_image_artifact(
    db_path: str | Path | None,
    file_hash: str | None,
    payload: dict[str, Any] | None,
) -> str | None:
    if not db_path or not file_hash or not payload:
        return None
    relpath = _image_artifact_relpath(file_hash)
    artifact_path = Path(db_path) / relpath
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = artifact_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, artifact_path)
    return relpath


def _read_image_artifact(db_path: str | Path | None, relpath: str | None) -> dict[str, Any] | None:
    if not db_path or not relpath:
        return None
    artifact_path = Path(db_path) / relpath
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _update_image_artifact(
    db_path: str | Path | None,
    relpath: str | None,
    payload: dict[str, Any],
) -> str | None:
    if not db_path or not relpath or not payload:
        return None
    artifact_path = Path(db_path) / relpath
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = artifact_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, artifact_path)
    return relpath


# --- Indexing ---
def _chunk_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _stable_chunk_id(source_path: str, mtime: float, chunk_index: int) -> str:
    key = f"{source_path}|{mtime}|{chunk_index}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def _flatten_get_values(values: Any) -> list[Any]:
    """Normalize Chroma get() values to a flat list."""
    if not values:
        return []
    if isinstance(values, list) and values and isinstance(values[0], list):
        return list(values[0])
    if isinstance(values, list):
        return list(values)
    return []


def _is_local(source_path: str) -> int:
    """1 if path looks local (no cloud sync), 0 if cloud (OneDrive, iCloud, etc.). ChromaDB metadata: int."""
    if not source_path:
        return 1
    p = source_path.lower()
    if "cloudstorage" in p or "onedrive" in p or "icloud" in p or "google drive" in p or "dropbox" in p:
        return 0
    return 1


def _chunks_from_content(
    file_id: str,
    text: str,
    source_path: str,
    mtime: float,
    page: int | None = None,
    prefix: str = "",
    file_hash: str | None = None,
    content_extracted: bool = True,
    extra_meta: dict[str, Any] | None = None,
) -> list[ChunkTuple]:
    """Build chunk list (id, doc, meta). doc_type from content (first 500 chars) overrides path when not 'other'."""
    content_type = _doc_type_from_content(text[:CONTENT_SAMPLE_LEN])
    doc_type = content_type if content_type != "other" else _doc_type_from_path(source_path)
    chunks_with_lines = chunk_text(text)
    out: list[ChunkTuple] = []
    for i, (chunk, line_s) in enumerate(chunks_with_lines):
        cid = _stable_chunk_id(source_path, mtime, i)
        meta: dict = {
            "source": source_path,
            "source_path": source_path,
            "mtime": mtime,
            "chunk_hash": _chunk_hash(chunk),
            "file_id": file_id,
            "line_start": line_s,
            "is_local": _is_local(source_path),
            "doc_type": doc_type,
            "content_extracted": 1 if content_extracted else 0,
        }
        if file_hash:
            meta["file_hash"] = file_hash
        if page is not None:
            meta["page"] = page
        if extra_meta:
            meta.update(extra_meta)
        out.append((cid, chunk, meta))
    return out


def _image_parent_id(source_path: str, file_hash: str | None, mtime: float) -> str:
    if file_hash:
        return file_hash
    return hashlib.sha256(f"image|{source_path}|{mtime}".encode()).hexdigest()[:20]


def _image_doc_type_from_text(source_path: str, *parts: str) -> str:
    sample = "\n".join(part for part in parts if part).strip()
    content_type = _doc_type_from_content(sample[:CONTENT_SAMPLE_LEN])
    return content_type if content_type != "other" else _doc_type_from_path(source_path)


def _chunks_from_image_result(
    file_id: str,
    result: ExtractedImage,
    source_path: str,
    mtime: float,
    *,
    db_path: str | Path | None = None,
    file_hash: str | None = None,
) -> list[ChunkTuple]:
    parent_image_id = _image_parent_id(source_path, file_hash, mtime)
    artifact_relpath = _write_image_artifact(db_path, file_hash, result.artifact)
    summary_text = (result.summary or "").strip()
    visible_text = (result.visible_text or "").strip()
    doc_type = _image_doc_type_from_text(source_path, summary_text, visible_text)
    base_meta: dict[str, Any] = {
        "source": source_path,
        "source_path": source_path,
        "mtime": mtime,
        "file_id": file_id,
        "is_local": _is_local(source_path),
        "doc_type": doc_type,
        "content_extracted": 1,
        "source_modality": "image",
        "parent_image_id": parent_image_id,
        "bbox_x": 0.0,
        "bbox_y": 0.0,
        "bbox_w": 1.0,
        "bbox_h": 1.0,
    }
    if file_hash:
        base_meta["file_hash"] = file_hash
    if artifact_relpath:
        base_meta["image_artifact_relpath"] = artifact_relpath
    if result.meta:
        base_meta.update({k: v for k, v in result.meta.items() if v is not None})

    out: list[ChunkTuple] = []
    summary_meta = dict(base_meta)
    summary_meta.update(
        {
            "record_type": "image_summary",
            "region_index": 0,
            "region_role": "image_summary",
            "needs_vision_enrichment": bool(base_meta.get("needs_vision_enrichment")),
            "summary_status": base_meta.get("summary_status"),
            "chunk_hash": _chunk_hash(summary_text),
        }
    )
    summary_meta = {k: v for k, v in summary_meta.items() if v is not None}
    out.append((_stable_chunk_id(source_path, mtime, 0), summary_text, summary_meta))

    for idx, region in enumerate(result.regions, start=1):
        region_text = (region.text or "").strip()
        if not region_text:
            continue
        doc = f"Image region: {region.role}\n{region_text}"
        region_meta = dict(base_meta)
        region_meta.update(
            {
                "record_type": "image_region",
                "region_index": idx,
                "region_role": region.role,
                "bbox_x": float(region.x),
                "bbox_y": float(region.y),
                "bbox_w": float(region.w),
                "bbox_h": float(region.h),
                "needs_vision_enrichment": bool(region.needs_vision_enrichment),
                "summary_status": base_meta.get("summary_status"),
                "chunk_hash": _chunk_hash(doc),
            }
        )
        region_extra = dict(region.meta or {})
        if region_extra:
            region_meta.update({k: v for k, v in region_extra.items() if v is not None})
        region_meta = {k: v for k, v in region_meta.items() if v is not None}
        out.append((_stable_chunk_id(source_path, mtime, idx), doc, region_meta))
    return out


def _image_vector_id(parent_image_id: str) -> str:
    return hashlib.sha256(f"image-vector|{parent_image_id}".encode("utf-8")).hexdigest()[:20]


def _image_vector_from_chunks(
    *,
    source_path: str,
    chunks: list[ChunkTuple],
) -> ImageVectorTuple | None:
    summary: tuple[str, str, dict[str, Any]] | None = None
    for cid, doc, meta in chunks:
        if str((meta or {}).get("record_type") or "") == "image_summary":
            summary = (cid, doc, meta)
            break
    if summary is None:
        return None
    _cid, doc, meta = summary
    parent_image_id = str(meta.get("parent_image_id") or "")
    if not parent_image_id:
        return None
    vector_meta = {
        "source": source_path,
        "source_path": source_path,
        "source_modality": "image",
        "record_type": "image_vector",
        "parent_image_id": parent_image_id,
        "doc_type": meta.get("doc_type"),
        "file_id": meta.get("file_id"),
        "mtime": meta.get("mtime"),
        "file_hash": meta.get("file_hash"),
        "ocr_backend": meta.get("ocr_backend"),
        "ocr_mode": meta.get("ocr_mode"),
        "vision_model": meta.get("vision_model"),
        "image_artifact_relpath": meta.get("image_artifact_relpath"),
        "image_embedding_backend": image_embedding_backend_name(),
        "needs_vision_enrichment": meta.get("needs_vision_enrichment"),
        "summary_status": meta.get("summary_status"),
        "is_local": meta.get("is_local"),
    }
    vector_meta = {k: v for k, v in vector_meta.items() if v is not None}
    return (_image_vector_id(parent_image_id), source_path, doc, vector_meta)


def _chunks_from_csv_text(
    file_id: str,
    text: str,
    source_path: str,
    mtime: float,
    file_hash: str | None = None,
) -> list[ChunkTuple]:
    """
    Build chunk list for CSV with one chunk per row.
    This preserves tabular semantics for rank/lookup queries better than generic paragraph chunking.
    """
    if not (text or "").strip():
        return []
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []

    header = [h.strip() for h in (rows[0] or [])]
    out: list[ChunkTuple] = []
    # Data rows start at line 2 in the file.
    for i, row in enumerate(rows[1:], start=2):
        if not any((c or "").strip() for c in row):
            continue
        cells = [str(c).strip() for c in row]
        if header and len(header) == len(cells):
            pairs = [f"{header[j]}={cells[j]}" for j in range(len(cells))]
        else:
            pairs = [f"c{j + 1}={cells[j]}" for j in range(len(cells))]
        # Keep chunks compact/structured for retrieval.
        doc = f"CSV row {i - 1}: " + " | ".join(pairs)
        cid = _stable_chunk_id(source_path, mtime, i - 1)
        meta: dict[str, Any] = {
            "source": source_path,
            "source_path": source_path,
            "mtime": mtime,
            "chunk_hash": _chunk_hash(doc),
            "file_id": file_id,
            "line_start": i,
            "row_number": i - 1,
            "is_local": _is_local(source_path),
            "doc_type": "other",
            "content_extracted": 1,
        }
        if file_hash:
            meta["file_hash"] = file_hash
        out.append((cid, doc, meta))
    return out


_TRANSCRIPT_TERM_RE = re.compile(r"\b(Spring|Summer|Fall|Winter)\s+(20\d{2})\b", re.IGNORECASE)
_TRANSCRIPT_CODE_RE = re.compile(
    r"^\s*(?:[A-Z]\s+)?(?P<code>[A-Z]{2,4}\s*-?\s*\d{3,4}[A-Z]?)\b"
)
_TRANSCRIPT_GRADES = frozenset({"A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F", "P", "NP", "S", "U", "W", "I"})
_TRANSCRIPT_LEVELS = frozenset({"UG", "GR", "LD", "UD"})
_TRANSCRIPT_CREDITS_RE = re.compile(r"^\d{1,2}(?:\.\d{1,3})?$")
_TRANSCRIPT_STUDENT_NAME_RE = re.compile(r"\bNAME\s*:\s*([^\n]+)", re.IGNORECASE)
_TRANSCRIPT_TITLE_SKIP_RE = re.compile(
    r"\b("
    r"course\s+title|crse\s+nr|units|grade|pnts|"
    r"att|earned|gpahrs|gpapts|gpa|"
    r"cumulative\s+credits|term\s+totals?|"
    r"degrees?,?\s+certificates?|other\s+institutions|"
    r"transfer,?test|end\s+of\s+academic\s+record|"
    r"page\s+\d+\s+of\s+\d+"
    r")\b",
    re.IGNORECASE,
)
_TRANSCRIPT_SCHOOL_ALIAS_RE = re.compile(
    r"\b("
    r"uccs|"
    r"cu\s+colo(?:rado)?\s+springs|"
    r"university\s+of\s+colorado(?:\s+colorado\s+springs)?"
    r")\b",
    re.IGNORECASE,
)
_TRANSCRIPT_PAGE_ANCHOR_RE = re.compile(
    r"\b("
    r"unofficial\s+transcript|"
    r"official\s+transcript|"
    r"course\s+history|"
    r"academic\s+standing|"
    r"term\s+totals?|"
    r"quality\s+points|"
    r"institution\s+credits|"
    r"transfer\s+credits|"
    r"last\s+academic\s+standing"
    r")\b",
    re.IGNORECASE,
)


def _parse_transcript_body(body: str) -> tuple[str, str, str]:
    tokens = [t for t in (body or "").split() if t]
    if not tokens:
        return ("", "", "")
    grade = ""
    credits = ""
    if len(tokens) >= 2:
        last = tokens[-1].upper()
        prev = tokens[-2].upper()
        if last in _TRANSCRIPT_GRADES and _TRANSCRIPT_CREDITS_RE.match(tokens[-2]):
            grade = tokens[-1].upper()
            credits = tokens[-2]
            tokens = tokens[:-2]
        elif _TRANSCRIPT_CREDITS_RE.match(tokens[-1]) and prev in _TRANSCRIPT_GRADES:
            grade = tokens[-2].upper()
            credits = tokens[-1]
            tokens = tokens[:-2]
        elif last in _TRANSCRIPT_GRADES:
            grade = tokens[-1].upper()
            tokens = tokens[:-1]
        elif _TRANSCRIPT_CREDITS_RE.match(tokens[-1]):
            credits = tokens[-1]
            tokens = tokens[:-1]
    title = " ".join(tokens).strip(" -|:")
    return (title, grade, credits)


def _normalize_person_name(raw_name: str) -> str:
    name = " ".join((raw_name or "").split()).strip(" ,;:-")
    if not name:
        return ""
    name = re.split(
        r"\b(STUDENT\s*NR|BIRTHDATE|PRINT\s+DATE|PAGE\s+\d+\s+OF\s+\d+)\b",
        name,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" ,;:-")
    if "," in name:
        last, rest = name.split(",", 1)
        ordered = [tok for tok in rest.split() if tok] + [last.strip()]
        name = " ".join(tok for tok in ordered if tok)
    name = re.sub(r"[^A-Za-z' -]", " ", name)
    return " ".join(name.split())


def _extract_transcript_student_name(page_text: str) -> str:
    match = _TRANSCRIPT_STUDENT_NAME_RE.search(page_text or "")
    if not match:
        return ""
    return _normalize_person_name(match.group(1))


def _normalize_school_label(value: str) -> str:
    school = " ".join((value or "").split()).strip(" -|:")
    if not school:
        return ""
    # Keep a stable canonical display label when UCCS aliases are detected.
    if _TRANSCRIPT_SCHOOL_ALIAS_RE.search(school):
        return "University of Colorado Colorado Springs"
    return school


def _extract_default_transcript_school(page_text: str, source_path: str) -> str:
    src = (source_path or "").lower()
    if "uccs" in src:
        return "University of Colorado Colorado Springs"
    match = _TRANSCRIPT_SCHOOL_ALIAS_RE.search(page_text or "")
    if not match:
        return ""
    return _normalize_school_label(match.group(0))


def _is_transcript_title_candidate(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    if _TRANSCRIPT_TERM_RE.search(s):
        return False
    if _TRANSCRIPT_CODE_RE.match(s):
        return False
    if _TRANSCRIPT_TITLE_SKIP_RE.search(s):
        return False
    if re.fullmatch(r"[-=]{3,}", s):
        return False
    # Titles are typically alpha-heavy and short enough to avoid headers/noise.
    digit_count = sum(ch.isdigit() for ch in s)
    if digit_count >= 4:
        return False
    return bool(re.search(r"[A-Za-z]{3,}", s))


def _extract_grade_credits_from_lookahead(lines: list[str], start_idx: int) -> tuple[str, str]:
    grade = ""
    credits = ""
    for idx in range(start_idx, min(len(lines), start_idx + 4)):
        line = (lines[idx] or "").strip()
        if not line:
            continue
        for token in line.split():
            token_u = token.upper()
            if not grade and token_u in _TRANSCRIPT_GRADES:
                grade = token_u
            if not credits and _TRANSCRIPT_CREDITS_RE.match(token):
                credits = token
        if grade and credits:
            break
    return grade, credits


def _course_status_from_grade(grade: str) -> str:
    if (grade or "").upper() in {"W", "I", "NP", "U"}:
        return "attempted_not_completed"
    return "attempted"


def _extract_transcript_rows(page_text: str, *, source_path: str = "") -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current_term = ""
    current_school = _extract_default_transcript_school(page_text, source_path)
    student_name = _extract_transcript_student_name(page_text)
    pending_title = ""
    normalized_lines = [" ".join(raw_line.split()).strip() for raw_line in (page_text or "").splitlines()]
    for idx, line in enumerate(normalized_lines):
        if not line:
            continue
        term_match = _TRANSCRIPT_TERM_RE.search(line)
        if term_match:
            current_term = f"{term_match.group(1).title()} {term_match.group(2)}"
            school_tail = _normalize_school_label(line[term_match.end() :])
            if school_tail:
                current_school = school_tail
            pending_title = ""
            continue
        school_match = _TRANSCRIPT_SCHOOL_ALIAS_RE.search(line)
        if school_match:
            current_school = _normalize_school_label(school_match.group(0))
        code_match = _TRANSCRIPT_CODE_RE.match(line)
        if not code_match:
            pending_title = line if _is_transcript_title_candidate(line) else ""
            continue
        code = " ".join((code_match.group("code") or "").replace("-", " ").split())
        body = line[code_match.end() :].strip()
        title = ""
        grade = ""
        credits = ""
        if body:
            body_tokens = body.split()
            if body_tokens and body_tokens[0].upper() in _TRANSCRIPT_LEVELS:
                body = " ".join(body_tokens[1:]).strip()
            title, grade, credits = _parse_transcript_body(body)
        if not title and pending_title:
            title = pending_title
        if not grade or not credits:
            look_grade, look_credits = _extract_grade_credits_from_lookahead(normalized_lines, idx + 1)
            if not grade:
                grade = look_grade
            if not credits:
                credits = look_credits
        if not title:
            pending_title = ""
            continue
        rows.append(
            {
                "course_code": code,
                "course_title": title,
                "course_term": current_term,
                "course_grade": grade,
                "course_credits": credits,
                "course_school": current_school,
                "student_name": student_name,
                "course_status": _course_status_from_grade(grade),
            }
        )
        pending_title = ""
    return rows


def _format_transcript_row_doc(row: dict[str, str]) -> str:
    parts = [f"Course row: {row.get('course_code', '').strip()} | {row.get('course_title', '').strip()}"]
    if row.get("course_term"):
        parts.append(f"Term: {row['course_term']}")
    if row.get("course_school"):
        parts.append(f"School: {row['course_school']}")
    if row.get("course_credits"):
        parts.append(f"Credits: {row['course_credits']}")
    if row.get("course_grade"):
        parts.append(f"Grade: {row['course_grade']}")
    return " | ".join(parts)


def _should_extract_transcript_rows(source_path: str, page_text: str, doc_type: str) -> bool:
    if doc_type != "transcript":
        return False
    src = (source_path or "").lower()
    if "transcript" in src:
        return True
    return bool(_TRANSCRIPT_PAGE_ANCHOR_RE.search(page_text or ""))


def _chunks_from_pdf_pages(
    file_id: str,
    pages: list[ExtractedPage] | list[tuple[str, int]],
    source_path: str,
    mtime: float,
    file_hash: str | None = None,
) -> list[ChunkTuple]:
    """Build chunk list (id, doc, meta) for already-extracted PDF page tuples."""
    if pages and isinstance(pages[0], ExtractedPage):
        first_page_text = pages[0].text[:CONTENT_SAMPLE_LEN]
    else:
        first_page_text = (pages[0][0] if pages else "")[:CONTENT_SAMPLE_LEN]
    content_type = _doc_type_from_content(first_page_text)
    doc_type = content_type if content_type != "other" else _doc_type_from_path(source_path)
    out: list[ChunkTuple] = []
    for page in pages:
        page_extra_meta: dict[str, Any] = {}
        if isinstance(page, ExtractedPage):
            page_text = page.text
            page_num = page.page_num
            page_extra_meta = dict(page.meta or {})
        else:
            page_text, page_num = page
        if not page_text.strip():
            continue
        if _should_extract_transcript_rows(source_path, page_text, doc_type):
            transcript_rows = _extract_transcript_rows(page_text, source_path=source_path)
            if transcript_rows:
                for row_index, row in enumerate(transcript_rows, start=1):
                    doc = _format_transcript_row_doc(row)
                    chunk_id = _stable_chunk_id(source_path, mtime, page_num * 1000 + row_index)
                    meta: dict[str, Any] = {
                        "source": source_path,
                        "source_path": source_path,
                        "mtime": mtime,
                        "chunk_hash": _chunk_hash(doc),
                        "file_id": file_id,
                        "page": page_num,
                        "is_local": _is_local(source_path),
                        "doc_type": doc_type,
                        "content_extracted": 1,
                        "record_type": "transcript_row",
                        "course_code": row.get("course_code", ""),
                        "course_title": row.get("course_title", ""),
                        "course_term": row.get("course_term", ""),
                        "course_grade": row.get("course_grade", ""),
                        "course_credits": row.get("course_credits", ""),
                        "course_school": row.get("course_school", ""),
                        "student_name": row.get("student_name", ""),
                        "course_status": row.get("course_status", ""),
                    }
                    if file_hash:
                        meta["file_hash"] = file_hash
                    if page_extra_meta:
                        meta.update(page_extra_meta)
                    out.append((chunk_id, doc, meta))
                continue
        chunk_id = _stable_chunk_id(source_path, mtime, page_num)
        meta = {
            "source": source_path,
            "source_path": source_path,
            "mtime": mtime,
            "chunk_hash": _chunk_hash(page_text),
            "file_id": file_id,
            "page": page_num,
            "is_local": _is_local(source_path),
            "doc_type": doc_type,
            "content_extracted": 1,
        }
        if file_hash:
            meta["file_hash"] = file_hash
        if page_extra_meta:
            meta.update(page_extra_meta)
        out.append((chunk_id, page_text, meta))
    return out


def _chunks_from_pdf(
    file_id: str,
    pdf_bytes: bytes,
    source_path: str,
    mtime: float,
    file_hash: str | None = None,
) -> list[ChunkTuple]:
    """Build chunk list (id, doc, meta) for PDF by page. doc_type from first page content overrides path when not 'other'."""
    pages = get_text_from_pdf_bytes(pdf_bytes, source_path=source_path)
    return _chunks_from_pdf_pages(file_id, pages, source_path, mtime, file_hash=file_hash)


def _chunks_from_processor_text(
    file_id: str,
    result: str | ExtractedText,
    source_path: str,
    mtime: float,
    suffix: str,
    file_hash: str | None = None,
) -> list[ChunkTuple]:
    if isinstance(result, ExtractedText):
        text = result.text
        extra_meta = dict(result.meta or {})
    else:
        text = result
        extra_meta = None
    if suffix == ".csv":
        return _chunks_from_csv_text(file_id, text, source_path, mtime, file_hash=file_hash)
    return _chunks_from_content(file_id, text, source_path, mtime, prefix="", file_hash=file_hash, extra_meta=extra_meta)


def _chunks_from_processor_result(
    file_id: str,
    result: str | ExtractedText | ExtractedImage | list[ExtractedPage] | None,
    source_path: str,
    mtime: float,
    suffix: str,
    processor: DocumentProcessor,
    file_hash: str | None = None,
    db_path: str | Path | None = None,
) -> list[ChunkTuple]:
    if isinstance(result, list):
        return _chunks_from_pdf_pages(file_id, result, source_path, mtime, file_hash=file_hash)
    if isinstance(result, ExtractedImage):
        return _chunks_from_image_result(
            file_id,
            result,
            source_path,
            mtime,
            db_path=db_path,
            file_hash=file_hash,
        )
    if result is None:
        return _chunk_metadata_only(
            file_id, source_path, mtime,
            processor.format_label, processor.install_hint,
            file_hash=file_hash,
        )
    return _chunks_from_processor_text(file_id, result, source_path, mtime, suffix, file_hash=file_hash)


def _tag_zip_meta(chunks: list[ChunkTuple], zip_path: Path) -> list[ChunkTuple]:
    if not chunks:
        return chunks
    zp = str(zip_path)
    for _id, _doc, meta in chunks:
        meta["zip_path"] = zp
    return chunks


def _should_use_tqdm() -> bool:
    if tqdm is None:
        return False
    env_pref = os.environ.get("LLMLIBRARIAN_PROGRESS", "auto").strip().lower()
    if env_pref in ("1", "true", "yes"):
        return True
    if env_pref in ("0", "false", "no"):
        return False
    return sys.stderr.isatty()


def _batch_add(
    collection: Any,
    chunks: list[ChunkTuple],
    batch_size: int = ADD_BATCH_SIZE,
    no_color: bool = False,
    log_line: Any = None,
    embedding_fn: Any | None = None,
    embedding_workers: int = 1,
) -> None:
    """Add chunks to collection in batches. Progress printed so you can see where it hangs (embedding)."""
    if not chunks:
        return
    total_batches = (len(chunks) + batch_size - 1) // batch_size
    use_tqdm = _should_use_tqdm()
    iterator = range(0, len(chunks), batch_size)
    pbar = None
    if use_tqdm:
        pbar = tqdm(iterator, desc="Embedding", total=total_batches, leave=False)
        iterator = pbar
    if embedding_fn is None or embedding_workers <= 1:
        for i in iterator:
            batch = chunks[i : i + batch_size]
            batch_num = i // batch_size + 1
            if not use_tqdm:
                msg = f"  Adding batch {batch_num}/{total_batches} ({len(batch)} chunks)..."
                print(dim(no_color, msg))
                if log_line:
                    log_line(msg.strip())
            ids_b = [c[0] for c in batch]
            docs_b = [c[1] for c in batch]
            metas_b = [c[2] for c in batch]
            collection.add(ids=ids_b, documents=docs_b, metadatas=metas_b)
        return

    # Experimental: parallelize embedding computation, then add in main thread.
    def _embed_batch(batch: list[ChunkTuple]) -> tuple[list[str], list[str], list[dict[str, Any]], Any]:
        ids_b = [c[0] for c in batch]
        docs_b = [c[1] for c in batch]
        metas_b = [c[2] for c in batch]
        embeddings = embedding_fn(docs_b)
        return (ids_b, docs_b, metas_b, embeddings)

    with ThreadPoolExecutor(max_workers=embedding_workers) as executor:
        futures = {}
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i : i + batch_size]
            futures[executor.submit(_embed_batch, batch)] = i
        completed = 0
        for future in as_completed(futures):
            i = futures[future]
            batch_num = i // batch_size + 1
            if not use_tqdm:
                msg = f"  Adding batch {batch_num}/{total_batches} ({min(batch_size, len(chunks) - i)} chunks)..."
                print(dim(no_color, msg))
                if log_line:
                    log_line(msg.strip())
            ids_b, docs_b, metas_b, embeddings = future.result()
            collection.add(ids=ids_b, documents=docs_b, metadatas=metas_b, embeddings=embeddings)
            completed += 1
            if pbar is not None:
                pbar.update(1)
        if pbar is not None:
            pbar.close()




def process_one_file(
    path: Path,
    kind: str,
    file_hash: str | None = None,
    follow_symlinks: bool = False,
    path_resolved: Path | None = None,
    db_path: str | Path | None = None,
) -> list[ChunkTuple]:
    """
    Read file and return list of (id, doc, meta). Runs in worker thread.
    Uses processor registry from processors.py. Falls back to TextProcessor for code/text.
    """
    if path.is_symlink() and not follow_symlinks:
        return []
    if path_resolved is None:
        path_resolved = path.resolve()
    path_str = str(path_resolved)
    try:
        stat = path_resolved.stat()
        mtime = stat.st_mtime
    except OSError:
        return []
    file_id = path_resolved.name
    try:
        data = path_resolved.read_bytes()
    except OSError:
        return []
    suffix = path_resolved.suffix.lower()
    processor = PROCESSORS.get(suffix, DEFAULT_PROCESSOR)
    try:
        if isinstance(processor, ImageProcessor):
            result = processor.extract(data, path_str, enable_multimodal=True)
        else:
            result = processor.extract(data, path_str)
    except DocumentExtractionError as e:
        _log_event(
            "ERROR",
            "Extraction failed",
            path=path_str,
            kind=kind,
            error=str(e),
            extractor=getattr(processor, "format_label", "unknown"),
        )
        return []
    return _chunks_from_processor_result(
        file_id,
        result,
        path_str,
        mtime,
        suffix,
        processor,
        file_hash=file_hash,
        db_path=db_path,
    )


def collect_files(
    root: Path,
    include: list[str],
    exclude: list[str],
    max_depth: int,
    max_file_bytes: int,
    current_depth: int = 0,
    follow_symlinks: bool = False,
    stats: dict[str, Any] | None = None,
) -> list[tuple[Path, str]]:
    """
    Walk tree and return list of (path, kind) for files to index.
    kind: 'code' | 'pdf' | 'docx' | 'xlsx' | 'pptx' | 'zip' | 'image'. Does not read file contents.
    """
    out: list[tuple[Path, str]] = []
    if current_depth > max_depth:
        return out
    try:
        for item in sorted(root.iterdir()):
            if item.name.startswith("."):
                continue
            if item.is_symlink() and not follow_symlinks:
                continue
            path_str = str(item)
            if item.is_dir():
                if not should_descend_into_dir(path_str, exclude):
                    continue
                out.extend(
                    collect_files(
                        item,
                        include,
                        exclude,
                        max_depth,
                        max_file_bytes,
                        current_depth + 1,
                        follow_symlinks=follow_symlinks,
                        stats=stats,
                    )
                )
            else:
                if not should_index(path_str, include, exclude):
                    if stats is not None:
                        suf = item.suffix.lower()
                        if suf in _PREVIEW_SKIPPED_EXTENSIONS:
                            skipped = stats.setdefault("skipped_ext", {})
                            skipped[suf] = int(skipped.get(suf, 0)) + 1
                    continue
                try:
                    if item.stat().st_size > max_file_bytes:
                        continue
                except OSError:
                    continue
                suf = item.suffix.lower()
                if suf == ".zip":
                    kind = "zip"
                elif suf == ".pdf":
                    kind = "pdf"
                elif suf == ".docx":
                    kind = "docx"
                elif suf == ".xlsx":
                    kind = "xlsx"
                elif suf == ".pptx":
                    kind = "pptx"
                elif suf in IMAGE_EXTENSIONS:
                    kind = "image"
                else:
                    kind = "code"
                out.append((item, kind))
                if stats is not None:
                    supported = stats.setdefault("supported_kind", {})
                    supported[kind] = int(supported.get(kind, 0)) + 1
    except PermissionError:
        pass
    except OSError:
        pass
    return out


def _format_preflight_summary(file_list: list[tuple[Path, str]], stats: dict[str, Any] | None) -> str:
    if not file_list:
        return "  No supported files found."
    def _count_label(count: int, singular: str, plural: str | None = None) -> str:
        word = singular if count == 1 else (plural or f"{singular}s")
        return f"{count} {word}"

    supported = dict((stats or {}).get("supported_kind") or {})
    parts: list[str] = []
    image_count = int(supported.get("image") or 0)
    if image_count:
        parts.append(_count_label(image_count, "image"))
    zip_count = int(supported.get("zip") or 0)
    if zip_count:
        parts.append(_count_label(zip_count, "zip"))
    doc_count = sum(int(supported.get(kind) or 0) for kind in ("pdf", "docx", "xlsx", "pptx"))
    if doc_count:
        parts.append(_count_label(doc_count, "doc"))
    code_count = int(supported.get("code") or 0)
    if code_count:
        parts.append(_count_label(code_count, "text/code"))
    if not parts:
        parts.append(f"{len(file_list)} supported files")
    msg = "  Preflight: " + ", ".join(parts)
    skipped = dict((stats or {}).get("skipped_ext") or {})
    skipped_parts = [
        f"{_count_label(int(count), ext.lstrip('.'))} skipped"
        for ext, count in sorted(skipped.items(), key=lambda kv: (-int(kv[1]), kv[0]))
        if int(count) > 0
    ]
    if skipped_parts:
        msg += " | " + ", ".join(skipped_parts[:4])
    return msg


def _image_progress_snapshot(chunks: list[ChunkTuple]) -> tuple[str | None, bool]:
    summary_status: str | None = None
    has_image = False
    for _cid, _doc, meta in chunks:
        if str((meta or {}).get("source_modality") or "") != "image":
            continue
        has_image = True
        if str((meta or {}).get("record_type") or "") == "image_summary":
            summary_status = str((meta or {}).get("summary_status") or "") or None
            break
    return summary_status, has_image


def _print_image_progress(
    *,
    image_done: int,
    image_total: int,
    eager_count: int,
    deferred_count: int,
    embeddings_complete: int,
    started_at: float,
    no_color: bool,
) -> None:
    if image_total <= 0:
        return
    elapsed = max(0.001, time.perf_counter() - started_at)
    rate = image_done / elapsed if image_done else 0.0
    remaining = max(0, image_total - image_done)
    eta = remaining / rate if rate > 0 else 0.0
    msg = (
        "  Image progress: "
        f"{image_done}/{image_total} | "
        f"ocr complete={image_done} | "
        f"eager summaries={eager_count} | "
        f"deferred summaries={deferred_count} | "
        f"image embeddings complete={embeddings_complete} | "
        f"elapsed={elapsed:.1f}s | eta={eta:.1f}s"
    )
    print(dim(no_color, msg))


def process_zip_to_chunks(
    zip_path: Path,
    include: list[str],
    exclude: list[str],
    max_archive_bytes: int,
    max_file_bytes: int,
    max_files_per_zip: int,
    max_extracted_per_zip: int,
    db_path: str | Path | None = None,
) -> list[ChunkTuple]:
    """
    Extract supported files from ZIP: PDF, DOCX, XLSX, PPTX + text (by extension). Limits and skip encrypted.
    """
    if zip_path.stat().st_size > max_archive_bytes:
        return []
    out: list[ChunkTuple] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as z:
            for info in z.infolist():
                if info.flag_bits & 0x1:
                    return []  # encrypted
            count = 0
            extracted_bytes = 0
            for info in z.infolist():
                if info.filename.endswith("/") or info.file_size == 0:
                    continue
                # Symlink entries can point outside intended locations; never index them.
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    _log_event(
                        "WARN",
                        "Skipped ZIP symlink entry",
                        path=str(zip_path),
                        entry=info.filename,
                    )
                    continue
                if not is_safe_path(zip_path.parent, info.filename):
                    _log_event(
                        "WARN",
                        "Skipped unsafe ZIP entry",
                        path=str(zip_path),
                        entry=info.filename,
                    )
                    continue
                if count >= max_files_per_zip or extracted_bytes >= max_extracted_per_zip:
                    break
                if not should_index(info.filename, include, exclude):
                    continue
                if info.filename.lower().endswith(".zip"):
                    continue
                with z.open(info.filename) as f:
                    data = f.read()
                extracted_bytes += len(data)
                if len(data) > max_file_bytes:
                    continue
                source_label = f"{zip_path} > {info.filename}"
                file_id = f"{zip_path.name}/{Path(info.filename).name}"
                mtime = 0.0
                suf = Path(info.filename).suffix.lower()
                processor = PROCESSORS.get(suf)
                if processor is not None:
                    try:
                        if isinstance(processor, ImageProcessor):
                            result = processor.extract(data, source_label, enable_multimodal=False)
                        else:
                            result = processor.extract(data, source_label)
                        out.extend(
                            _tag_zip_meta(
                                _chunks_from_processor_result(
                                    file_id,
                                    result,
                                    source_label,
                                    mtime,
                                    suf,
                                    processor,
                                    db_path=db_path,
                                ),
                                zip_path,
                            )
                        )
                    except DocumentExtractionError as e:
                        _log_event(
                            "ERROR",
                            "Document extraction failed in ZIP",
                            path=str(zip_path),
                            entry=info.filename,
                            error=str(e),
                            extractor=getattr(processor, "format_label", "unknown"),
                        )
                elif suf == ".csv":
                    try:
                        text = data.decode("utf-8", errors="replace")
                        if text.strip():
                            out.extend(_tag_zip_meta(_chunks_from_csv_text(file_id, text, source_label, mtime), zip_path))
                    except Exception:
                        pass
                elif suf in ZIP_TEXT_EXTENSIONS:
                    try:
                        text = data.decode("utf-8", errors="replace")
                        if text.strip():
                            out.extend(_tag_zip_meta(_chunks_from_content(file_id, text, source_label, mtime, prefix=""), zip_path))
                    except Exception:
                        pass
                count += 1
    except (zipfile.BadZipFile, OSError):
        return []
    return out


def _make_log_writer(log_path: str | Path | None) -> tuple[Any, Any]:
    """
    If log_path or LLMLIBRARIAN_LOG is set, return (log_line, close_fn).
    log_line(msg) writes timestamp + msg to the log file; close_fn() closes it.
    Otherwise return (lambda msg: None, lambda: None).
    LLMLIBRARIAN_LOG=1 or --log with no path -> llmlibrarian_index.log in cwd.
    """
    path = log_path if log_path is not None else os.environ.get("LLMLIBRARIAN_LOG")
    if path is None or path is False:
        return (lambda msg: None, lambda: None)
    if path is True or (isinstance(path, str) and path.strip().lower() in ("1", "true", "yes", "")):
        p = Path.cwd() / "llmlibrarian_index.log"
    else:
        p = Path(path)
    try:
        f = open(p, "a", encoding="utf-8")
    except OSError:
        return (lambda msg: None, lambda: None)

    def log_line(msg: str) -> None:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
        f.flush()

    def close_fn() -> None:
        f.close()

    return (log_line, close_fn)


def _flatten_collection_list(values: list[Any] | None) -> list[Any]:
    if not values:
        return []
    if values and isinstance(values[0], list):
        return values[0]  # type: ignore[index]
    return values


def _get_image_collection(client: Any, base_collection_name: str = LLMLI_COLLECTION) -> Any:
    return client.get_or_create_collection(name=image_collection_name(base_collection_name))


def _delete_source_from_collections(
    *,
    collection: Any,
    image_collection: Any | None,
    silo_slug: str,
    source_path: str,
) -> None:
    try:
        collection.delete(where={"$and": [{"silo": silo_slug}, {"source": source_path}]})
    except Exception as e:
        _log_event("WARN", "Failed to delete updated file chunks", path=source_path, error=str(e))
    try:
        collection.delete(where={"$and": [{"silo": silo_slug}, {"zip_path": source_path}]})
    except Exception:
        pass
    if image_collection is None:
        return
    try:
        image_collection.delete(where={"$and": [{"silo": silo_slug}, {"source": source_path}]})
    except Exception as e:
        _log_event("WARN", "Failed to delete image vector rows", path=source_path, error=str(e))


def _batch_add_image_vectors(
    collection: Any,
    rows: list[ImageVectorTuple],
    *,
    batch_size: int = ADD_BATCH_SIZE,
    no_color: bool = False,
    log_line: Any = None,
) -> None:
    if not rows:
        return
    adapter = ensure_image_embedding_adapter_ready()
    total_batches = (len(rows) + batch_size - 1) // batch_size
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        batch_num = i // batch_size + 1
        msg = f"  Adding image batch {batch_num}/{total_batches} ({len(batch)} images)..."
        print(dim(no_color, msg))
        if log_line:
            log_line(msg.strip())
        ids_b = [row[0] for row in batch]
        paths_b = [row[1] for row in batch]
        docs_b = [row[2] for row in batch]
        metas_b = [row[3] for row in batch]
        embeddings = adapter.embed_image_paths(paths_b)
        collection.add(ids=ids_b, documents=docs_b, metadatas=metas_b, embeddings=embeddings)


def _clone_chunks_from_existing_silo(
    *,
    collection: Any,
    from_silo: str,
    source_path: str,
    target_silo: str,
) -> list[ChunkTuple]:
    """
    Best-effort chunk clone for cross-silo dedupe.
    Returns chunk tuples shaped like process_one_file output.
    """
    try:
        result = collection.get(
            where={"$and": [{"silo": from_silo}, {"source": source_path}]},
            include=["documents", "metadatas"],
        )
    except Exception:
        return []
    docs = _flatten_collection_list(result.get("documents"))
    metas = _flatten_collection_list(result.get("metadatas"))
    out: list[ChunkTuple] = []
    for i, doc_raw in enumerate(docs):
        doc = str(doc_raw or "")
        if not doc:
            continue
        meta_raw = metas[i] if i < len(metas) else {}
        meta = dict(meta_raw) if isinstance(meta_raw, dict) else {}
        src = str(meta.get("source") or "")
        if src != source_path:
            continue
        cloned_meta = dict(meta)
        cloned_meta["silo"] = target_silo
        chunk_id = str(meta.get("chunk_hash") or f"clone-{i}-{source_path}")
        out.append((chunk_id, doc, cloned_meta))
    return out


def _clone_image_vectors_from_existing_silo(
    *,
    collection: Any,
    from_silo: str,
    source_path: str,
    target_silo: str,
) -> list[ImageVectorTuple]:
    try:
        result = collection.get(
            where={"$and": [{"silo": from_silo}, {"source": source_path}]},
            include=["documents", "metadatas"],
        )
    except Exception:
        return []
    docs = _flatten_collection_list(result.get("documents"))
    metas = _flatten_collection_list(result.get("metadatas"))
    out: list[ImageVectorTuple] = []
    for i, doc_raw in enumerate(docs):
        doc = str(doc_raw or "")
        meta_raw = metas[i] if i < len(metas) else {}
        meta = dict(meta_raw) if isinstance(meta_raw, dict) else {}
        src = str(meta.get("source") or "")
        if src != source_path:
            continue
        cloned_meta = dict(meta)
        cloned_meta["silo"] = target_silo
        row_id = str(meta.get("parent_image_id") or meta.get("chunk_hash") or f"image-clone-{i}-{source_path}")
        out.append((_image_vector_id(row_id), source_path, doc, cloned_meta))
    return out


def run_index(
    archetype_id: str,
    config_path: str | Path | None = None,
    no_color: bool = False,
    log_path: str | Path | None = None,
    mode: str = "normal",
    follow_symlinks: bool = False,
) -> None:
    try:
        from floor import print_resources
        print_resources(DB_PATH, mode=mode, reranker_loaded=False, no_color=no_color)
    except Exception:
        pass
    workers = {"fast": 4, "normal": MAX_WORKERS, "deep": MAX_WORKERS}.get(mode.lower(), MAX_WORKERS)

    config = load_config(config_path)
    arch = get_archetype(config, archetype_id)
    limits_cfg = config.get("limits") or {}
    max_file_bytes = int(
        (limits_cfg.get("max_file_size_mb") or 20) * 1024 * 1024
    )
    max_depth = int(limits_cfg.get("max_depth") or DEFAULT_MAX_DEPTH)
    max_archive_bytes = int(
        (limits_cfg.get("max_archive_size_mb") or 100) * 1024 * 1024
    )
    max_files_per_zip = int(limits_cfg.get("max_files_per_zip") or DEFAULT_MAX_FILES_PER_ZIP)
    max_extracted_per_zip = int(
        limits_cfg.get("max_extracted_bytes_per_zip") or DEFAULT_MAX_EXTRACTED_BYTES_PER_ZIP
    )

    include = arch.get("include") or []
    exclude = arch.get("exclude") or []
    folders = arch.get("folders") or []
    collection_name = arch["collection"]

    ef = get_embedding_function()
    client = chromadb.PersistentClient(path=DB_PATH, settings=Settings(anonymized_telemetry=False))

    try:
        client.delete_collection(name=collection_name)
    except Exception:
        pass  # collection may not exist
    try:
        client.delete_collection(name=image_collection_name(collection_name))
    except Exception:
        pass
    collection = client.get_or_create_collection(
        name=collection_name,
        embedding_function=ef,
    )
    image_collection = _get_image_collection(client, collection_name)

    log_line, log_close = _make_log_writer(log_path)
    t0 = time.perf_counter()

    def log(msg: str) -> None:
        log_line(msg)

    print(label_style(no_color, f"Indexing archetype: {arch.get('name', archetype_id)} ({collection_name})"))
    log(f"Indexing archetype: {arch.get('name', archetype_id)} ({collection_name})")
    print(dim(no_color, f"  Folders: {folders}"))

    # 1. Collect file list (path, kind) from all folders
    log("Collecting file list...")
    file_list: list[tuple[Path, str]] = []
    for folder in folders:
        p = Path(folder)
        if p.is_symlink() and not follow_symlinks:
            print(warn_style(no_color, f"  ⚠️ Skipping symlinked folder: {folder} (use --follow-symlinks to allow)"))
            continue
        if not p.exists():
            print(warn_style(no_color, f"  ⚠️ Folder does not exist: {folder}"))
            continue
        if not p.is_dir():
            print(warn_style(no_color, f"  ⚠️ Not a directory: {folder}"))
            continue
        print(dim(no_color, f"  📁 {folder}"))
        file_list.extend(collect_files(p, include, exclude, max_depth, max_file_bytes, follow_symlinks=follow_symlinks))

    log(f"Collected {len(file_list)} files")
    if _requires_standalone_image_enrichment(file_list):
        ensure_vision_model_ready()
        ensure_image_embedding_adapter_ready()
    # 2. Split: regular files (parallel) vs zips (main thread, limits)
    regular = [(path, kind) for path, kind in file_list if kind != "zip"]
    zips = [path for path, kind in file_list if kind == "zip"]

    all_chunks: list[ChunkTuple] = []
    all_image_vectors: list[ImageVectorTuple] = []
    files_indexed = 0

    # 3. Process regular files in parallel
    log("Processing files (parallel)...")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_item = {}
        for path, kind in regular:
            try:
                p_res = path.resolve()
            except OSError:
                p_res = None
            future_to_item[executor.submit(process_one_file, path, kind, None, follow_symlinks, p_res)] = (path, kind)
        for future in as_completed(future_to_item):
            path, kind = future_to_item[future]
            try:
                chunks = future.result()
            except Exception as e:
                _log_event(
                    "ERROR",
                    "Worker failed",
                    path=str(path),
                    kind=kind,
                    error=str(e),
                    traceback=traceback.format_exc(),
                )
                print(f"[llmlibrarian] FAIL {path}: {e}", file=sys.stderr)
                continue
            if chunks:
                all_chunks.extend(chunks)
                if any(str((meta or {}).get("source_modality") or "") == "image" for _cid, _doc, meta in chunks):
                    source_path = str((chunks[0][2] or {}).get("source") or path)
                    vector_row = _image_vector_from_chunks(source_path=source_path, chunks=chunks)
                    if vector_row is not None:
                        all_image_vectors.append(vector_row)
                files_indexed += 1
                print(success_style(no_color, f"  ✅ {path.name} ({len(chunks)} chunks)"))
            elif kind == "pdf" and not _suppress_recoverable_warnings():
                print(
                    warn_style(
                        no_color,
                        f"  ⚠️ {path.name}: no extractable text (image-only, empty, or OCR unavailable)",
                    )
                )

    # 4. Process ZIPs in main thread (limits, encrypted check)
    for zip_path in zips:
        if zip_path.stat().st_size > max_archive_bytes:
            print(warn_style(no_color, f"  ⚠️ Skip ZIP (over max size): {zip_path.name}"))
            continue
        encrypted = False
        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                for info in z.infolist():
                    if info.flag_bits & 0x1:
                        encrypted = True
                        break
        except zipfile.BadZipFile as e:
            print(warn_style(no_color, f"  ⚠️ Bad ZIP: {zip_path.name} — {e}"))
            continue
        except Exception as e:
            _log_event(
                "ERROR",
                "ZIP inspection failed",
                path=str(zip_path),
                error=str(e),
                traceback=traceback.format_exc(),
            )
            print(warn_style(no_color, f"  ⚠️ ZIP error: {zip_path.name} — {e}"))
            continue
        if encrypted:
            print(warn_style(no_color, f"  ⚠️ Skip ZIP (encrypted): {zip_path.name}"))
            continue
        chunks = process_zip_to_chunks(
            zip_path,
            include,
            exclude,
            max_archive_bytes,
            max_file_bytes,
            max_files_per_zip,
            max_extracted_per_zip,
            db_path=DB_PATH,
        )
        if chunks:
            all_chunks.extend(chunks)
            files_indexed += 1
            print(success_style(no_color, f"  ✅ {zip_path.name} ({len(chunks)} chunks)"))

    # 5. Batch add to ChromaDB (embedding runs here; progress so you can see where it hangs)
    if all_chunks:
        batch_size = ADD_BATCH_SIZE
        try:
            batch_size = int(os.environ.get("LLMLIBRARIAN_ADD_BATCH_SIZE", batch_size))
        except (ValueError, TypeError):
            pass
        batch_size = max(1, min(batch_size, 2000))  # sane range
        try:
            embedding_workers = int(os.environ.get("LLMLIBRARIAN_EMBEDDING_WORKERS", "1"))
        except (TypeError, ValueError):
            embedding_workers = 1
        embedding_workers = max(1, min(embedding_workers, 32))
        if not _should_use_tqdm():
            log(f"Adding {len(all_chunks)} chunks to collection (embedding, batch_size={batch_size})...")
        _batch_add(
            collection,
            all_chunks,
            batch_size=batch_size,
            no_color=no_color,
            log_line=log_line,
            embedding_fn=ef,
            embedding_workers=embedding_workers,
        )
    if all_image_vectors:
        _batch_add_image_vectors(
            image_collection,
            all_image_vectors,
            batch_size=max(1, min(64, ADD_BATCH_SIZE)),
            no_color=no_color,
            log_line=log_line,
        )

    elapsed = time.perf_counter() - t0
    done_msg = f"Done: {files_indexed} files, {len(all_chunks)} chunks in {elapsed:.1f}s"
    print(bold(no_color, done_msg))
    log(done_msg)
    log_close()


def run_add(
    path: str | Path,
    db_path: str | Path | None = None,
    no_color: bool = False,
    allow_cloud: bool = False,
    follow_symlinks: bool = False,
    incremental: bool = True,
    forced_silo_slug: str | None = None,
    display_name_override: str | None = None,
) -> tuple[int, int]:
    """
    Index a single folder into the unified collection (llmli). Silo name = basename(path) unless forced.
    Returns (files_indexed, failed_count). Failures saved for llmli log --last.
    Refuses cloud-sync roots (OneDrive, iCloud, Dropbox, etc.) unless allow_cloud=True.

    If interrupted (e.g. Ctrl+C): Chroma may have 0 or partial chunks for this silo;
    the registry is only updated on success. Re-run add for the same path to get a consistent state.
    """
    from state import update_silo, set_last_failures, slugify, resolve_silo_by_path

    db_path = db_path or DB_PATH
    path = Path(path)
    if path.is_symlink() and not follow_symlinks:
        raise ValueError(f"Refusing to follow symlinked path: {path}. Use --follow-symlinks to allow.")
    path = path.resolve()
    if not path.is_dir():
        raise NotADirectoryError(f"Not a directory: {path}")
    if not allow_cloud:
        cloud_kind = is_cloud_sync_path(path)
        if cloud_kind:
            raise CloudSyncPathError(path, cloud_kind)
    display_name = display_name_override or path.name
    if forced_silo_slug:
        silo_slug = forced_silo_slug
    else:
        existing_slug = resolve_silo_by_path(db_path, path)
        silo_slug = existing_slug if existing_slug else slugify(display_name, str(path))
    limits_cfg = {}
    try:
        config = load_config()
        limits_cfg = config.get("limits") or {}
    except Exception:
        pass
    max_file_bytes = int((limits_cfg.get("max_file_size_mb") or 20) * 1024 * 1024)
    max_depth = int(limits_cfg.get("max_depth") or DEFAULT_MAX_DEPTH)
    max_archive_bytes = int((limits_cfg.get("max_archive_size_mb") or 100) * 1024 * 1024)
    max_files_per_zip = int(limits_cfg.get("max_files_per_zip") or DEFAULT_MAX_FILES_PER_ZIP)
    max_extracted_per_zip = int(limits_cfg.get("max_extracted_bytes_per_zip") or DEFAULT_MAX_EXTRACTED_BYTES_PER_ZIP)
    quiet = os.environ.get("LLMLIBRARIAN_QUIET", "").strip().lower() in ("1", "true", "yes")

    collect_stats: dict[str, Any] = {}
    file_list = collect_files(
        path,
        ADD_DEFAULT_INCLUDE,
        ADD_DEFAULT_EXCLUDE,
        max_depth,
        max_file_bytes,
        follow_symlinks=follow_symlinks,
        stats=collect_stats,
    )
    if _requires_standalone_image_enrichment(file_list):
        ensure_vision_model_ready()
        ensure_image_embedding_adapter_ready()
    regular = [(p, k) for p, k in file_list if k != "zip"]
    zips = [p for p, k in file_list if k == "zip"]

    workers = max(1, min(MAX_WORKERS, (os.cpu_count() or 8)))
    try:
        workers = int(os.environ.get("LLMLIBRARIAN_MAX_WORKERS", workers))
    except (TypeError, ValueError):
        pass
    workers = max(1, min(workers, 32))
    try:
        embedding_workers = int(os.environ.get("LLMLIBRARIAN_EMBEDDING_WORKERS", "8"))
    except (TypeError, ValueError):
        embedding_workers = 1
    embedding_workers = max(1, min(embedding_workers, 32))
    if not quiet:
        print(dim(no_color, _format_preflight_summary(file_list, collect_stats)))
        print(dim(no_color, f"  Workers: file={workers}, embedding={embedding_workers}"))

    ef = get_embedding_function()
    client = chromadb.PersistentClient(path=str(db_path), settings=Settings(anonymized_telemetry=False))
    collection = client.get_or_create_collection(name=LLMLI_COLLECTION, embedding_function=ef)
    image_collection = _get_image_collection(client)

    if not incremental:
        try:
            collection.delete(where={"silo": silo_slug})
        except Exception:
            pass  # no chunks for this silo yet
        try:
            image_collection.delete(where={"silo": silo_slug})
        except Exception:
            pass
        _file_registry_remove_silo(db_path, silo_slug)

    # Pre-pass: resolve paths, hash, skip duplicates (same file already indexed in any silo)
    regular_with_hash: list[tuple[Path, str, str, Path | None]] = []
    manifest = _read_file_manifest(db_path) if incremental else {"silos": {}}
    silo_manifest = (manifest.get("silos") or {}).get(silo_slug, {})
    manifest_files = (silo_manifest.get("files") or {}) if isinstance(silo_manifest, dict) else {}
    ledger_sources_to_replace: set[str] = set()
    current_paths: set[str] = set()
    if incremental:
        for zp in zips:
            try:
                current_paths.add(str(zp.resolve()))
            except OSError:
                current_paths.add(str(zp))
    skipped = 0
    precloned_by_path: dict[str, tuple[str, list[ChunkTuple]]] = {}
    precloned_image_vectors_by_path: dict[str, list[ImageVectorTuple]] = {}
    for p, k in regular:
        try:
            p_res = p.resolve()
            if not p_res.is_file():
                continue
            current_paths.add(str(p_res))
            stat = p_res.stat()
            mtime = stat.st_mtime
            size = stat.st_size
            h = get_file_hash(p_res)
            if incremental:
                prev = manifest_files.get(str(p_res)) if isinstance(manifest_files, dict) else None
                if prev and prev.get("mtime") == mtime and prev.get("size") == size:
                    if not h:
                        continue
                    existing_same = any(
                        str(e.get("silo") or "") == silo_slug and str(e.get("path") or "") == str(p_res)
                        for e in _file_registry_get(db_path, h)
                    )
                    if existing_same:
                        continue
            if h:
                existing_entries = _file_registry_get(db_path, h)
                clone_from = next(
                    (str(e.get("silo") or "") for e in existing_entries if str(e.get("silo") or "") != silo_slug),
                    "",
                )
                if clone_from:
                    cloned = _clone_chunks_from_existing_silo(
                        collection=collection,
                        from_silo=clone_from,
                        source_path=str(p_res),
                        target_silo=silo_slug,
                    )
                    if cloned:
                        precloned_by_path[str(p_res)] = (h, cloned)
                        precloned_image_vectors_by_path[str(p_res)] = _clone_image_vectors_from_existing_silo(
                            collection=image_collection,
                            from_silo=clone_from,
                            source_path=str(p_res),
                            target_silo=silo_slug,
                        )
                        continue
            if not h:
                regular_with_hash.append((p, k, "", p_res))
                continue
            regular_with_hash.append((p, k, h, p_res))
        except OSError:
            regular_with_hash.append((p, k, "", None))

    if incremental and isinstance(manifest_files, dict):
        cleanup_targets: list[Path] = [p_res for _p, _k, _h, p_res in regular_with_hash if p_res is not None]
        for p_res in cleanup_targets:
            if p_res is None:
                continue
            path_str = str(p_res)
            ledger_sources_to_replace.add(path_str)
            if path_str in manifest_files:
                _delete_source_from_collections(
                    collection=collection,
                    image_collection=image_collection,
                    silo_slug=silo_slug,
                    source_path=path_str,
                )
                prev = manifest_files.get(path_str) or {}
                _file_registry_remove_path(db_path, silo_slug, path_str, prev.get("hash"))

    all_chunks = []
    all_image_vectors: list[ImageVectorTuple] = []
    tax_rows: list[dict[str, Any]] = []
    files_indexed = 0
    failures = []
    to_register: list[tuple[str, str]] = []  # (file_hash, path_str) for main-thread registry update
    image_total = sum(1 for _p, kind in regular if kind == "image")
    image_done = 0
    eager_summaries = 0
    deferred_summaries = 0
    image_embeddings_complete = 0
    extraction_started_at = time.perf_counter()
    last_image_progress_at = extraction_started_at

    if incremental and isinstance(manifest_files, dict):
        removed = [path_str for path_str in manifest_files.keys() if path_str not in current_paths]
        for path_str in removed:
            ledger_sources_to_replace.add(path_str)
            _delete_source_from_collections(
                collection=collection,
                image_collection=image_collection,
                silo_slug=silo_slug,
                source_path=path_str,
            )
            prev = manifest_files.get(path_str) or {}
            _file_registry_remove_path(db_path, silo_slug, path_str, prev.get("hash"))

    if precloned_by_path:
        for path_str, (fhash, cloned_chunks) in precloned_by_path.items():
            cloned_norm = [
                (
                    hashlib.sha256(f"{silo_slug}|{cid}".encode()).hexdigest()[:20],
                    doc,
                    {**meta, "silo": silo_slug},
                )
                for cid, doc, meta in cloned_chunks
            ]
            if not cloned_norm:
                continue
            all_chunks.extend(cloned_norm)
            cloned_vectors = precloned_image_vectors_by_path.get(path_str) or []
            if cloned_vectors:
                all_image_vectors.extend(cloned_vectors)
            summary_status, has_image = _image_progress_snapshot(cloned_norm)
            if has_image:
                image_done += 1
                if summary_status == "eager":
                    eager_summaries += 1
                else:
                    deferred_summaries += 1
            tax_rows.extend(extract_tax_rows_from_chunks(cloned_norm))
            files_indexed += 1
            if fhash:
                to_register.append((fhash, path_str))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_item = {
            executor.submit(process_one_file, p, k, h, follow_symlinks, p_res, db_path): (p, k, h)
            for p, k, h, p_res in regular_with_hash
        }
        for future in as_completed(future_to_item):
            p, kind, fhash = future_to_item[future]
            try:
                chunks = future.result()
            except Exception as e:
                _log_event(
                    "ERROR",
                    "Worker failed",
                    path=str(p),
                    kind=kind,
                    error=str(e),
                    traceback=traceback.format_exc(),
                )
                failures.append({"path": str(p), "error": str(e)})
                print(f"[llmli] FAIL {p}: {e}", file=sys.stderr)
                continue
            if chunks:
                chunks = [
                    (hashlib.sha256(f"{silo_slug}|{cid}".encode()).hexdigest()[:20], doc, {**meta, "silo": silo_slug})
                    for cid, doc, meta in chunks
                ]
                all_chunks.extend(chunks)
                summary_status, has_image = _image_progress_snapshot(chunks)
                if has_image:
                    source_path = str((chunks[0][2] or {}).get("source") or p)
                    vector_row = _image_vector_from_chunks(source_path=source_path, chunks=chunks)
                    if vector_row is not None:
                        vid, vpath, vdoc, vmeta = vector_row
                        all_image_vectors.append((vid, vpath, vdoc, {**vmeta, "silo": silo_slug}))
                    image_done += 1
                    if summary_status == "eager":
                        eager_summaries += 1
                    else:
                        deferred_summaries += 1
                    if not quiet:
                        now = time.perf_counter()
                        if image_done == image_total or (now - last_image_progress_at) >= 2.5:
                            _print_image_progress(
                                image_done=image_done,
                                image_total=image_total,
                                eager_count=eager_summaries,
                                deferred_count=deferred_summaries,
                                embeddings_complete=image_embeddings_complete,
                                started_at=extraction_started_at,
                                no_color=no_color,
                            )
                            last_image_progress_at = now
                elif kind == "image":
                    image_done += 1
                tax_rows.extend(extract_tax_rows_from_chunks(chunks))
                files_indexed += 1
                if fhash:
                    to_register.append((fhash, str(p)))
            elif kind == "pdf" and not _suppress_recoverable_warnings():
                print(
                    warn_style(
                        no_color,
                        f"  ⚠️ {p.name}: no extractable text (image-only, empty, or OCR unavailable)",
                    ),
                    file=sys.stderr,
                )
            elif kind == "image":
                image_done += 1

    for fhash, path_str in to_register:
        _file_registry_add(db_path, fhash, silo_slug, path_str)

    for zip_path in zips:
        if zip_path.stat().st_size > max_archive_bytes:
            continue
        ledger_sources_to_replace.add(str(zip_path))
        if incremental:
            try:
                zstat = zip_path.stat()
                prev = manifest_files.get(str(zip_path)) if isinstance(manifest_files, dict) else None
                if prev and prev.get("mtime") == zstat.st_mtime and prev.get("size") == zstat.st_size:
                    continue
                try:
                    collection.delete(where={"$and": [{"silo": silo_slug}, {"zip_path": str(zip_path)}]})
                except Exception as e:
                    _log_event("WARN", "Failed to delete ZIP chunks", path=str(zip_path), error=str(e))
            except OSError:
                pass
        try:
            chunks = process_zip_to_chunks(
                zip_path, ADD_DEFAULT_INCLUDE, ADD_DEFAULT_EXCLUDE,
                max_archive_bytes, max_file_bytes, max_files_per_zip, max_extracted_per_zip,
                db_path=db_path,
            )
        except Exception as e:
            _log_event(
                "ERROR",
                "ZIP processing failed",
                path=str(zip_path),
                error=str(e),
                traceback=traceback.format_exc(),
            )
            failures.append({"path": str(zip_path), "error": str(e)})
            print(f"[llmli] FAIL {zip_path}: {e}", file=sys.stderr)
            continue
        if chunks:
            chunks = [
                (hashlib.sha256(f"{silo_slug}|{cid}".encode()).hexdigest()[:20], doc, {**meta, "silo": silo_slug})
                for cid, doc, meta in chunks
            ]
            all_chunks.extend(chunks)
            tax_rows.extend(extract_tax_rows_from_chunks(chunks))
            files_indexed += 1

    if regular_with_hash:
        for _p, _k, _h, p_res in regular_with_hash:
            if p_res is not None:
                ledger_sources_to_replace.add(str(p_res))

    if incremental:
        def _update_manifest(manifest_data: dict) -> None:
            silos = manifest_data.setdefault("silos", {})
            silo_entry = silos.setdefault(silo_slug, {"path": str(path), "files": {}})
            files_map = silo_entry.setdefault("files", {})
            if not isinstance(files_map, dict):
                files_map = {}
                silo_entry["files"] = files_map
            # Remove deleted
            for path_str in list(files_map.keys()):
                if path_str not in current_paths and path_str not in [str(z) for z in zips]:
                    del files_map[path_str]
            # Update regular files
            for p, _k, h, p_res in regular_with_hash:
                if p_res is None:
                    continue
                try:
                    st = p_res.stat()
                    files_map[str(p_res)] = {"mtime": st.st_mtime, "size": st.st_size, "hash": h}
                except OSError:
                    continue
            # Update zips
            for zp in zips:
                try:
                    st = zp.stat()
                    files_map[str(zp)] = {"mtime": st.st_mtime, "size": st.st_size, "hash": ""}
                except OSError:
                    continue

        _update_file_manifest(db_path, _update_manifest)

    if all_chunks:
        batch_size = ADD_BATCH_SIZE
        try:
            batch_size = int(os.environ.get("LLMLIBRARIAN_ADD_BATCH_SIZE", batch_size))
        except (TypeError, ValueError):
            pass
        batch_size = max(1, min(batch_size, 2000))
        total_batches = (len(all_chunks) + batch_size - 1) // batch_size
        if not _should_use_tqdm():
            print(dim(no_color, f"  Adding {len(all_chunks)} chunks in {total_batches} batches (batch_size={batch_size})..."))
        _batch_add(
            collection,
            all_chunks,
            batch_size=batch_size,
            no_color=no_color,

            embedding_workers=embedding_workers,
        )
    if all_image_vectors:
        _batch_add_image_vectors(
            image_collection,
            all_image_vectors,
            batch_size=max(1, min(64, ADD_BATCH_SIZE)),
            no_color=no_color,
        )
        image_embeddings_complete = len(all_image_vectors)
        if not quiet and image_total:
            _print_image_progress(
                image_done=image_done,
                image_total=image_total,
                eager_count=eager_summaries,
                deferred_count=deferred_summaries,
                embeddings_complete=image_embeddings_complete,
                started_at=extraction_started_at,
                no_color=no_color,
            )

    if (not incremental) or ledger_sources_to_replace or tax_rows:
        replace_tax_rows_for_sources(
            db_path,
            silo=silo_slug,
            sources=ledger_sources_to_replace,
            new_rows=tax_rows,
            replace_all_in_silo=(not incremental),
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    language_stats = None
    if all_chunks:
        unique_by_ext: dict[str, set[str]] = {}
        for _id, _doc, meta in all_chunks:
            src = (meta or {}).get("source") or ""
            if not src:
                continue
            ext = Path(src).suffix.lower()
            if ext not in ADD_CODE_EXTENSIONS:
                continue
            unique_by_ext.setdefault(ext, set()).add(src)
        if unique_by_ext:
            language_stats = {
                "by_ext": {ext: len(paths) for ext, paths in unique_by_ext.items()},
                "sample_paths": {ext: list(paths)[:3] for ext, paths in unique_by_ext.items()},
            }
    chunks_count = len(all_chunks)
    total_files = files_indexed
    if incremental:
        try:
            result = collection.get(where={"silo": silo_slug}, include=["metadatas"])
            chunks_count = len(result.get("metadatas") or [])
        except Exception:
            pass
        try:
            manifest = _read_file_manifest(db_path)
            silo_manifest = (manifest.get("silos") or {}).get(silo_slug, {})
            manifest_files = (silo_manifest.get("files") or {}) if isinstance(silo_manifest, dict) else {}
            if isinstance(manifest_files, dict):
                total_files = len(manifest_files)
        except Exception:
            pass
    update_silo(db_path, silo_slug, str(path), total_files, chunks_count, now_iso, display_name=display_name, language_stats=language_stats)
    set_last_failures(db_path, failures)

    # Summary: trust + usability (per-file FAIL still printed above)
    if not quiet:
        if failures:
            print(warn_style(no_color, f"Indexed {files_indexed} files ({len(failures)} failed). pal log or llmli log --last to see failures."), file=sys.stderr)
        else:
            if files_indexed == 0:
                print(success_style(no_color, "All files up to date."))
            else:
                print(success_style(no_color, f"Indexed {files_indexed} files."))

    # Optional status file for tooling (e.g., pal pull). Avoid writing to silos.
    status_path = os.environ.get("LLMLIBRARIAN_STATUS_FILE")
    if status_path:
        try:
            status = {
                "path": str(path),
                "slug": silo_slug,
                "files_indexed": files_indexed,
                "failures": len(failures),
                "chunks_count": chunks_count,
                "updated": now_iso,
            }
            with open(status_path, "w", encoding="utf-8") as f:
                json.dump(status, f)
        except Exception:
            pass
    return (files_indexed, len(failures))


def _load_limits_config() -> tuple[int, int, int, int, int]:
    """Return (max_file_bytes, max_depth, max_archive_bytes, max_files_per_zip, max_extracted_per_zip)."""
    limits_cfg = {}
    try:
        config = load_config()
        limits_cfg = config.get("limits") or {}
    except Exception:
        pass
    max_file_bytes = int((limits_cfg.get("max_file_size_mb") or 20) * 1024 * 1024)
    max_depth = int(limits_cfg.get("max_depth") or DEFAULT_MAX_DEPTH)
    max_archive_bytes = int((limits_cfg.get("max_archive_size_mb") or 100) * 1024 * 1024)
    max_files_per_zip = int(limits_cfg.get("max_files_per_zip") or DEFAULT_MAX_FILES_PER_ZIP)
    max_extracted_per_zip = int(limits_cfg.get("max_extracted_bytes_per_zip") or DEFAULT_MAX_EXTRACTED_BYTES_PER_ZIP)
    return max_file_bytes, max_depth, max_archive_bytes, max_files_per_zip, max_extracted_per_zip


def update_silo_counts(db_path: str | Path, silo_slug: str, display_name: str | None = None) -> None:
    """Recompute silo file/chunk counts and update llmli registry."""
    from state import update_silo, list_silos

    db_path = db_path or DB_PATH
    manifest = _read_file_manifest(db_path)
    silo_manifest = (manifest.get("silos") or {}).get(silo_slug, {})
    manifest_files = (silo_manifest.get("files") or {}) if isinstance(silo_manifest, dict) else {}
    total_files = len(manifest_files) if isinstance(manifest_files, dict) else 0
    silo_path = (silo_manifest.get("path") if isinstance(silo_manifest, dict) else "") or ""

    existing_display = None
    if display_name is None:
        for entry in list_silos(db_path):
            if entry.get("slug") == silo_slug:
                existing_display = entry.get("display_name")
                if not silo_path:
                    silo_path = entry.get("path") or ""
                break
    name = display_name or existing_display or silo_slug

    ef = get_embedding_function()
    client = chromadb.PersistentClient(path=str(db_path), settings=Settings(anonymized_telemetry=False))
    collection = client.get_or_create_collection(name=LLMLI_COLLECTION, embedding_function=ef)
    chunks_count = 0
    try:
        result = collection.get(where={"silo": silo_slug}, include=["ids"])
        ids = result.get("ids") if isinstance(result, dict) else None
        if isinstance(ids, list):
            chunks_count = len(ids)
    except Exception:
        pass

    now_iso = datetime.now(timezone.utc).isoformat()
    update_silo(db_path, silo_slug, silo_path, total_files, chunks_count, now_iso, display_name=name)


def _detect_kind(path: Path) -> str:
    suf = path.suffix.lower()
    if suf == ".zip":
        return "zip"
    if suf == ".pdf":
        return "pdf"
    if suf == ".docx":
        return "docx"
    if suf == ".xlsx":
        return "xlsx"
    if suf == ".pptx":
        return "pptx"
    if suf in IMAGE_EXTENSIONS:
        return "image"
    return "code"


def remove_single_file(
    path: str | Path,
    db_path: str | Path | None = None,
    silo_slug: str = "__self__",
    update_counts: bool = True,
) -> tuple[str, str]:
    """Remove a single file from a silo (chunks, manifest, registry). Returns (status, path)."""
    db_path = db_path or DB_PATH
    try:
        Path(db_path).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    p = Path(path).resolve()
    path_str = str(p)

    manifest = _read_file_manifest(db_path)
    silo_manifest = (manifest.get("silos") or {}).get(silo_slug, {})
    manifest_files = (silo_manifest.get("files") or {}) if isinstance(silo_manifest, dict) else {}
    prev = manifest_files.get(path_str) if isinstance(manifest_files, dict) else None

    ef = get_embedding_function()
    client = chromadb.PersistentClient(path=str(db_path), settings=Settings(anonymized_telemetry=False))
    collection = client.get_or_create_collection(name=LLMLI_COLLECTION, embedding_function=ef)
    image_collection = _get_image_collection(client)
    _delete_source_from_collections(
        collection=collection,
        image_collection=image_collection,
        silo_slug=silo_slug,
        source_path=path_str,
    )
    if prev:
        _file_registry_remove_path(db_path, silo_slug, path_str, prev.get("hash"))

    def _update_manifest(manifest_data: dict) -> None:
        silos = manifest_data.setdefault("silos", {})
        silo_entry = silos.setdefault(silo_slug, {"path": "", "files": {}})
        if not silo_entry.get("path"):
            silo_entry["path"] = str(p.parent)
        files_map = silo_entry.setdefault("files", {})
        if isinstance(files_map, dict) and path_str in files_map:
            del files_map[path_str]

    _update_file_manifest(db_path, _update_manifest)
    replace_tax_rows_for_sources(
        db_path,
        silo=silo_slug,
        sources={path_str},
        new_rows=[],
    )
    if update_counts:
        update_silo_counts(db_path, silo_slug)
    return ("removed" if prev else "skipped", path_str)


def update_single_file(
    path: str | Path,
    db_path: str | Path | None = None,
    silo_slug: str = "__self__",
    allow_cloud: bool = False,
    follow_symlinks: bool = False,
    no_color: bool = False,
    update_counts: bool = True,
) -> tuple[str, str]:
    """
    Index or update a single file within a silo. Returns (status, path).
    status: updated|unchanged|removed|skipped|duplicate|error
    """
    db_path = db_path or DB_PATH
    try:
        Path(db_path).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    p = Path(path)
    if p.is_symlink() and not follow_symlinks:
        return ("skipped", str(p))
    try:
        p = p.resolve()
    except Exception:
        return ("error", str(p))
    path_str = str(p)
    if not p.exists() or not p.is_file():
        return remove_single_file(p, db_path=db_path, silo_slug=silo_slug, update_counts=update_counts)
    if not allow_cloud:
        cloud_kind = is_cloud_sync_path(p)
        if cloud_kind:
            return ("skipped", path_str)
    if not should_index(path_str, ADD_DEFAULT_INCLUDE, ADD_DEFAULT_EXCLUDE):
        return remove_single_file(p, db_path=db_path, silo_slug=silo_slug, update_counts=update_counts)
    if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff"}:
        ensure_vision_model_ready()
        ensure_image_embedding_adapter_ready()

    max_file_bytes, _max_depth, max_archive_bytes, max_files_per_zip, max_extracted_per_zip = _load_limits_config()
    try:
        stat = p.stat()
        mtime = stat.st_mtime
        size = stat.st_size
    except OSError:
        return ("error", path_str)

    kind = _detect_kind(p)
    if kind != "zip" and size > max_file_bytes:
        return remove_single_file(p, db_path=db_path, silo_slug=silo_slug, update_counts=update_counts)
    if kind == "zip" and size > max_archive_bytes:
        return remove_single_file(p, db_path=db_path, silo_slug=silo_slug, update_counts=update_counts)

    manifest = _read_file_manifest(db_path)
    silo_manifest = (manifest.get("silos") or {}).get(silo_slug, {})
    manifest_files = (silo_manifest.get("files") or {}) if isinstance(silo_manifest, dict) else {}
    prev = manifest_files.get(path_str) if isinstance(manifest_files, dict) else None

    file_hash = ""
    existing: list[dict[str, Any]] = []
    if kind != "zip":
        file_hash = get_file_hash(p)
        if prev and prev.get("mtime") == mtime and prev.get("size") == size:
            if not file_hash:
                return ("unchanged", path_str)
            existing_same = any(
                str(e.get("silo") or "") == silo_slug and str(e.get("path") or "") == path_str
                for e in _file_registry_get(db_path, file_hash)
            )
            if prev.get("hash") == file_hash and existing_same:
                return ("unchanged", path_str)
        if file_hash:
            existing = _file_registry_get(db_path, file_hash)
    else:
        if prev and prev.get("mtime") == mtime and prev.get("size") == size:
            return ("unchanged", path_str)

    ef = get_embedding_function()
    client = chromadb.PersistentClient(path=str(db_path), settings=Settings(anonymized_telemetry=False))
    collection = client.get_or_create_collection(name=LLMLI_COLLECTION, embedding_function=ef)
    image_collection = _get_image_collection(client)
    _delete_source_from_collections(
        collection=collection,
        image_collection=image_collection,
        silo_slug=silo_slug,
        source_path=path_str,
    )
    if prev:
        _file_registry_remove_path(db_path, silo_slug, path_str, prev.get("hash"))

    chunks: list[ChunkTuple] = []
    image_vectors: list[ImageVectorTuple] = []
    if kind == "zip":
        try:
            chunks = process_zip_to_chunks(
                p,
                ADD_DEFAULT_INCLUDE,
                ADD_DEFAULT_EXCLUDE,
                max_archive_bytes,
                max_file_bytes,
                max_files_per_zip,
                max_extracted_per_zip,
                db_path=db_path,
            )
        except Exception:
            chunks = []
    else:
        clone_from = next(
            (str(e.get("silo") or "") for e in existing if str(e.get("silo") or "") != silo_slug),
            "",
        )
        if clone_from and file_hash:
            chunks = _clone_chunks_from_existing_silo(
                collection=collection,
                from_silo=clone_from,
                source_path=path_str,
                target_silo=silo_slug,
            )
            image_vectors = _clone_image_vectors_from_existing_silo(
                collection=image_collection,
                from_silo=clone_from,
                source_path=path_str,
                target_silo=silo_slug,
            )
        if not chunks:
            try:
                chunks = process_one_file(p, kind, file_hash or None, follow_symlinks, p, db_path=db_path)
            except Exception:
                chunks = []

    if chunks:
        chunks = [
            (hashlib.sha256(f"{silo_slug}|{cid}".encode()).hexdigest()[:20], doc, {**meta, "silo": silo_slug})
            for cid, doc, meta in chunks
        ]
        batch_size = ADD_BATCH_SIZE
        try:
            batch_size = int(os.environ.get("LLMLIBRARIAN_ADD_BATCH_SIZE", batch_size))
        except (TypeError, ValueError):
            pass
        batch_size = max(1, min(batch_size, 2000))
        embedding_workers = 1
        try:
            embedding_workers = int(os.environ.get("LLMLIBRARIAN_EMBEDDING_WORKERS", "1"))
        except (TypeError, ValueError):
            embedding_workers = 1
        embedding_workers = max(1, min(embedding_workers, 32))
        _batch_add(
            collection,
            chunks,
            batch_size=batch_size,
            no_color=no_color,
            embedding_fn=ef,
            embedding_workers=embedding_workers,
        )
        if not image_vectors:
            vector_row = _image_vector_from_chunks(source_path=path_str, chunks=chunks)
            if vector_row is not None:
                vid, vpath, vdoc, vmeta = vector_row
                image_vectors = [(vid, vpath, vdoc, {**vmeta, "silo": silo_slug})]
        if image_vectors:
            _batch_add_image_vectors(
                image_collection,
                image_vectors,
                batch_size=1,
                no_color=no_color,
            )
        if file_hash:
            _file_registry_add(db_path, file_hash, silo_slug, path_str)
    tax_rows = extract_tax_rows_from_chunks(chunks) if chunks else []

    def _update_manifest(manifest_data: dict) -> None:
        silos = manifest_data.setdefault("silos", {})
        silo_entry = silos.setdefault(silo_slug, {"path": "", "files": {}})
        files_map = silo_entry.setdefault("files", {})
        if not isinstance(files_map, dict):
            files_map = {}
            silo_entry["files"] = files_map
        files_map[path_str] = {"mtime": mtime, "size": size, "hash": file_hash if kind != "zip" else ""}

    _update_file_manifest(db_path, _update_manifest)
    replace_tax_rows_for_sources(
        db_path,
        silo=silo_slug,
        sources={path_str},
        new_rows=tax_rows,
    )
    if update_counts:
        update_silo_counts(db_path, silo_slug)
    return ("updated", path_str)
