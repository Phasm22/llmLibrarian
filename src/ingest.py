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
import os
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
# Default include/exclude for llmli add (code-style; auto-ignore .git, node_modules, etc.)
ADD_DEFAULT_INCLUDE = ["*.py", "*.ts", "*.tsx", "*.js", "*.go", "*.rs", "*.sh", "*.md", "*.txt", "*.yml", "*.yaml", "*.pdf", "*.docx"]
ADD_DEFAULT_EXCLUDE = [
    "node_modules/", ".venv/", "venv/", "env/", "__pycache__/", "vendor", "dist", "build", ".git",
    "llmLibrarianVenv/", "site-packages/", "Old Firefox Data", "Firefox", ".app/",
]

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
) -> list[ChunkTuple]:
    """Build chunk list (id, doc, meta) for text/code. No collection."""
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
        }
        if page is not None:
            meta["page"] = page
        out.append((cid, chunk, meta))
    return out


def _chunks_from_pdf(
    file_id: str,
    pdf_bytes: bytes,
    source_path: str,
    mtime: float,
) -> list[ChunkTuple]:
    """Build chunk list (id, doc, meta) for PDF by page. No collection."""
    pages = get_text_from_pdf_bytes(pdf_bytes)
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
        }
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


def process_one_file(path: Path, kind: str) -> list[ChunkTuple]:
    """
    Read file and return list of (id, doc, meta). Runs in worker thread.
    kind: 'code' | 'pdf' | 'docx'. Returns [] on read error.
    """
    path_str = str(path)
    try:
        stat = path.stat()
        mtime = stat.st_mtime
    except OSError:
        return []
    file_id = path.name
    if kind == "pdf":
        try:
            data = path.read_bytes()
        except OSError:
            return []
        return _chunks_from_pdf(file_id, data, path_str, mtime)
    if kind == "docx":
        try:
            data = path.read_bytes()
        except OSError:
            return []
        text = get_text_from_docx_bytes(data)
        return _chunks_from_content(file_id, text, path_str, mtime, prefix="")
    # code / text
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _chunks_from_content(file_id, text, path_str, mtime)


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
                if item.suffix.lower() == ".zip":
                    out.append((item, "zip"))
                elif item.suffix.lower() == ".pdf":
                    out.append((item, "pdf"))
                elif item.suffix.lower() == ".docx":
                    out.append((item, "docx"))
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
    Extract PDF/DOCX from ZIP (with limits) and return list of (id, doc, meta).
    Skips encrypted and over-size. Returns [] on error.
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
                if not info.filename.lower().endswith((".pdf", ".docx")):
                    continue
                with z.open(info.filename) as f:
                    data = f.read()
                extracted_bytes += len(data)
                if len(data) > max_file_bytes:
                    continue
                source_label = f"{zip_path} > {info.filename}"
                file_id = f"{zip_path.name}/{Path(info.filename).name}"
                mtime = 0.0
                if info.filename.lower().endswith(".pdf"):
                    out.extend(_chunks_from_pdf(file_id, data, source_label, mtime))
                else:
                    text = get_text_from_docx_bytes(data)
                    out.extend(_chunks_from_content(file_id, text, source_label, mtime, prefix=""))
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

    all_chunks: list[ChunkTuple] = []
    files_indexed = 0
    failures: list[dict[str, str]] = []

    workers = max(1, min(MAX_WORKERS, (os.cpu_count() or 8)))
    try:
        workers = int(os.environ.get("LLMLIBRARIAN_MAX_WORKERS", workers))
    except (TypeError, ValueError):
        pass
    workers = max(1, min(workers, 32))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_item = {executor.submit(process_one_file, p, k): (p, k) for p, k in regular}
        for future in as_completed(future_to_item):
            p, kind = future_to_item[future]
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
            elif kind == "pdf":
                print(warn_style(no_color, f"  ⚠️ {p.name}: no extractable text"), file=sys.stderr)

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

    ef = get_embedding_function()
    client = chromadb.PersistentClient(path=str(db_path), settings=Settings(anonymized_telemetry=False))
    collection = client.get_or_create_collection(name=LLMLI_COLLECTION, embedding_function=ef)

    try:
        collection.delete(where={"silo": silo_slug})
    except Exception:
        pass  # no chunks for this silo yet

    if all_chunks:
        batch_size = ADD_BATCH_SIZE
        try:
            batch_size = int(os.environ.get("LLMLIBRARIAN_ADD_BATCH_SIZE", batch_size))
        except (TypeError, ValueError):
            pass
        batch_size = max(1, min(batch_size, 2000))
        _batch_add(collection, all_chunks, batch_size=batch_size, no_color=no_color)

    now_iso = datetime.now(timezone.utc).isoformat()
    update_silo(db_path, silo_slug, str(path), files_indexed, len(all_chunks), now_iso, display_name=display_name)
    set_last_failures(db_path, failures)

    if failures:
        print(f"Indexed {files_indexed} files ({len(failures)} failed). Run: llmli log --last", file=sys.stderr)
    return (files_indexed, len(failures))
