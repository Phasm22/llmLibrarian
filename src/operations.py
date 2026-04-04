"""
Shared operation layer for remove, repair, inspect, and list-silos.

Both cli.py and mcp_server.py call these functions; neither duplicates the logic.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# op_remove_silo
# ---------------------------------------------------------------------------

def op_remove_silo(db_path: str, slug_or_name: str) -> dict:
    """
    Remove a silo from the registry and delete its chunks from ChromaDB.
    Accepts display name, slug, path, or slug prefix.

    Returns {"removed_slug": str | None, "cleaned_slug": str, "not_found": bool}
    """
    from state import remove_silo, slugify, resolve_silo_by_path, resolve_silo_prefix, remove_manifest_silo
    from constants import LLMLI_COLLECTION
    from ingest import _file_registry_remove_silo
    from chroma_client import get_client

    raw = slug_or_name
    path_slug = resolve_silo_by_path(db_path, raw) if Path(raw).exists() else None
    prefix_slug = resolve_silo_prefix(db_path, raw)
    removed_slug = remove_silo(db_path, path_slug or prefix_slug or raw)
    slug_to_clean = removed_slug if removed_slug is not None else slugify(raw)

    chroma_error: str | None = None
    try:
        coll = get_client(db_path).get_or_create_collection(name=LLMLI_COLLECTION)
        coll.delete(where={"silo": slug_to_clean})
    except Exception as e:
        chroma_error = str(e)

    _file_registry_remove_silo(db_path, slug_to_clean)
    remove_manifest_silo(db_path, slug_to_clean)

    return {
        "removed_slug": removed_slug,
        "cleaned_slug": slug_to_clean,
        "not_found": removed_slug is None,
        **({"chroma_warning": chroma_error} if chroma_error else {}),
    }


# ---------------------------------------------------------------------------
# op_repair_silo
# ---------------------------------------------------------------------------

def op_repair_silo(db_path: str, slug_or_name: str, verbose: bool = True) -> dict:
    """
    Hard-wipe and fully re-index a silo to fix ChromaDB index corruption.

    verbose=True prints progress lines to stdout (CLI mode).
    Returns {"status": "completed"|"error", "slug": str, "files_indexed": int,
             "failures": int, "path": str} or {"status": "error", "error": str}
    """
    from state import list_silos, resolve_silo_to_slug, resolve_silo_prefix, remove_manifest_silo
    from constants import LLMLI_COLLECTION
    from ingest import _file_registry_remove_silo, run_add
    from chroma_client import get_client

    raw = slug_or_name
    slug = resolve_silo_to_slug(db_path, raw) or resolve_silo_prefix(db_path, raw)
    if slug is None:
        return {"status": "error", "error": f"silo not found: {raw!r}"}

    silos = {s["slug"]: s for s in list_silos(db_path)}
    silo_info = silos.get(slug, {})
    source_path = silo_info.get("path", "")

    if not source_path or not Path(source_path).exists():
        return {
            "status": "error",
            "error": (
                f"silo '{slug}' has no resolvable path ('{source_path}'). "
                "Cannot re-index automatically. Remove and re-add manually."
            ),
        }

    if verbose:
        print(f"[repair] Wiping ChromaDB chunks for silo '{slug}'...")
    try:
        coll = get_client(db_path).get_or_create_collection(name=LLMLI_COLLECTION)
        coll.delete(where={"silo": slug})
    except Exception as e:
        msg = f"could not wipe Chroma chunks: {e}"
        if verbose:
            print(f"[repair] Warning: {msg}", file=sys.stderr)

    if verbose:
        print(f"[repair] Clearing file registry for silo '{slug}'...")
    _file_registry_remove_silo(db_path, slug)
    remove_manifest_silo(db_path, slug)

    if verbose:
        print(f"[repair] Re-indexing '{source_path}' (full, non-incremental)...")
    files_ok, n_failures = run_add(path=source_path, db_path=db_path, incremental=False)

    if verbose:
        print(f"[repair] Done: {files_ok} file(s) re-indexed, {n_failures} failure(s).")
        if n_failures:
            print("  Run `llmli log` for failure details.")

    return {
        "status": "completed",
        "slug": slug,
        "display_name": silo_info.get("display_name", slug),
        "path": source_path,
        "files_indexed": files_ok,
        "failures": n_failures,
    }


# ---------------------------------------------------------------------------
# op_inspect_silo
# ---------------------------------------------------------------------------

def op_inspect_silo(db_path: str, slug_or_name: str, top: int = 50) -> dict:
    """
    Return per-file chunk counts for a silo.

    Returns a dict with slug, display_name, path, total_chunks_registry,
    total_chunks_chroma, registry_match, total_files, files_shown, and files list.
    Returns {"error": str} on failure.
    """
    from state import list_silos, resolve_silo_to_slug
    from constants import LLMLI_COLLECTION
    from chroma_client import get_client

    slug = resolve_silo_to_slug(db_path, slug_or_name)
    if slug is None:
        return {"error": f"silo not found: {slug_or_name!r}"}

    all_silos = list_silos(db_path)
    info = next((s for s in all_silos if s.get("slug") == slug), None)
    display = (info or {}).get("display_name", slug)
    path = (info or {}).get("path", "")
    total_registry = (info or {}).get("chunks_count", 0)

    try:
        coll = get_client(db_path).get_or_create_collection(name=LLMLI_COLLECTION)
        result = coll.get(where={"silo": slug}, include=["metadatas"])
        metas = result.get("metadatas") or []
    except Exception as e:
        return {"error": f"ChromaDB error: {e}"}

    total_chroma = len(metas)

    by_source: dict[str, int] = {}
    source_to_hash: dict[str, str] = {}
    for m in metas:
        meta = m or {}
        src = meta.get("source") or "?"
        by_source[src] = by_source.get(src, 0) + 1
        if src not in source_to_hash and meta.get("file_hash"):
            source_to_hash[src] = meta["file_hash"]

    hash_to_sources: dict[str, list[str]] = {}
    for src, h in source_to_hash.items():
        if h:
            hash_to_sources.setdefault(h, []).append(src)

    sorted_files = sorted(by_source.items(), key=lambda x: -x[1])[: max(1, top)]
    files = [
        {
            "source": src,
            "chunk_count": count,
            "has_duplicate_content": bool(
                source_to_hash.get(src)
                and len(hash_to_sources.get(source_to_hash[src], [])) > 1
            ),
        }
        for src, count in sorted_files
    ]

    return {
        "slug": slug,
        "display_name": display,
        "path": path,
        "total_chunks_registry": total_registry,
        "total_chunks_chroma": total_chroma,
        "registry_match": total_registry == total_chroma,
        "total_files": len(by_source),
        "files_shown": len(files),
        "files": files,
    }


# ---------------------------------------------------------------------------
# op_list_silos
# ---------------------------------------------------------------------------

_CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp",
    ".cs", ".rb", ".sh", ".swift", ".kt", ".scala", ".r", ".lua", ".pl", ".php",
}
_DOC_TYPE_MAP = {
    ".pdf": "pdf", ".docx": "docx", ".doc": "docx",
    ".xlsx": "xlsx", ".xls": "xlsx", ".csv": "xlsx",
    ".pptx": "pptx", ".ppt": "pptx",
}


def _doc_type_breakdown(by_ext: dict) -> dict:
    breakdown: dict[str, int] = {}
    for ext, count in (by_ext or {}).items():
        ext_lower = ext.lower()
        if ext_lower in _DOC_TYPE_MAP:
            cat = _DOC_TYPE_MAP[ext_lower]
        elif ext_lower in _CODE_EXTS:
            cat = "code"
        else:
            cat = "other"
        breakdown[cat] = breakdown.get(cat, 0) + count
    return breakdown


def _inject_staleness(silo_entry: dict) -> None:
    """Mutate silo_entry in-place with staleness fields."""
    source_path = silo_entry.get("path", "")
    updated_iso = silo_entry.get("updated", "")

    if not source_path or not Path(source_path).exists():
        silo_entry["is_stale"] = None
        silo_entry["staleness_note"] = "source path not accessible"
        return

    try:
        last_indexed = datetime.fromisoformat(updated_iso).timestamp()
    except Exception:
        silo_entry["is_stale"] = None
        silo_entry["staleness_note"] = "cannot parse updated timestamp"
        return

    stale_count = 0
    newest_stale_mtime: float | None = None

    try:
        for dirpath, _dirs, filenames in os.walk(source_path):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                except OSError:
                    continue
                if mtime > last_indexed:
                    stale_count += 1
                    if newest_stale_mtime is None or mtime > newest_stale_mtime:
                        newest_stale_mtime = mtime
    except Exception as e:
        silo_entry["is_stale"] = None
        silo_entry["staleness_note"] = f"walk error: {e}"
        return

    silo_entry["is_stale"] = stale_count > 0
    silo_entry["stale_file_count"] = stale_count
    if newest_stale_mtime is not None:
        silo_entry["newest_source_mtime_iso"] = datetime.fromtimestamp(
            newest_stale_mtime, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        silo_entry["newest_source_mtime_iso"] = None


def op_list_silos(db_path: str, check_staleness: bool = False) -> dict:
    """
    List all indexed silos with metadata.

    check_staleness=True walks source directories to detect changed files.
    Returns {"db_path": str, "db_exists": bool, "silo_count": int, "silos": list}
    """
    from state import list_silos as _list_silos, get_query_health

    silos = _list_silos(db_path)

    health_entries = get_query_health(db_path)
    silo_errors: dict[str, str] = {}
    for entry in health_entries:
        slug = entry.get("silo") or ""
        t = entry.get("time") or ""
        if slug and (slug not in silo_errors or t > silo_errors[slug]):
            silo_errors[slug] = t

    for s in silos:
        by_ext = (s.get("language_stats") or {}).get("by_ext") or {}
        s["doc_type_breakdown"] = _doc_type_breakdown(by_ext)

        slug = s.get("slug") or ""
        if slug in silo_errors:
            s["has_index_errors"] = True
            s["last_index_error_time"] = silo_errors[slug]
        else:
            s["has_index_errors"] = False
            s["last_index_error_time"] = None

        if check_staleness:
            _inject_staleness(s)

    return {
        "db_path": db_path,
        "db_exists": Path(db_path).exists(),
        "silo_count": len(silos),
        "silos": silos,
    }
