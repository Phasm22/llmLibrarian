"""
Shared operation layer for remove, repair, inspect, and list-silos.

Both cli.py and mcp_server.py call these functions; neither duplicates the logic.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from doc_type_taxonomy import doc_type_bucket_for_extension

# Chroma HNSW `link_lists.bin` should stay modest for a single-user index.
# Concurrent writers (multiple processes on one PersistentClient path) can
# corrupt it to hundreds of GiB; see chroma_lock.py and mcp_server.py _chroma_lock.
_HNSW_BLOAT_BYTES = 1 << 30  # 1 GiB


def op_db_storage_summary(db_path: str) -> dict[str, Any]:
    """
    Disk usage under the Chroma persist directory without opening Chroma.

    Surfaces oversized `link_lists.bin` (HNSW) which indicates unsafe concurrent
    writes or index corruption.
    """
    root = Path(db_path).expanduser().resolve()
    if not root.is_dir():
        return {"error": f"not a directory: {root}"}

    link_lists: list[dict[str, int | str]] = []
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            fp = Path(dirpath) / fn
            try:
                sz = int(fp.stat().st_size)
            except OSError:
                continue
            total += sz
            if fn == "link_lists.bin":
                link_lists.append({"path": str(fp), "bytes": sz})

    bloated = any(int(x.get("bytes") or 0) > _HNSW_BLOAT_BYTES for x in link_lists)
    note: str | None = None
    if bloated:
        note = (
            "At least one link_lists.bin is unusually large — typical cause is multiple "
            "processes writing the same Chroma path at once (e.g. several `pal pull --watch`, "
            "plus MCP/llmli). Stop all watchers and extra MCP servers, then repair silos or "
            "rebuild the DB; see README / mcp_server Chroma lock comment."
        )

    try:
        free = int(shutil.disk_usage(root).free)
    except OSError:
        free = -1

    try:
        from chroma_lock import chroma_lock_snapshot

        lock = chroma_lock_snapshot(root)
    except Exception:
        lock = None

    return {
        "db_path": str(root),
        "db_total_bytes": total,
        "disk_free_bytes": free,
        "link_lists": link_lists,
        "chroma_hnsw_bloat": bloated,
        "chroma_hnsw_bloat_note": note,
        "chroma_lock": lock,
    }


def op_chroma_diagnostics(db_path: str) -> dict[str, Any]:
    """
    Read-only Chroma diagnostics used by the L2 repair ladder.

    Does not open a Chroma client. Safe to run even when the DB is unhealthy.
    """
    db_root = Path(db_path).expanduser().resolve()
    storage = op_db_storage_summary(str(db_root))
    if "error" in storage:
        return {"status": "error", "error": storage["error"], "db_path": str(db_root)}

    sqlite_path = db_root / "chroma.sqlite3"
    sqlite_exists = sqlite_path.exists()
    sqlite_integrity: str | None = None
    sqlite_error: str | None = None
    if sqlite_exists:
        try:
            with sqlite3.connect(str(sqlite_path)) as conn:
                row = conn.execute("PRAGMA integrity_check;").fetchone()
                sqlite_integrity = str((row or [""])[0])
        except Exception as exc:
            sqlite_error = f"{type(exc).__name__}: {exc}"

    segment_dirs: list[str] = []
    hnsw_count = 0
    for dirpath, _dirnames, filenames in os.walk(db_root):
        if "link_lists.bin" in filenames:
            segment_dirs.append(str(Path(dirpath)))
            hnsw_count += 1

    chromadb_version: str | None = None
    try:
        import chromadb

        chromadb_version = str(getattr(chromadb, "__version__", "unknown"))
    except Exception:
        chromadb_version = None

    from state import get_query_health

    health = get_query_health(db_root)
    latest_health = health[-5:] if isinstance(health, list) else []

    # SQLite-only global consistency probe: compare distinct embedding_id in
    # `embeddings` vs id_to_label in HNSW segments, minus embeddings_queue lag.
    # Per-silo breakdown is in mcp_server.health() / op_silo_hnsw_consistency.
    hnsw_global_truly_missing: int | None = None
    hnsw_global_queued: int | None = None
    if sqlite_exists:
        try:
            from silo_audit import _hnsw_id_set, _embeddings_queue_id_set
            with sqlite3.connect(str(sqlite_path)) as conn:
                rows = conn.execute("SELECT DISTINCT embedding_id FROM embeddings").fetchall()
            sqlite_emb_ids = {r[0] for r in rows if r and r[0]}
            hnsw_ids = _hnsw_id_set(db_root)
            queued = _embeddings_queue_id_set(db_root)
            missing = sqlite_emb_ids - hnsw_ids
            hnsw_global_truly_missing = len(missing - queued)
            hnsw_global_queued = len(missing & queued)
        except Exception:
            pass

    return {
        "status": "ok",
        "db_path": str(db_root),
        "chromadb_version": chromadb_version,
        "sqlite_path": str(sqlite_path),
        "sqlite_exists": sqlite_exists,
        "sqlite_integrity_check": sqlite_integrity,
        "sqlite_integrity_error": sqlite_error,
        "segment_dir_count": len(segment_dirs),
        "segment_dirs": segment_dirs[:20],
        "hnsw_index_count": hnsw_count,
        "hnsw_global_truly_missing": hnsw_global_truly_missing,
        "hnsw_global_queued": hnsw_global_queued,
        "query_health_recent": latest_health,
        "storage": storage,
        "repair_ladder": {
            "l1": "llmli repair <silo>",
            "l2": "Run diagnostics only: sqlite integrity check + segment inspection.",
            "l3": "Run llmli rehydrate to rebuild into a fresh DB path from registry sources.",
        },
    }


def op_silo_hnsw_consistency(db_path: str) -> dict[str, Any]:
    """
    Per-silo SQLite ↔ HNSW consistency report. Opens a Chroma client.
    Returns {"status": "ok", "silos": [verify_silo_hnsw_consistency(...) per silo]}.
    """
    from chroma_client import get_client, release as _release
    from constants import LLMLI_COLLECTION
    from state import list_silos
    from silo_audit import verify_silo_hnsw_consistency

    db_root = Path(db_path).expanduser().resolve()
    try:
        silos = list_silos(str(db_root))
        client = get_client(str(db_root))
        coll = client.get_or_create_collection(name=LLMLI_COLLECTION)
        reports = []
        for s in silos:
            slug = s.get("slug")
            if not slug:
                continue
            reports.append(verify_silo_hnsw_consistency(coll, slug, db_root))
        bad = [r for r in reports if not r.get("consistent")]
        return {
            "status": "ok",
            "db_path": str(db_root),
            "silo_count": len(reports),
            "desynced_count": len(bad),
            "silos": reports,
        }
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            _release()
        except Exception:
            pass


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
    from chroma_client import get_client, release

    raw = slug_or_name
    path_slug = resolve_silo_by_path(db_path, raw) if Path(raw).exists() else None
    prefix_slug = resolve_silo_prefix(db_path, raw)
    removed_slug = remove_silo(db_path, path_slug or prefix_slug or raw)
    slug_to_clean = removed_slug if removed_slug is not None else slugify(raw)

    chroma_error: str | None = None
    from chroma_lock import chroma_exclusive_lock

    try:
        with chroma_exclusive_lock(db_path):
            coll = get_client(db_path).get_or_create_collection(name=LLMLI_COLLECTION)
            coll.delete(where={"silo": slug_to_clean})
    except Exception as e:
        chroma_error = str(e)
    finally:
        release()

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
    from chroma_client import get_client, release

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

    from chroma_lock import chroma_exclusive_lock

    try:
        with chroma_exclusive_lock(db_path):
            if verbose:
                print(f"[repair] Wiping ChromaDB chunks for silo '{slug}'...")
            try:
                coll = get_client(db_path).get_or_create_collection(name=LLMLI_COLLECTION)
                coll.delete(where={"silo": slug})
            except Exception as e:
                msg = f"could not wipe Chroma chunks for {slug}: {e}"
                if verbose:
                    print(f"[repair] Warning: {msg}", file=sys.stderr)
                try:
                    from state import record_index_error
                    record_index_error(db_path, slug, e)
                except Exception:
                    pass

            if verbose:
                print(f"[repair] Clearing file registry for silo '{slug}'...")
            _file_registry_remove_silo(db_path, slug)
            remove_manifest_silo(db_path, slug)

            # Drop the singleton PersistentClient before run_add opens its own
            # writer_client. Two live PersistentClients on the same persist dir
            # corrupt the HNSW segment writer (chroma_client.py:148).
            coll = None
            release()

            if verbose:
                print(f"[repair] Re-indexing '{source_path}' (full, non-incremental)...")
            files_ok, n_failures = run_add(path=source_path, db_path=db_path, incremental=False)
    finally:
        release()

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


def op_rehydrate_registry(
    db_path: str | Path,
    *,
    requested_silos: list[str] | None = None,
    dry_run: bool = False,
    verbose: bool = True,
) -> dict[str, Any]:
    """
    Rehydrate silos from llmli_registry.json by re-running full non-incremental add.

    Intended for L3 recovery after moving to a fresh DB path.
    """
    from ingest import run_add
    from state import list_silos, resolve_silo_prefix, resolve_silo_to_slug

    db = str(Path(db_path).resolve())
    silos = list_silos(db)
    by_slug = {str(s.get("slug") or ""): s for s in silos if s.get("slug")}
    if requested_silos:
        selected_slugs: list[str] = []
        for raw in requested_silos:
            slug = resolve_silo_to_slug(db, raw) or resolve_silo_prefix(db, raw) or raw
            if slug in by_slug and slug not in selected_slugs:
                selected_slugs.append(slug)
        targets = [by_slug[s] for s in selected_slugs if s in by_slug]
    else:
        targets = sorted(by_slug.values(), key=lambda row: str(row.get("slug") or ""))

    rows: list[dict[str, Any]] = []
    for item in targets:
        slug = str(item.get("slug") or "")
        source_path = str(item.get("path") or "")
        display_name = str(item.get("display_name") or slug)
        if not source_path:
            rows.append({"slug": slug, "status": "skipped", "reason": "missing_path"})
            continue
        p = Path(source_path)
        if not p.exists():
            rows.append({"slug": slug, "status": "skipped", "reason": "path_not_found", "path": source_path})
            continue
        if dry_run:
            rows.append({"slug": slug, "status": "planned", "path": source_path})
            continue
        if verbose:
            print(f"[rehydrate] Re-indexing silo '{slug}' from '{source_path}'...")
        try:
            files_ok, failures = run_add(
                path=source_path,
                db_path=db,
                incremental=False,
                forced_silo_slug=slug,
                display_name_override=display_name,
            )
            rows.append(
                {
                    "slug": slug,
                    "status": "completed",
                    "path": source_path,
                    "files_indexed": int(files_ok),
                    "failures": int(failures),
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "slug": slug,
                    "status": "error",
                    "path": source_path,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    return {
        "status": "ok",
        "db_path": db,
        "dry_run": bool(dry_run),
        "requested_silos": list(requested_silos or []),
        "total_targets": len(targets),
        "completed": sum(1 for r in rows if r.get("status") == "completed"),
        "planned": sum(1 for r in rows if r.get("status") == "planned"),
        "skipped": sum(1 for r in rows if r.get("status") == "skipped"),
        "errors": sum(1 for r in rows if r.get("status") == "error"),
        "results": rows,
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
    from chroma_client import get_client, release

    slug = resolve_silo_to_slug(db_path, slug_or_name)
    if slug is None:
        return {"error": f"silo not found: {slug_or_name!r}"}

    all_silos = list_silos(db_path)
    info = next((s for s in all_silos if s.get("slug") == slug), None)
    display = (info or {}).get("display_name", slug)
    path = (info or {}).get("path", "")
    total_registry = (info or {}).get("chunks_count", 0)

    from chroma_lock import chroma_shared_lock

    _PAGE_SIZE = 200
    try:
        with chroma_shared_lock(db_path):
            coll = get_client(db_path).get_or_create_collection(name=LLMLI_COLLECTION)
            metas: list = []
            offset = 0
            while True:
                result = coll.get(
                    where={"silo": slug},
                    include=["metadatas"],
                    limit=_PAGE_SIZE,
                    offset=offset,
                )
                page = result.get("metadatas") or []
                metas.extend(page)
                if len(page) < _PAGE_SIZE:
                    break
                offset += _PAGE_SIZE
    except Exception as e:
        return {"error": f"ChromaDB error: {e}"}
    finally:
        release()

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

def _doc_type_breakdown(by_ext: dict) -> dict:
    breakdown: dict[str, int] = {}
    for ext, count in (by_ext or {}).items():
        cat = doc_type_bucket_for_extension(str(ext or "").lower())
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
    from state import list_silos as _list_silos, get_last_failures, get_query_health

    silos = _list_silos(db_path)

    health_entries = get_query_health(db_path)
    silo_errors: dict[str, str] = {}
    for entry in health_entries:
        slug = entry.get("silo") or ""
        t = entry.get("time") or ""
        if slug and (slug not in silo_errors or t > silo_errors[slug]):
            silo_errors[slug] = t

    ingest_failures = get_last_failures(db_path)

    def _failure_belongs_to_silo(failure: dict, silo_path: str) -> bool:
        raw = (failure or {}).get("path") or ""
        if not raw or not silo_path:
            return False
        try:
            failure_path = Path(raw).expanduser().resolve()
            root = Path(silo_path).expanduser().resolve()
            return failure_path == root or failure_path.is_relative_to(root)
        except Exception:
            return str(raw).startswith(str(silo_path).rstrip("/") + "/")

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

        silo_failures = [
            failure for failure in ingest_failures
            if isinstance(failure, dict) and _failure_belongs_to_silo(failure, s.get("path") or "")
        ]
        s["has_ingest_failures"] = bool(silo_failures)
        s["last_ingest_failure_count"] = len(silo_failures)
        if silo_failures:
            s["last_ingest_failures"] = silo_failures[:5]
        else:
            s["last_ingest_failures"] = []

        if check_staleness:
            _inject_staleness(s)

    return {
        "db_path": db_path,
        "db_exists": Path(db_path).exists(),
        "silo_count": len(silos),
        "last_ingest_failure_count": len(ingest_failures),
        "silos": silos,
    }


# ---------------------------------------------------------------------------
# Pal bookmarks + daemon watch coverage (read-only, for MCP / diagnostics)
# ---------------------------------------------------------------------------

def _read_pal_source_registry(pal_home: Path) -> dict[str, Any]:
    """Load ~/.pal/registry.json shape expected by jobs_runtime.derive_watch_jobs."""
    path = pal_home / "registry.json"
    empty: dict[str, Any] = {"bookmarks": []}
    if not path.exists():
        return empty
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return empty
    if not isinstance(data, dict):
        return empty
    if "sources" in data and "bookmarks" not in data:
        raw = data.get("sources") or []
        data = dict(data)
        data["bookmarks"] = [
            {k: v for k, v in entry.items() if k in ("path", "name", "silo")}
            for entry in raw
            if isinstance(entry, dict) and entry.get("path")
        ]
        del data["sources"]
    if "bookmarks" not in data:
        data["bookmarks"] = []
    return data


def op_watch_coverage(db_path: str | Path, pal_home: str | Path | None = None) -> dict[str, Any]:
    """
    Summarize pal bookmarks vs llmli silos vs derived daemon watch jobs (read-only).

    Does not start watchers or mutate launchd/systemd. When daemon metadata exists,
    reports whether each job's unit file is present on disk.
    """
    import jobs_runtime as jobsrt
    from state import list_silos, resolve_silo_by_path

    db_resolved = str(Path(db_path).resolve())
    pal_home_p = (
        Path(pal_home).expanduser().resolve()
        if pal_home is not None
        else Path(os.environ.get("PAL_HOME", str(Path.home() / ".pal"))).expanduser().resolve()
    )

    source_registry = _read_pal_source_registry(pal_home_p)
    silos_list = list_silos(db_resolved)
    llmli_registry: dict[str, Any] = {str(s["slug"]): s for s in silos_list if s.get("slug")}

    manager = jobsrt.supported_service_manager() or "launchd"
    jobs, warnings = jobsrt.derive_watch_jobs(
        source_registry,
        llmli_registry,
        pal_home=pal_home_p,
        db_path=db_resolved,
        manager=manager,
    )

    bookmarks_out: list[dict[str, Any]] = []
    bookmark_resolved_paths: set[str] = set()
    for raw in source_registry.get("bookmarks") or []:
        if not isinstance(raw, dict):
            continue
        path_raw = str(raw.get("path") or "").strip()
        name = raw.get("name")
        name_str = str(name) if name is not None else ""
        silo_hint = raw.get("silo")
        hint_str = str(silo_hint) if silo_hint is not None else None

        resolved: Path | None = None
        path_exists = False
        if path_raw:
            try:
                resolved = Path(path_raw).expanduser().resolve()
                path_exists = resolved.exists() and resolved.is_dir()
            except Exception:
                resolved = None
                path_exists = False

        silo_slug: str | None = None
        if resolved is not None and path_exists:
            silo_slug = resolve_silo_by_path(db_resolved, resolved)
        if silo_slug is None and hint_str:
            hint_slug, _hint_warning = jobsrt._resolve_silo_by_hint(llmli_registry, hint_str)
            silo_slug = hint_slug
        if resolved is not None:
            bookmark_resolved_paths.add(str(resolved))

        effective_source_path: str | None = None
        if silo_slug:
            reg_row = llmli_registry.get(silo_slug) or {}
            raw_effective = str((reg_row or {}).get("path") or "").strip()
            if raw_effective:
                try:
                    effective_source_path = str(Path(raw_effective).expanduser().resolve())
                except Exception:
                    effective_source_path = raw_effective

        bookmarks_out.append(
            {
                "path": path_raw,
                "resolved_path": str(resolved) if resolved is not None else None,
                "name": name_str,
                "silo_hint": hint_str,
                "silo_slug": silo_slug,
                "path_exists": path_exists,
                "indexed": silo_slug is not None,
                "would_watch": bool(silo_slug) and bool(effective_source_path),
                "effective_source_path": effective_source_path,
            }
        )

    meta = jobsrt.read_daemon_metadata(pal_home_p)
    daemon_block: dict[str, Any] = {"installed": meta is not None}
    if isinstance(meta, dict):
        daemon_block.update(
            {
                "manager": meta.get("manager"),
                "python_executable": meta.get("python_executable"),
                "pal_path": meta.get("pal_path"),
                "workdir": meta.get("workdir"),
                "db_path": meta.get("db_path"),
            }
        )

    service_files: list[dict[str, Any]] = []
    if isinstance(meta, dict):
        manager_name = str(meta.get("manager") or "")
        if manager_name:
            try:
                pm = jobsrt.PlatformManager(manager_name)
                for job in jobs:
                    unit = pm.desired_path(job.slug)
                    service_files.append(
                        {
                            "slug": job.slug,
                            "service_name": job.service_name,
                            "unit_path": str(unit),
                            "unit_file_exists": unit.exists(),
                        }
                    )
            except ValueError:
                pass

    indexed_not_bookmarked: list[dict[str, Any]] = []
    for s in silos_list:
        sp = s.get("path") or ""
        if not sp:
            continue
        try:
            norm = str(Path(sp).resolve())
        except Exception:
            continue
        if norm not in bookmark_resolved_paths:
            indexed_not_bookmarked.append(
                {
                    "slug": s.get("slug"),
                    "display_name": s.get("display_name", s.get("slug")),
                    "path": sp,
                }
            )

    return {
        "pal_home": str(pal_home_p),
        "db_path": db_resolved,
        "daemon": daemon_block,
        "bookmarks": bookmarks_out,
        "watch_jobs": [asdict(j) for j in jobs],
        "service_files": service_files,
        "warnings": list(warnings),
        "indexed_not_bookmarked": indexed_not_bookmarked,
    }
