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
import os
import re
import sys
import tempfile
import traceback
import zipfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# Chunk tuple: (id, document, metadata)
ChunkTuple = tuple[str, str, dict[str, Any]]

from constants import (
    DB_PATH,
    LLMLI_COLLECTION,
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
from load_config import load_config, get_archetype
from style import bold, dim, label_style, success_style, warn_style

try:
    import fcntl  # type: ignore[import-not-found]
except Exception:
    fcntl = None  # type: ignore[assignment]

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
]
ADD_DEFAULT_EXCLUDE = [
    "node_modules/", ".venv/", "venv/", "env/", "__pycache__/", "vendor", "dist", "build", ".git",
    "llmLibrarianVenv/", "site-packages/", "Old Firefox Data", "Firefox", ".app/",
]

# Code-file extensions for language_stats (CODE_LANGUAGE pipeline). Excludes .md, .txt, .pdf, .docx.
ADD_CODE_EXTENSIONS = frozenset(
    {".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".sh", ".bash", ".zsh", ".php", ".kt", ".sql"}
)
# Text extensions we decode as UTF-8 when found inside ZIPs (no binary handling).
ZIP_TEXT_EXTENSIONS = frozenset(
    {".txt", ".md", ".json", ".csv", ".xml", ".html", ".rst", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sql", ".py", ".js", ".ts", ".tsx", ".sh", ".go", ".rs"}
)


def get_capabilities_text() -> str:
    """Deterministic report: supported extensions and document extractors. Source of truth for llmli capabilities and ask routing."""
    exts = sorted({p.replace("*", "") for p in ADD_DEFAULT_INCLUDE if p.startswith("*.")})
    lines = ["Supported file extensions (indexed by llmli add):", "  " + ", ".join(exts), ""]
    lines.append("Document extractors:")
    lines.append("  PDF: yes (pymupdf)")
    lines.append("  DOCX: yes (python-docx)")
    lines.append("  ZIP: yes (PDF/DOCX/XLSX/PPTX + text inside)")
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
    # Transcript / academic record
    if re.search(r"\btranscript\b|credit\s+hours?\b|Grade\s+[A-F]|GPA|course\s+history\b", s, re.IGNORECASE):
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
    event = {"ts": datetime.now(timezone.utc).isoformat(), "level": level, "message": message}
    if fields:
        event.update(fields)
    try:
        print(json.dumps(event, ensure_ascii=False), file=sys.stderr)
    except Exception:
        pass


def _file_registry_path(db_path: str | Path) -> Path:
    p = Path(db_path).resolve()
    if p.is_dir():
        return p / "llmli_file_registry.json"
    return p.parent / "llmli_file_registry.json"


def _registry_lock_path(registry_path: Path) -> Path:
    if registry_path.suffix:
        return registry_path.with_suffix(registry_path.suffix + ".lock")
    return registry_path.with_name(registry_path.name + ".lock")


@contextmanager
def _registry_lock(registry_path: Path) -> Iterator[None]:
    if fcntl is None:
        yield
        return
    lock_path = _registry_lock_path(registry_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
            tmp_path = Path(f.name)
        os.replace(tmp_path, path)
    finally:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _file_manifest_path(db_path: str | Path) -> Path:
    p = Path(db_path).resolve()
    if p.is_dir():
        return p / "llmli_file_manifest.json"
    return p.parent / "llmli_file_manifest.json"


def _read_file_manifest(db_path: str | Path) -> dict:
    path = _file_manifest_path(db_path)
    if not path.exists():
        return {"silos": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict) or "silos" not in data:
                return {"silos": {}}
            return data
    except Exception as e:
        print(f"[llmli] file manifest read failed: {path}: {e}; using empty.", file=sys.stderr)
        return {"silos": {}}


def _write_file_manifest(db_path: str | Path, data: dict) -> None:
    path = _file_manifest_path(db_path)
    try:
        _atomic_write_json(path, data)
    except Exception as e:
        print(f"[llmli] file manifest write failed: {path}: {e}", file=sys.stderr)
        raise


def _update_file_manifest(db_path: str | Path, update_fn: Any) -> None:
    path = _file_manifest_path(db_path)
    with _registry_lock(path):
        manifest = _read_file_manifest(db_path)
        update_fn(manifest)
        _write_file_manifest(db_path, manifest)


def _read_file_registry(db_path: str | Path) -> dict:
    path = _file_registry_path(db_path)
    if not path.exists():
        return {"by_hash": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data.get("by_hash"), dict) else {"by_hash": {}}
    except Exception as e:
        print(f"[llmli] file registry read failed: {path}: {e}; using empty.", file=sys.stderr)
        return {"by_hash": {}}


def _write_file_registry(db_path: str | Path, data: dict) -> None:
    path = _file_registry_path(db_path)
    try:
        _atomic_write_json(path, data)
    except Exception as e:
        print(f"[llmli] file registry write failed: {path}: {e}", file=sys.stderr)
        raise


def _update_file_registry(db_path: str | Path, update_fn: Any) -> None:
    path = _file_registry_path(db_path)
    with _registry_lock(path):
        reg = _read_file_registry(db_path)
        update_fn(reg)
        _write_file_registry(db_path, reg)


def _file_registry_get(db_path: str | Path, file_hash: str) -> list[dict]:
    """Return list of {silo, path} that have indexed this hash."""
    reg = _read_file_registry(db_path)
    return list(reg.get("by_hash", {}).get(file_hash, []))


def _file_registry_add(db_path: str | Path, file_hash: str, silo: str, path_str: str) -> None:
    def _apply(reg: dict) -> None:
        by_hash = reg.setdefault("by_hash", {})
        entries = by_hash.setdefault(file_hash, [])
        if not any(e.get("silo") == silo and e.get("path") == path_str for e in entries):
            entries.append({"silo": silo, "path": path_str})

    _update_file_registry(db_path, _apply)


def _file_registry_remove_path(db_path: str | Path, silo: str, path_str: str, file_hash: str | None = None) -> None:
    def _apply(reg: dict) -> None:
        by_hash = reg.get("by_hash", {})
        if file_hash and file_hash in by_hash:
            entries = by_hash.get(file_hash, [])
            entries = [e for e in entries if not (e.get("silo") == silo and e.get("path") == path_str)]
            if entries:
                by_hash[file_hash] = entries
            else:
                del by_hash[file_hash]
            return
        # Fallback: scan all hashes for the path (slower but safe).
        for h, entries in list(by_hash.items()):
            new_entries = [e for e in entries if not (e.get("silo") == silo and e.get("path") == path_str)]
            if not new_entries:
                del by_hash[h]
            else:
                by_hash[h] = new_entries

    _update_file_registry(db_path, _apply)

def _file_registry_remove_silo(db_path: str | Path, silo: str) -> None:
    def _apply(reg: dict) -> None:
        by_hash = reg.get("by_hash", {})
        for h, entries in list(by_hash.items()):
            new_entries = [e for e in entries if e.get("silo") != silo]
            if not new_entries:
                del by_hash[h]
            else:
                by_hash[h] = new_entries

    _update_file_registry(db_path, _apply)


def get_paths_by_silo(db_path: str | Path) -> dict[str, set[str]]:
    """Build catalog: silo -> set of indexed paths. Derived from file registry (by_hash -> [{silo, path}])."""
    reg = _read_file_registry(db_path)
    by_silo: dict[str, set[str]] = {}
    for entries in (reg.get("by_hash") or {}).values():
        for e in entries:
            s = e.get("silo")
            p = e.get("path")
            if s is not None and p:
                by_silo.setdefault(s, set()).add(p)
    return by_silo


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
    PROCESSORS,
    DEFAULT_PROCESSOR,
    DocumentExtractionError,
    PDFExtractionError,
)

_pdf_proc = PDFProcessor()
_docx_proc = DOCXProcessor()
_xlsx_proc = XLSXProcessor()
_pptx_proc = PPTXProcessor()


def get_text_from_pdf_bytes(pdf_bytes: bytes) -> list[tuple[str, int]]:
    """Returns list of (page_text, page_number) for per-page chunks. Raises PDFExtractionError on failure."""
    result = _pdf_proc.extract(pdf_bytes, "")
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


# --- Indexing ---
def _chunk_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _stable_chunk_id(source_path: str, mtime: float, chunk_index: int) -> str:
    key = f"{source_path}|{mtime}|{chunk_index}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


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
        out.append((cid, chunk, meta))
    return out


def _chunks_from_pdf(
    file_id: str,
    pdf_bytes: bytes,
    source_path: str,
    mtime: float,
    file_hash: str | None = None,
) -> list[ChunkTuple]:
    """Build chunk list (id, doc, meta) for PDF by page. doc_type from first page content overrides path when not 'other'."""
    pages = get_text_from_pdf_bytes(pdf_bytes)
    first_page_text = (pages[0][0] if pages else "")[:CONTENT_SAMPLE_LEN]
    content_type = _doc_type_from_content(first_page_text)
    doc_type = content_type if content_type != "other" else _doc_type_from_path(source_path)
    out: list[ChunkTuple] = []
    for page_text, page_num in pages:
        if not page_text.strip():
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
        out.append((chunk_id, page_text, meta))
    return out


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
    # PDF: returns list of (page_text, page_num) — use _chunks_from_pdf
    if isinstance(result, list):
        try:
            return _chunks_from_pdf(file_id, data, path_str, mtime, file_hash=file_hash)
        except PDFExtractionError as e:
            _log_event("ERROR", "PDF extraction failed", path=path_str, error=str(e))
            return []
    # Extraction returned None — library not available; emit metadata-only chunk
    if result is None:
        return _chunk_metadata_only(
            file_id, path_str, mtime,
            processor.format_label, processor.install_hint,
            file_hash=file_hash,
        )
    # String result — use _chunks_from_content
    return _chunks_from_content(file_id, result, path_str, mtime, prefix="", file_hash=file_hash)


def collect_files(
    root: Path,
    include: list[str],
    exclude: list[str],
    max_depth: int,
    max_file_bytes: int,
    current_depth: int = 0,
    follow_symlinks: bool = False,
) -> list[tuple[Path, str]]:
    """
    Walk tree and return list of (path, kind) for files to index.
    kind: 'code' | 'pdf' | 'docx' | 'zip'. Does not read file contents.
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
                    )
                )
            else:
                if not should_index(path_str, include, exclude):
                    continue
                try:
                    if item.stat().st_size > max_file_bytes:
                        continue
                except OSError:
                    continue
                suf = item.suffix.lower()
                if suf == ".zip":
                    out.append((item, "zip"))
                elif suf == ".pdf":
                    out.append((item, "pdf"))
                elif suf == ".docx":
                    out.append((item, "docx"))
                elif suf == ".xlsx":
                    out.append((item, "xlsx"))
                elif suf == ".pptx":
                    out.append((item, "pptx"))
                else:
                    out.append((item, "code"))
    except PermissionError:
        pass
    except OSError:
        pass
    return out


def process_zip_to_chunks(
    zip_path: Path,
    include: list[str],
    exclude: list[str],
    max_archive_bytes: int,
    max_file_bytes: int,
    max_files_per_zip: int,
    max_extracted_per_zip: int,
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
                if suf == ".pdf":
                    try:
                        out.extend(_tag_zip_meta(_chunks_from_pdf(file_id, data, source_label, mtime), zip_path))
                    except PDFExtractionError as e:
                        _log_event(
                            "ERROR",
                            "PDF extraction failed in ZIP",
                            path=str(zip_path),
                            entry=info.filename,
                            error=str(e),
                        )
                elif suf == ".docx":
                    text = get_text_from_docx_bytes(data)
                    out.extend(_tag_zip_meta(_chunks_from_content(file_id, text, source_label, mtime, prefix=""), zip_path))
                elif suf == ".xlsx":
                    text, ok = get_text_from_xlsx_bytes(data)
                    if ok and text:
                        out.extend(_tag_zip_meta(_chunks_from_content(file_id, text, source_label, mtime, prefix=""), zip_path))
                    else:
                        out.extend(_tag_zip_meta(_chunk_metadata_only(file_id, source_label, mtime, "Spreadsheet", "openpyxl"), zip_path))
                elif suf == ".pptx":
                    text, ok = get_text_from_pptx_bytes(data)
                    if ok and text:
                        out.extend(_tag_zip_meta(_chunks_from_content(file_id, text, source_label, mtime, prefix=""), zip_path))
                    else:
                        out.extend(_tag_zip_meta(_chunk_metadata_only(file_id, source_label, mtime, "Presentation", "python-pptx"), zip_path))
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
    collection = client.get_or_create_collection(
        name=collection_name,
        embedding_function=ef,
    )

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
    # 2. Split: regular files (parallel) vs zips (main thread, limits)
    regular = [(path, kind) for path, kind in file_list if kind != "zip"]
    zips = [path for path, kind in file_list if kind == "zip"]

    all_chunks: list[ChunkTuple] = []
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
                files_indexed += 1
                print(success_style(no_color, f"  ✅ {path.name} ({len(chunks)} chunks)"))
            elif kind == "pdf":
                print(warn_style(no_color, f"  ⚠️ {path.name}: no extractable text (encrypted, image-only, or empty)"))

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
) -> tuple[int, int]:
    """
    Index a single folder into the unified collection (llmli). Silo name = basename(path).
    Returns (files_indexed, failed_count). Failures saved for llmli log --last.
    Refuses cloud-sync roots (OneDrive, iCloud, Dropbox, etc.) unless allow_cloud=True.

    If interrupted (e.g. Ctrl+C): Chroma may have 0 or partial chunks for this silo;
    the registry is only updated on success. Re-run add for the same path to get a consistent state.
    """
    from state import update_silo, set_last_failures, slugify

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
    display_name = path.name
    silo_slug = slugify(display_name)
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

    file_list = collect_files(
        path,
        ADD_DEFAULT_INCLUDE,
        ADD_DEFAULT_EXCLUDE,
        max_depth,
        max_file_bytes,
        follow_symlinks=follow_symlinks,
    )
    regular = [(p, k) for p, k in file_list if k != "zip"]
    zips = [p for p, k in file_list if k == "zip"]

    workers = max(1, min(MAX_WORKERS, (os.cpu_count() or 8)))
    try:
        workers = int(os.environ.get("LLMLIBRARIAN_MAX_WORKERS", workers))
    except (TypeError, ValueError):
        pass
    workers = max(1, min(workers, 32))

    ef = get_embedding_function()
    client = chromadb.PersistentClient(path=str(db_path), settings=Settings(anonymized_telemetry=False))
    collection = client.get_or_create_collection(name=LLMLI_COLLECTION, embedding_function=ef)

    if not incremental:
        try:
            collection.delete(where={"silo": silo_slug})
        except Exception:
            pass  # no chunks for this silo yet
        _file_registry_remove_silo(db_path, silo_slug)

    # Pre-pass: resolve paths, hash, skip duplicates (same file already indexed in any silo)
    regular_with_hash: list[tuple[Path, str, str, Path | None]] = []
    manifest = _read_file_manifest(db_path) if incremental else {"silos": {}}
    silo_manifest = (manifest.get("silos") or {}).get(silo_slug, {})
    manifest_files = (silo_manifest.get("files") or {}) if isinstance(silo_manifest, dict) else {}
    current_paths: set[str] = set()
    if incremental:
        for zp in zips:
            try:
                current_paths.add(str(zp.resolve()))
            except OSError:
                current_paths.add(str(zp))
    skipped = 0
    for p, k in regular:
        try:
            p_res = p.resolve()
            if not p_res.is_file():
                continue
            current_paths.add(str(p_res))
            stat = p_res.stat()
            mtime = stat.st_mtime
            size = stat.st_size
            if incremental:
                prev = manifest_files.get(str(p_res)) if isinstance(manifest_files, dict) else None
                if prev and prev.get("mtime") == mtime and prev.get("size") == size:
                    continue
            h = get_file_hash(p_res)
            if not h:
                regular_with_hash.append((p, k, "", p_res))
                continue
            existing = _file_registry_get(db_path, h)
            if existing:
                other = existing[0]
                if other.get("silo") != silo_slug:
                    print(dim(no_color, f"  SKIPPING: {p_res.name} already indexed in [silo: {other.get('silo', '?')}]"))
                    skipped += 1
                    continue
            regular_with_hash.append((p, k, h, p_res))
        except OSError:
            regular_with_hash.append((p, k, "", None))

    if incremental and isinstance(manifest_files, dict):
        for _p, _k, _h, p_res in regular_with_hash:
            if p_res is None:
                continue
            path_str = str(p_res)
            if path_str in manifest_files:
                try:
                    collection.delete(where={"$and": [{"silo": silo_slug}, {"source": path_str}]})
                except Exception as e:
                    _log_event("WARN", "Failed to delete updated file chunks", path=path_str, error=str(e))
                prev = manifest_files.get(path_str) or {}
                _file_registry_remove_path(db_path, silo_slug, path_str, prev.get("hash"))

    all_chunks = []
    files_indexed = 0
    failures = []
    to_register: list[tuple[str, str]] = []  # (file_hash, path_str) for main-thread registry update

    if incremental and isinstance(manifest_files, dict):
        removed = [path_str for path_str in manifest_files.keys() if path_str not in current_paths]
        for path_str in removed:
            try:
                collection.delete(where={"$and": [{"silo": silo_slug}, {"source": path_str}]})
            except Exception as e:
                _log_event("WARN", "Failed to delete removed file chunks", path=path_str, error=str(e))
            try:
                collection.delete(where={"$and": [{"silo": silo_slug}, {"zip_path": path_str}]})
            except Exception:
                pass
            prev = manifest_files.get(path_str) or {}
            _file_registry_remove_path(db_path, silo_slug, path_str, prev.get("hash"))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_item = {
            executor.submit(process_one_file, p, k, h, follow_symlinks, p_res): (p, k, h)
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
                for _id, doc, meta in chunks:
                    meta["silo"] = silo_slug
                all_chunks.extend(chunks)
                files_indexed += 1
                if fhash:
                    to_register.append((fhash, str(p)))
            elif kind == "pdf":
                print(warn_style(no_color, f"  ⚠️ {p.name}: no extractable text"), file=sys.stderr)

    for fhash, path_str in to_register:
        _file_registry_add(db_path, fhash, silo_slug, path_str)

    for zip_path in zips:
        if zip_path.stat().st_size > max_archive_bytes:
            continue
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
            for _id, doc, meta in chunks:
                meta["silo"] = silo_slug
            all_chunks.extend(chunks)
            files_indexed += 1

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
        try:
            embedding_workers = int(os.environ.get("LLMLIBRARIAN_EMBEDDING_WORKERS", "1"))
        except (TypeError, ValueError):
            embedding_workers = 1
        embedding_workers = max(1, min(embedding_workers, 32))
        total_batches = (len(all_chunks) + batch_size - 1) // batch_size
        if not _should_use_tqdm():
            print(dim(no_color, f"  Adding {len(all_chunks)} chunks in {total_batches} batches (batch_size={batch_size})..."))
        _batch_add(
            collection,
            all_chunks,
            batch_size=batch_size,
            no_color=no_color,
            embedding_fn=ef,
            embedding_workers=embedding_workers,
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
    if failures:
        print(warn_style(no_color, f"Indexed {files_indexed} files ({len(failures)} failed). pal log or llmli log --last to see failures."), file=sys.stderr)
    else:
        print(success_style(no_color, f"Indexed {files_indexed} files."))
    return (files_indexed, len(failures))
