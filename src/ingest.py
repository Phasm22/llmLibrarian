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
import zipfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Chunk tuple: (id, document, metadata)
ChunkTuple = tuple[str, str, dict[str, Any]]
# Embedding is the bottleneck (~1s per 100 chunks with ONNX). Larger batches = fewer add() calls.
ADD_BATCH_SIZE = 256
# Parallelism for file read+chunk only; embedding runs in main thread in batches.
MAX_WORKERS = 8

import chromadb
from chromadb.config import Settings

from embeddings import get_embedding_function
from load_config import load_config, get_archetype
from style import bold, dim, label_style, success_style, warn_style

# --- Default limits (overridden by config) ---
DEFAULT_MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB
DEFAULT_MAX_DEPTH = 10
DEFAULT_MAX_ARCHIVE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB
DEFAULT_MAX_FILES_PER_ZIP = 500
DEFAULT_MAX_EXTRACTED_BYTES_PER_ZIP = 50 * 1024 * 1024  # 50 MB
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 100
DB_PATH = "./my_brain_db"
# Single collection for llmli add/ask/ls
LLMLI_COLLECTION = "llmli"
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


def _file_registry_path(db_path: str | Path) -> Path:
    p = Path(db_path).resolve()
    if p.is_dir():
        return p / "llmli_file_registry.json"
    return p.parent / "llmli_file_registry.json"


def _read_file_registry(db_path: str | Path) -> dict:
    path = _file_registry_path(db_path)
    if not path.exists():
        return {"by_hash": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data.get("by_hash"), dict) else {"by_hash": {}}
    except Exception:
        return {"by_hash": {}}


def _write_file_registry(db_path: str | Path, data: dict) -> None:
    path = _file_registry_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _file_registry_get(db_path: str | Path, file_hash: str) -> list[dict]:
    """Return list of {silo, path} that have indexed this hash."""
    reg = _read_file_registry(db_path)
    return list(reg.get("by_hash", {}).get(file_hash, []))


def _file_registry_add(db_path: str | Path, file_hash: str, silo: str, path_str: str) -> None:
    reg = _read_file_registry(db_path)
    by_hash = reg.setdefault("by_hash", {})
    entries = by_hash.setdefault(file_hash, [])
    if not any(e.get("silo") == silo and e.get("path") == path_str for e in entries):
        entries.append({"silo": silo, "path": path_str})
    _write_file_registry(db_path, reg)


def _file_registry_remove_silo(db_path: str | Path, silo: str) -> None:
    reg = _read_file_registry(db_path)
    by_hash = reg.get("by_hash", {})
    for h, entries in list(by_hash.items()):
        new_entries = [e for e in entries if e.get("silo") != silo]
        if not new_entries:
            del by_hash[h]
        else:
            by_hash[h] = new_entries
    _write_file_registry(db_path, reg)


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


# --- Extraction (PDF/DOCX/code) ---
def get_text_from_pdf_bytes(pdf_bytes: bytes) -> list[tuple[str, int]]:
    """Returns list of (page_text, page_number) for per-page chunks. Includes form field values. Returns [] if encrypted or broken."""
    import fitz
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            out: list[tuple[str, int]] = []
            for page in doc:
                text = page.get_text()
                # Filled tax forms often store values in form fields; get_text() may miss them.
                try:
                    for w in page.widgets():
                        name = getattr(w, "field_name", None) or ""
                        val = getattr(w, "field_value", None)
                        if name and val is not None and str(val).strip():
                            text += f"\n{name}: {val}"
                except Exception:
                    pass
                out.append((text, page.number + 1))
            return out
    except (ValueError, RuntimeError, Exception):
        return []


def get_text_from_docx_bytes(docx_bytes: bytes) -> str:
    import docx
    try:
        doc = docx.Document(io.BytesIO(docx_bytes))
        return "\n".join(para.text for para in doc.paragraphs)
    except Exception as e:
        return f"Error reading DOCX: {e}"


def get_text_from_xlsx_bytes(data: bytes) -> tuple[str | None, bool]:
    """Extract text from Excel (sheet names + cell values). Returns (text, True) or (None, False) if openpyxl not available."""
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
        return (text.strip() or None, True)
    except ImportError:
        return (None, False)
    except Exception:
        return (None, False)


def get_text_from_pptx_bytes(data: bytes) -> tuple[str | None, bool]:
    """Extract text from PowerPoint (slide shapes). Returns (text, True) or (None, False) if python-pptx not available."""
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
        return (text.strip() or None, True)
    except ImportError:
        return (None, False)
    except Exception:
        return (None, False)


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


def get_text_from_code_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error reading file: {e}"


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


def _batch_add(
    collection: Any,
    chunks: list[ChunkTuple],
    batch_size: int = ADD_BATCH_SIZE,
    no_color: bool = False,
    log_line: Any = None,
) -> None:
    """Add chunks to collection in batches. Progress printed so you can see where it hangs (embedding)."""
    if not chunks:
        return
    total_batches = (len(chunks) + batch_size - 1) // batch_size
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        batch_num = i // batch_size + 1
        msg = f"  Adding batch {batch_num}/{total_batches} ({len(batch)} chunks)..."
        print(dim(no_color, msg))
        if log_line:
            log_line(msg.strip())
        ids_b = [c[0] for c in batch]
        docs_b = [c[1] for c in batch]
        metas_b = [c[2] for c in batch]
        collection.add(ids=ids_b, documents=docs_b, metadatas=metas_b)


def process_one_file(path: Path, kind: str, file_hash: str | None = None) -> list[ChunkTuple]:
    """
    Read file and return list of (id, doc, meta). Runs in worker thread.
    kind: 'code' | 'pdf' | 'docx' | 'xlsx' | 'pptx'. file_hash: precomputed for dedup/registry; path is normalized (resolved).
    xlsx/pptx: extract text if openpyxl/python-pptx available; else one metadata-only chunk (content_extracted=0).
    """
    path_resolved = path.resolve()
    path_str = str(path_resolved)
    try:
        stat = path_resolved.stat()
        mtime = stat.st_mtime
    except OSError:
        return []
    file_id = path_resolved.name
    if kind == "pdf":
        try:
            data = path_resolved.read_bytes()
        except OSError:
            return []
        return _chunks_from_pdf(file_id, data, path_str, mtime, file_hash=file_hash)
    if kind == "docx":
        try:
            data = path_resolved.read_bytes()
        except OSError:
            return []
        text = get_text_from_docx_bytes(data)
        return _chunks_from_content(file_id, text, path_str, mtime, prefix="", file_hash=file_hash)
    if kind == "xlsx":
        try:
            data = path_resolved.read_bytes()
        except OSError:
            return []
        text, ok = get_text_from_xlsx_bytes(data)
        if ok and text:
            return _chunks_from_content(file_id, text, path_str, mtime, prefix="", file_hash=file_hash)
        return _chunk_metadata_only(file_id, path_str, mtime, "Spreadsheet", "openpyxl", file_hash=file_hash)
    if kind == "pptx":
        try:
            data = path_resolved.read_bytes()
        except OSError:
            return []
        text, ok = get_text_from_pptx_bytes(data)
        if ok and text:
            return _chunks_from_content(file_id, text, path_str, mtime, prefix="", file_hash=file_hash)
        return _chunk_metadata_only(file_id, path_str, mtime, "Presentation", "python-pptx", file_hash=file_hash)
    # code / text
    try:
        text = path_resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _chunks_from_content(file_id, text, path_str, mtime, file_hash=file_hash)


def collect_files(
    root: Path,
    include: list[str],
    exclude: list[str],
    max_depth: int,
    max_file_bytes: int,
    current_depth: int = 0,
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
            path_str = str(item)
            if item.is_dir():
                if not should_descend_into_dir(path_str, exclude):
                    continue
                out.extend(
                    collect_files(item, include, exclude, max_depth, max_file_bytes, current_depth + 1)
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
                    out.extend(_chunks_from_pdf(file_id, data, source_label, mtime))
                elif suf == ".docx":
                    text = get_text_from_docx_bytes(data)
                    out.extend(_chunks_from_content(file_id, text, source_label, mtime, prefix=""))
                elif suf == ".xlsx":
                    text, ok = get_text_from_xlsx_bytes(data)
                    if ok and text:
                        out.extend(_chunks_from_content(file_id, text, source_label, mtime, prefix=""))
                    else:
                        out.extend(_chunk_metadata_only(file_id, source_label, mtime, "Spreadsheet", "openpyxl"))
                elif suf == ".pptx":
                    text, ok = get_text_from_pptx_bytes(data)
                    if ok and text:
                        out.extend(_chunks_from_content(file_id, text, source_label, mtime, prefix=""))
                    else:
                        out.extend(_chunk_metadata_only(file_id, source_label, mtime, "Presentation", "python-pptx"))
                elif suf in ZIP_TEXT_EXTENSIONS:
                    try:
                        text = data.decode("utf-8", errors="replace")
                        if text.strip():
                            out.extend(_chunks_from_content(file_id, text, source_label, mtime, prefix=""))
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
        if not p.exists():
            print(warn_style(no_color, f"  ⚠️ Folder does not exist: {folder}"))
            continue
        if not p.is_dir():
            print(warn_style(no_color, f"  ⚠️ Not a directory: {folder}"))
            continue
        print(dim(no_color, f"  📁 {folder}"))
        file_list.extend(collect_files(p, include, exclude, max_depth, max_file_bytes))

    log(f"Collected {len(file_list)} files")
    # 2. Split: regular files (parallel) vs zips (main thread, limits)
    regular = [(path, kind) for path, kind in file_list if kind != "zip"]
    zips = [path for path, kind in file_list if kind == "zip"]

    all_chunks: list[ChunkTuple] = []
    files_indexed = 0

    # 3. Process regular files in parallel
    log("Processing files (parallel)...")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_item = {
            executor.submit(process_one_file, path, kind): (path, kind)
            for path, kind in regular
        }
        for future in as_completed(future_to_item):
            path, kind = future_to_item[future]
            try:
                chunks = future.result()
            except Exception as e:
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
        log(f"Adding {len(all_chunks)} chunks to collection (embedding, batch_size={batch_size})...")
        _batch_add(collection, all_chunks, batch_size=batch_size, no_color=no_color, log_line=log_line)

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
    path = Path(path).resolve()
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

    file_list = collect_files(path, ADD_DEFAULT_INCLUDE, ADD_DEFAULT_EXCLUDE, max_depth, max_file_bytes)
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

    try:
        collection.delete(where={"silo": silo_slug})
    except Exception:
        pass  # no chunks for this silo yet
    _file_registry_remove_silo(db_path, silo_slug)

    # Pre-pass: resolve paths, hash, skip duplicates (same file already indexed in any silo)
    regular_with_hash: list[tuple[Path, str, str]] = []
    skipped = 0
    for p, k in regular:
        try:
            p_res = p.resolve()
            if not p_res.is_file():
                continue
            h = get_file_hash(p_res)
            if not h:
                regular_with_hash.append((p_res, k, ""))
                continue
            existing = _file_registry_get(db_path, h)
            if existing:
                other = existing[0]
                print(dim(no_color, f"  SKIPPING: {p_res.name} already indexed in [silo: {other.get('silo', '?')}]"))
                skipped += 1
                continue
            regular_with_hash.append((p_res, k, h))
        except OSError:
            regular_with_hash.append((p.resolve(), k, ""))

    all_chunks = []
    files_indexed = 0
    failures = []
    to_register: list[tuple[str, str]] = []  # (file_hash, path_str) for main-thread registry update

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_item = {executor.submit(process_one_file, p, k, h): (p, k, h) for p, k, h in regular_with_hash}
        for future in as_completed(future_to_item):
            p, kind, fhash = future_to_item[future]
            try:
                chunks = future.result()
            except Exception as e:
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
        try:
            chunks = process_zip_to_chunks(
                zip_path, ADD_DEFAULT_INCLUDE, ADD_DEFAULT_EXCLUDE,
                max_archive_bytes, max_file_bytes, max_files_per_zip, max_extracted_per_zip,
            )
        except Exception as e:
            failures.append({"path": str(zip_path), "error": str(e)})
            print(f"[llmli] FAIL {zip_path}: {e}", file=sys.stderr)
            continue
        if chunks:
            for _id, doc, meta in chunks:
                meta["silo"] = silo_slug
            all_chunks.extend(chunks)
            files_indexed += 1

    if all_chunks:
        batch_size = ADD_BATCH_SIZE
        try:
            batch_size = int(os.environ.get("LLMLIBRARIAN_ADD_BATCH_SIZE", batch_size))
        except (TypeError, ValueError):
            pass
        batch_size = max(1, min(batch_size, 2000))
        total_batches = (len(all_chunks) + batch_size - 1) // batch_size
        print(dim(no_color, f"  Adding {len(all_chunks)} chunks in {total_batches} batches (batch_size={batch_size})..."))
        _batch_add(collection, all_chunks, batch_size=batch_size, no_color=no_color)

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
    update_silo(db_path, silo_slug, str(path), files_indexed, len(all_chunks), now_iso, display_name=display_name, language_stats=language_stats)
    set_last_failures(db_path, failures)

    # Summary: trust + usability (per-file FAIL still printed above)
    if failures:
        print(warn_style(no_color, f"Indexed {files_indexed} files ({len(failures)} failed). pal log or llmli log --last to see failures."), file=sys.stderr)
    else:
        print(success_style(no_color, f"Indexed {files_indexed} files."))
    return (files_indexed, len(failures))
