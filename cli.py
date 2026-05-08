#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""
llmLibrarian (llmli) CLI: add, ask, ls, index, rm, log.
Deterministic, locally-hosted context engine for high-stakes personal data.
Run from project root; uses archetypes.yaml and ./my_brain_db by default.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent


def _silo_completer(prefix, **kwargs):
    """Return silo slugs and display names for shell completion."""
    try:
        src = _ROOT / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        from state import list_silos
        db = os.environ.get("LLMLIBRARIAN_DB", str(_ROOT / "my_brain_db"))
        silos = list_silos(db)
        results = []
        for s in silos:
            slug = s.get("slug", "")
            display = s.get("display_name", "")
            if slug and slug.startswith(prefix):
                results.append(slug)
            if display and display != slug and display.startswith(prefix):
                results.append(display)
        return results
    except Exception:
        return []


def _iter_editable_roots(site_root: Path) -> list[Path]:
    roots: list[Path] = []
    for pth_path in sorted(site_root.glob("*llmlibrarian*.pth")):
        try:
            for raw_line in pth_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith("import "):
                    continue
                candidate = Path(line).expanduser()
                if candidate.exists():
                    roots.append(candidate.resolve())
        except Exception:
            continue
    return roots


def _bootstrap_src_path() -> None:
    candidates: list[Path] = []
    cwd_src = Path.cwd() / "src"
    if cwd_src.is_dir():
        candidates.append(cwd_src.resolve())

    root_src = _ROOT / "src"
    if (root_src / "state.py").exists():
        candidates.append(root_src.resolve())

    if (_ROOT / "state.py").exists():
        candidates.append(_ROOT)

    for editable_root in _iter_editable_roots(_ROOT):
        editable_src = editable_root / "src"
        if (editable_src / "state.py").exists():
            candidates.append(editable_src.resolve())
        elif (editable_root / "state.py").exists():
            candidates.append(editable_root.resolve())

    seen: set[str] = set()
    for candidate in candidates:
        candidate_str = str(candidate)
        if candidate_str in seen:
            continue
        seen.add(candidate_str)
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)


_bootstrap_src_path()

def _bootstrap_process_env() -> None:
    """Load non-repo-local env files (preferred) before command dispatch."""
    try:
        from env_bootstrap import bootstrap_llmlibrarian_env

        bootstrap_llmlibrarian_env(repo_root=_ROOT)
    except Exception:
        # Never block CLI startup on env bootstrap; missing secrets will fail later with context.
        return


_bootstrap_process_env()

def _db_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "db", None) or os.environ.get("LLMLIBRARIAN_DB", str(_ROOT / "my_brain_db"))).resolve()

def _config_path(args: argparse.Namespace) -> Path | None:
    return getattr(args, "config", None) or os.environ.get("LLMLIBRARIAN_CONFIG")

def _sanitize_query(parts: list[str]) -> str | None:
    query = " ".join(parts or [])
    query = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", query).strip()
    if not query:
        return None
    if len(query) > 8000:
        return None
    return query


def _truncate_tail(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return "..." + text[-(max_len - 3):]


def _truncate_mid(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    keep = max_len - 3
    left = max(1, keep // 2)
    right = max(1, keep - left)
    return text[:left] + "..." + text[-right:]

def cmd_add(args: argparse.Namespace) -> int:
    """Index a folder into the unified llmli collection; silo name = basename(path)."""
    from ingest import CloudSyncPathError
    from orchestration.ingest import IngestRequest, run_ingest

    path = Path(args.path).resolve()
    db = _db_path(args)
    if not path.is_dir() and not path.is_file():
        print(f"Error: not a file or directory: {path}", file=sys.stderr)
        return 1
    try:
        run_ingest(
            IngestRequest(
                path=path,
                db_path=db,
                no_color=args.no_color,
                allow_cloud=getattr(args, "allow_cloud", False),
                follow_symlinks=getattr(args, "follow_symlinks", False),
                incremental=not getattr(args, "full", False),
                forced_silo_slug=getattr(args, "silo", None),
                display_name=getattr(args, "display_name", None),
                exclude_patterns=getattr(args, "exclude_patterns", None),
                image_vision_enabled=getattr(args, "image_vision", None),
                workers=getattr(args, "workers", None),
                embedding_workers=getattr(args, "embedding_workers", None),
            )
        )
        return 0
    except CloudSyncPathError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

def cmd_ask(args: argparse.Namespace) -> int:
    """Query an archetype's collection or unified llmli collection (default) via Ollama."""
    from query.core import run_ask, QueryPolicyError
    from state import resolve_silo_to_slug, resolve_silo_prefix
    config = _config_path(args)
    archetype = getattr(args, "archetype", None)
    in_silo = getattr(args, "in_silo", None)
    if archetype and in_silo:
        print("Error: use either --archetype or --in, not both.", file=sys.stderr)
        return 1
    unified = getattr(args, "unified", False)
    if in_silo and not unified:
        db = _db_path(args)
        silo_slug = resolve_silo_to_slug(db, in_silo) or resolve_silo_prefix(db, in_silo) or in_silo
        archetype = None  # unified collection, filter by silo
    else:
        silo_slug = None  # query all silos (unified)
        if not archetype:
            archetype = None
    query = _sanitize_query(args.query)
    if query is None:
        print("Error: empty or invalid query (or too long).", file=sys.stderr)
        return 1
    try:
        out = run_ask(
            archetype,
            query,
            config_path=config,
            n_results=getattr(args, "n_results", 12),
            model=getattr(args, "model", None) or os.environ.get("LLMLIBRARIAN_MODEL", "llama3.1:8b"),
            no_color=args.no_color or getattr(args, "quiet", False),
            silo=silo_slug,
            db_path=_db_path(args),
            strict=getattr(args, "strict", False),
            quiet=getattr(args, "quiet", False),
            explain=getattr(args, "explain", False),
            force=getattr(args, "force", False),
            explicit_unified=bool(getattr(args, "unified", False)),
        )
    except QueryPolicyError as e:
        print(str(e), file=sys.stderr)
        return int(getattr(e, "exit_code", 2) or 2)
    print(out)
    return 0

def cmd_ls(args: argparse.Namespace) -> int:
    """List silos in the llmli registry."""
    from state import list_silos
    from style import dim, link_style
    db = _db_path(args)
    silos = list_silos(db)
    if not silos:
        print(dim(args.no_color, "No silos. Use: llmli add <path>"))
        return 0
    rows = []
    for s in silos:
        slug = s.get("slug", "?")
        display = s.get("display_name", slug)
        path = s.get("path", "?")
        files = int(s.get("files_indexed", 0) or 0)
        chunks = int(s.get("chunks_count", 0) or 0)
        updated = (s.get("updated", "") or "")[:19]
        rows.append((display, slug, files, chunks, updated, path))

    name_w = min(max(len(r[0]) for r in rows), 24)
    slug_w = min(max(len(r[1]) for r in rows), 20)
    path_w = 60
    header = f"{'Name':<{name_w}}  {'Slug':<{slug_w}}  {'Files':>5}  {'Chunks':>7}  {'Updated':<19}  Path"
    print(header)
    print("-" * len(header))
    for display, slug, files, chunks, updated, path in rows:
        display = _truncate_mid(display, name_w)
        slug = _truncate_mid(slug, slug_w)
        short_path = _truncate_tail(path, path_w)
        url = f"file://{path}" if path and path != "?" else ""
        path_cell = link_style(args.no_color, url, short_path)
        print(f"{display:<{name_w}}  {slug:<{slug_w}}  {files:>5}  {chunks:>7}  {updated:<19}  {path_cell}")
    return 0

def cmd_index(args: argparse.Namespace) -> int:
    """Rebuild an archetype's collection from config folders."""
    from ingest import run_index
    arch = getattr(args, "archetype", None)
    if not arch:
        print("Error: index requires --archetype <id>", file=sys.stderr)
        return 1
    config = _config_path(args)
    try:
        run_index(
            arch,
            config_path=config,
            no_color=args.no_color,
            log_path=getattr(args, "log", None),
            mode=getattr(args, "mode", "normal"),
            follow_symlinks=getattr(args, "follow_symlinks", False),
        )
        return 0
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

def cmd_rm(args: argparse.Namespace) -> int:
    """Remove a silo from the registry and delete its chunks. Accepts slug or display name.
    If the silo is not in the silo list, still removes Chroma chunks and file-registry entries
    for that slug (fixes orphaned 'desktop' etc.)."""
    silo = getattr(args, "silo", None)
    if not silo:
        print("Error: remove requires silo name. Example: llmli remove \"Tax\"", file=sys.stderr)
        return 1
    from operations import op_remove_silo
    db = _db_path(args)
    raw = " ".join(silo) if isinstance(silo, list) else str(silo)
    result = op_remove_silo(str(db), raw)
    if result.get("chroma_warning"):
        print(f"Warning: could not delete chunks from DB: {result['chroma_warning']}", file=sys.stderr)
    if not result["not_found"]:
        print(f"Removed silo: {result['removed_slug']}")
    else:
        print(f"Removed chunks and file registry for silo: {result['cleaned_slug']} (was not in silo list)")
    return 0

def cmd_repair(args: argparse.Namespace) -> int:
    """Wipe and re-index a silo to fix ChromaDB index inconsistencies ('Error finding id')."""
    silo_arg = getattr(args, "silo", None)
    if not silo_arg:
        print("Error: repair requires a silo slug or display name.", file=sys.stderr)
        return 1
    from operations import op_repair_silo
    db = _db_path(args)
    raw = " ".join(silo_arg) if isinstance(silo_arg, list) else str(silo_arg)
    result = op_repair_silo(str(db), raw, verbose=True)
    if result.get("status") == "error":
        print(f"Error: {result['error']}", file=sys.stderr)
        return 1
    return 0


def cmd_repair_ladder(args: argparse.Namespace) -> int:
    """Read-only L2 diagnostics for Chroma + suggested L1/L3 next steps."""
    from operations import op_chroma_diagnostics

    db = _db_path(args)
    result = op_chroma_diagnostics(str(db))
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") != "error" else 1
    if result.get("status") == "error":
        print(f"Error: {result.get('error', 'unknown error')}", file=sys.stderr)
        return 1
    print(f"DB path: {result['db_path']}")
    print(f"Chroma version: {result.get('chromadb_version') or 'unknown'}")
    print(f"SQLite file: {result['sqlite_path']} (exists={result['sqlite_exists']})")
    print(f"SQLite integrity_check: {result.get('sqlite_integrity_check') or '?'}")
    if result.get("sqlite_integrity_error"):
        print(f"SQLite check error: {result['sqlite_integrity_error']}", file=sys.stderr)
    print(f"Segment directories: {result.get('segment_dir_count', 0)}")
    print(f"HNSW indexes: {result.get('hnsw_index_count', 0)}")
    storage = result.get("storage") or {}
    if storage.get("chroma_hnsw_bloat"):
        print("[warn] HNSW bloat detected.", file=sys.stderr)
        print(storage.get("chroma_hnsw_bloat_note", ""), file=sys.stderr)
    ladder = result.get("repair_ladder") or {}
    print("\nRepair ladder:")
    print(f"  L1: {ladder.get('l1', 'llmli repair <silo>')}")
    print(f"  L2: {ladder.get('l2', 'diagnostics only')}")
    print(f"  L3: {ladder.get('l3', 'rehydrate from registry into fresh DB path')}")
    return 0


def cmd_rehydrate(args: argparse.Namespace) -> int:
    """Rebuild one or more silos from llmli_registry into the current DB path."""
    from operations import op_rehydrate_registry

    db = _db_path(args)
    requested = getattr(args, "silos", None)
    result = op_rehydrate_registry(
        db,
        requested_silos=requested,
        dry_run=bool(getattr(args, "dry_run", False)),
        verbose=not bool(getattr(args, "quiet", False)),
    )
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        mode = "planned" if result.get("dry_run") else "completed"
        print(
            f"Rehydrate {mode}: targets={result.get('total_targets', 0)} "
            f"completed={result.get('completed', 0)} planned={result.get('planned', 0)} "
            f"skipped={result.get('skipped', 0)} errors={result.get('errors', 0)}"
        )
        for row in result.get("results", []):
            slug = row.get("slug", "?")
            status = row.get("status", "unknown")
            if status == "completed":
                print(
                    f"  {slug}: files_indexed={row.get('files_indexed', 0)} "
                    f"failures={row.get('failures', 0)}"
                )
            elif status == "planned":
                print(f"  {slug}: planned ({row.get('path', '?')})")
            elif status == "skipped":
                print(f"  {slug}: skipped ({row.get('reason', 'unknown')})")
            else:
                print(f"  {slug}: error ({row.get('error', 'unknown')})", file=sys.stderr)
    return 0 if int(result.get("errors", 0) or 0) == 0 else 1


def cmd_capabilities(args: argparse.Namespace) -> int:
    """Print supported file types and extractors (source of truth). No LLM, no retrieval."""
    from ingest import get_capabilities_text
    print(get_capabilities_text())
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    """Show last index/add failures (from llmli add)."""
    from state import get_last_failures
    db = _db_path(args)
    failures = get_last_failures(db)
    if not failures:
        print("No recent failures.")
        return 0
    for f in failures:
        print(f"  {f.get('path', '?')}: {f.get('error', '?')}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Show silo details: path, total chunks, and per-file chunk counts (from indexed data)."""
    from operations import op_inspect_silo
    db = _db_path(args)
    silo_arg = getattr(args, "silo", None)
    if not silo_arg:
        print("Error: inspect requires silo name. Example: llmli inspect stuff", file=sys.stderr)
        return 1
    top_n = max(1, getattr(args, "top", 20))
    result = op_inspect_silo(str(db), silo_arg, top=top_n)
    if "error" in result:
        print(f"Error: {result['error']}", file=sys.stderr)
        return 1

    slug = result["slug"]
    display = result["display_name"]
    path = result.get("path", "?")
    total_registry = result["total_chunks_registry"]
    total_chroma = result["total_chunks_chroma"]
    print(f"Silo: {display} ({slug})")
    print(f"Path: {path}")
    print(f"Total chunks (registry): {total_registry}")
    if not result["registry_match"]:
        print(
            f"[llmli] registry mismatch: Chroma has {total_chroma} chunks for this silo, "
            f"registry says {total_registry}. Re-run add to fix.",
            file=sys.stderr,
        )

    files = result.get("files", [])
    if not files:
        print("No indexed files (chunks) found for this silo in the store.")
        return 0

    # Apply CLI-only extension filter (not exposed in MCP)
    ext_filter = getattr(args, "filter", None)
    if ext_filter == "pdf":
        files = [f for f in files if f["source"].lower().endswith(".pdf")]
    elif ext_filter == "docx":
        files = [f for f in files if f["source"].lower().endswith(".docx")]
    elif ext_filter == "code":
        code_exts = (".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".c", ".cpp", ".cs", ".rb", ".sh", ".md", ".txt", ".yml", ".yaml")
        files = [f for f in files if any(f["source"].lower().endswith(e) for e in code_exts)]

    total_files = result["total_files"]
    print(f"Indexed files: {total_files}" + (f" (showing top {top_n})" if total_files > top_n else ""))
    for file_info in files:
        src = file_info["source"]
        count = file_info["chunk_count"]
        short = src if len(src) <= 72 else "..." + src[-69:]
        dup_note = "  [same content as duplicate file]" if file_info.get("has_duplicate_content") else ""
        print(f"  {count:6d} chunks  {short}{dup_note}")
    return 0


def _parse_find_date(raw: str | None) -> tuple[Any, Any]:
    """Parse --date as either a single ISO date or a START:END range."""
    from datetime import date as _date

    if not raw:
        return None, None
    sep = ":" if ":" in raw else (".." if ".." in raw else None)
    if sep is None:
        try:
            d = _date.fromisoformat(raw)
        except ValueError as e:
            raise ValueError(f"invalid --date value {raw!r}: {e}") from e
        return d, d
    parts = raw.split(sep, 1)
    lo_raw = parts[0].strip()
    hi_raw = parts[1].strip()
    try:
        lo = _date.fromisoformat(lo_raw) if lo_raw else None
        hi = _date.fromisoformat(hi_raw) if hi_raw else None
    except ValueError as e:
        raise ValueError(f"invalid --date range {raw!r}: {e}") from e
    return lo, hi


def cmd_find(args: argparse.Namespace) -> int:
    """Find files by name and/or date against the manifest (no embeddings)."""
    import json

    from operations_find import op_find_files
    from state import resolve_silo_to_slug, resolve_silo_prefix

    db = _db_path(args)
    silo_args = getattr(args, "in_silo", None) or []
    resolved_silos: list[str] = []
    for raw in silo_args:
        slug = resolve_silo_to_slug(db, raw) or resolve_silo_prefix(db, raw) or raw
        if slug not in resolved_silos:
            resolved_silos.append(slug)

    try:
        date_start, date_end = _parse_find_date(getattr(args, "date", None))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    try:
        result = op_find_files(
            db,
            silos=resolved_silos or None,
            name_glob=getattr(args, "name", None),
            date_start=date_start,
            date_end=date_end,
            date_field=getattr(args, "field", "either") or "either",
            include_chunk_count=bool(getattr(args, "with_chunks", False)),
            limit=int(getattr(args, "limit", 50) or 50),
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, default=str))
        return 0

    from query.find_format import (
        format_filename_lookup,
        format_filename_lookup_with_excerpt,
        render_range_label,
    )

    hits = result.get("files") or []
    warnings = result.get("warnings") or []
    range_label = render_range_label(date_start, date_end)
    source_label = ", ".join(resolved_silos) if resolved_silos else ""

    if len(hits) == 1 and getattr(args, "with_chunks", False):
        out = format_filename_lookup_with_excerpt(
            hits[0],
            db_path=str(db),
            source_label=source_label,
            no_color=args.no_color,
            range_label=range_label,
        )
    else:
        out = format_filename_lookup(
            hits,
            source_label=source_label,
            no_color=args.no_color,
            range_label=range_label,
        )
    print(out)
    if result.get("truncated"):
        print(f"(truncated to {len(hits)} of {result.get('total_matched')} matches)", file=sys.stderr)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def cmd_reindex_names(args: argparse.Namespace) -> int:
    """Walk the manifest and persist name_date / name_date_precision for files missing it."""
    from file_registry import _read_file_manifest, _update_file_manifest
    from query.filename_dates import parse_filename_date

    db = _db_path(args)
    manifest = _read_file_manifest(db)
    silo_map = manifest.get("silos") or {}
    if not isinstance(silo_map, dict) or not silo_map:
        print("No silos in manifest. Nothing to do.")
        return 0

    target_silos = set(getattr(args, "in_silo", None) or [])
    updated = 0
    cleared = 0
    scanned = 0

    def _update(m: dict[str, Any]) -> None:
        nonlocal updated, cleared, scanned
        smap = m.get("silos") or {}
        for slug, silo_entry in smap.items():
            if target_silos and slug not in target_silos:
                continue
            files_map = silo_entry.get("files") or {}
            if not isinstance(files_map, dict):
                continue
            for path_str, info in files_map.items():
                if not isinstance(info, dict):
                    continue
                scanned += 1
                fresh_iso, fresh_precision = parse_filename_date(path_str)
                stored_iso = info.get("name_date")
                stored_precision = info.get("name_date_precision")
                if fresh_iso:
                    if stored_iso != fresh_iso or stored_precision != fresh_precision:
                        info["name_date"] = fresh_iso
                        info["name_date_precision"] = fresh_precision
                        updated += 1
                elif stored_iso:
                    info.pop("name_date", None)
                    info.pop("name_date_precision", None)
                    cleared += 1

    _update_file_manifest(db, _update)
    print(f"Scanned {scanned} files. Wrote name_date for {updated}; cleared {cleared} stale entries.")
    return 0


def cmd_eval_adversarial(args: argparse.Namespace) -> int:
    """Run synthetic adversarial trustfulness evaluation and emit score report."""
    from llmli_evals.adversarial import run_adversarial_eval, format_report_table

    db = _db_path(args)
    out = getattr(args, "out", None)
    model = getattr(args, "model", None) or os.environ.get("LLMLIBRARIAN_MODEL", "llama3.1:8b")
    limit = getattr(args, "limit", None)
    strict_mode = bool(getattr(args, "strict_mode", True))
    direct_decisive_mode = getattr(args, "direct_decisive_mode", None)
    try:
        report = run_adversarial_eval(
            db_path=db,
            out_path=out,
            model=model,
            limit=limit,
            strict_mode=strict_mode,
            direct_decisive_mode=direct_decisive_mode,
            config_path=getattr(args, "config", None),
            no_color=args.no_color,
        )
        print(format_report_table(report))
        if out:
            print(f"\nReport JSON: {out}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="llmli", description="llmLibrarian CLI: add, ask, ls, inspect, index, rm, capabilities, log")
    parser.add_argument("--db", default=os.environ.get("LLMLIBRARIAN_DB", str(_ROOT / "my_brain_db")), help="DB path")
    parser.add_argument("--config", help="Path to archetypes.yaml")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color")
    sub = parser.add_subparsers(dest="command", required=True)

    # add <path> [--allow-cloud]
    p_add = sub.add_parser("add", help="Index folder (or single file) into llmli (silo = basename); cloud-sync paths blocked unless --allow-cloud")
    p_add.add_argument("path", help="Folder or file path to index (single files bypass include/exclude filters)")
    p_add.add_argument("--allow-cloud", action="store_true", help="Allow OneDrive/iCloud/Dropbox/Google Drive (ingestion may be unreliable)")
    p_add.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks inside the target folder")
    p_add.add_argument("--full", action="store_true", help="Full reindex (delete + add) instead of incremental")
    p_add.add_argument("--exclude", action="append", dest="exclude_patterns", help="Extra path exclusion pattern (repeatable)")
    p_add.add_argument("--image-vision", action="store_true", default=None, help="Enable multimodal image summaries for this silo (default: off unless previously enabled)")
    p_add.add_argument("--workers", type=int, help="Override file/extraction worker count for this run")
    p_add.add_argument("--embedding-workers", type=int, help="Override embedding worker count for this run")
    p_add.add_argument("--silo", dest="silo", help=argparse.SUPPRESS)
    p_add.add_argument("--display-name", dest="display_name", help=argparse.SUPPRESS)
    p_add.set_defaults(db=None)
    p_add.set_defaults(_run=cmd_add)

    # ask [--archetype X | --in <silo>] [--strict] <query...>  (default: unified llmli collection)
    p_ask = sub.add_parser("ask", help="Ask (unified llmli by default; or archetype / --in silo)")
    p_ask.add_argument("--archetype", "-a", help="Archetype id (e.g. tax, infra)")
    in_silo_arg = p_ask.add_argument("--in", dest="in_silo", metavar="SILO", help="Limit to one silo (slug or display name)")
    in_silo_arg.completer = _silo_completer  # type: ignore[attr-defined]
    p_ask.add_argument("--unified", action="store_true", help="Search across all silos (compare/analyze across indexed content)")
    p_ask.add_argument("--strict", action="store_true", help="Never conclude absence from partial evidence; say unknown + sources when unsure")
    p_ask.add_argument("--quiet", "-q", action="store_true", help="Answer only (no source footer); useful for scripting")
    p_ask.add_argument("--explain", action="store_true", help="Print deterministic catalog diagnostics to stderr when applicable")
    p_ask.add_argument("--force", action="store_true", help="Allow deterministic catalog queries to run on stale scope")
    p_ask.add_argument("--model", "-m", help="Ollama model (default: LLMLIBRARIAN_MODEL or llama3.1:8b)")
    p_ask.add_argument("--n-results", type=int, default=12, help="Retrieval count")
    p_ask.add_argument("query", nargs="+", help="Question")
    p_ask.set_defaults(_run=cmd_ask)

    # ls
    p_ls = sub.add_parser("ls", help="List silos")
    p_ls.set_defaults(_run=cmd_ls)

    # inspect <silo> [--top N] [--filter pdf|docx|code]
    p_inspect = sub.add_parser("inspect", help="Show silo details and top files by chunk count (default: top 20)")
    inspect_silo_arg = p_inspect.add_argument("silo", help="Silo slug or display name")
    inspect_silo_arg.completer = _silo_completer  # type: ignore[attr-defined]
    p_inspect.add_argument("--top", type=int, default=20, help="Show top N chunkiest files (default: 20)")
    p_inspect.add_argument("--filter", choices=["pdf", "docx", "code"], help="Show only this type (pdf, docx, or code)")
    p_inspect.set_defaults(_run=cmd_inspect)

    # index --archetype X
    p_index = sub.add_parser("index", help="Rebuild archetype collection from config folders")
    p_index.add_argument("--archetype", "-a", required=True, help="Archetype id")
    p_index.add_argument("--log", help="Log file path (or set LLMLIBRARIAN_LOG=1)")
    p_index.add_argument("--mode", choices=["fast", "normal", "deep"], default="normal")
    p_index.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks in config folders")
    p_index.set_defaults(_run=cmd_index)

    # remove <silo>
    p_rm = sub.add_parser("rm", help="Remove silo (registry + chunks)")
    rm_silo_arg = p_rm.add_argument("silo", nargs="+", help="Silo slug, display name, or path")
    rm_silo_arg.completer = _silo_completer  # type: ignore[attr-defined]
    p_rm.set_defaults(_run=cmd_rm)

    # repair <silo>
    p_repair = sub.add_parser("repair", help="Fix ChromaDB index errors by wiping and re-indexing a silo")
    repair_silo_arg = p_repair.add_argument("silo", nargs="+", help="Silo slug or display name")
    repair_silo_arg.completer = _silo_completer  # type: ignore[attr-defined]
    p_repair.set_defaults(_run=cmd_repair)

    # repair-ladder (L2 diagnostics)
    p_repair_ladder = sub.add_parser(
        "repair-ladder",
        help="Read-only Chroma diagnostics and L1→L3 repair guidance",
    )
    p_repair_ladder.add_argument("--json", action="store_true", help="Emit diagnostics as JSON")
    p_repair_ladder.set_defaults(_run=cmd_repair_ladder)

    # rehydrate [SILO ...]
    p_rehydrate = sub.add_parser(
        "rehydrate",
        help="Re-index silos from llmli_registry (L3 rebuild helper)",
    )
    rehydrate_silo_arg = p_rehydrate.add_argument(
        "silos",
        nargs="*",
        help="Optional silo slug/display/prefix list (default: all registry silos)",
    )
    rehydrate_silo_arg.completer = _silo_completer  # type: ignore[attr-defined]
    p_rehydrate.add_argument("--dry-run", action="store_true", help="Show what would run without indexing")
    p_rehydrate.add_argument("--quiet", action="store_true", help="Suppress per-silo progress output")
    p_rehydrate.add_argument("--json", action="store_true", help="Emit result as JSON")
    p_rehydrate.set_defaults(_run=cmd_rehydrate)

    # capabilities
    p_capabilities = sub.add_parser("capabilities", help="Supported file types and document extractors (source of truth)")
    p_capabilities.set_defaults(_run=cmd_capabilities)

    # log [--last]
    p_log = sub.add_parser("log", help="Show last add failures")
    p_log.add_argument("--last", action="store_true", help="Show last add failures (default)")
    p_log.set_defaults(_run=cmd_log)

    # find [--in SILO ...] [--name GLOB] [--date YYYY-MM-DD | START:END]
    p_find = sub.add_parser("find", help="Find files by name/date (manifest-only; no embeddings)")
    find_in_arg = p_find.add_argument("--in", dest="in_silo", action="append", metavar="SILO", help="Restrict to silo (repeatable)")
    find_in_arg.completer = _silo_completer  # type: ignore[attr-defined]
    p_find.add_argument("--name", metavar="GLOB", help="fnmatch-style glob applied to relative path or filename")
    p_find.add_argument("--date", metavar="DATE", help="YYYY-MM-DD or START:END (either bound may be empty)")
    p_find.add_argument("--field", choices=["name_date", "mtime", "either"], default="either", help="Which date signal to filter on (default: either)")
    p_find.add_argument("--with-chunks", action="store_true", help="Include chunk_count per file (touches Chroma)")
    p_find.add_argument("--limit", type=int, default=50, help="Cap result count (default: 50)")
    p_find.add_argument("--json", action="store_true", help="Emit raw JSON instead of formatted text")
    p_find.set_defaults(_run=cmd_find)

    # reindex --names [--in SILO ...]
    p_reindex = sub.add_parser("reindex", help="One-shot maintenance commands for the manifest")
    reindex_sub = p_reindex.add_subparsers(dest="reindex_subcommand", required=True)
    p_reindex_names = reindex_sub.add_parser("names", help="Backfill name_date / name_date_precision in the manifest")
    reindex_in_arg = p_reindex_names.add_argument("--in", dest="in_silo", action="append", metavar="SILO", help="Restrict to silo (repeatable)")
    reindex_in_arg.completer = _silo_completer  # type: ignore[attr-defined]
    p_reindex_names.set_defaults(_run=cmd_reindex_names)

    # eval-adversarial [--model M] [--out report.json] [--limit N]
    p_eval = sub.add_parser("eval-adversarial", help="Run synthetic adversarial trustfulness eval")
    p_eval.add_argument("--model", "-m", help="Ollama model (default: LLMLIBRARIAN_MODEL or llama3.1:8b)")
    p_eval.add_argument("--out", help="Write JSON report to this path")
    p_eval.add_argument("--limit", type=int, help="Run only first N queries from the fixed suite")
    p_eval.add_argument("--strict-mode", dest="strict_mode", action="store_true", default=True, help="Run eval queries with strict ask mode (default: on)")
    p_eval.add_argument("--no-strict-mode", dest="strict_mode", action="store_false", help="Run eval queries without strict ask mode")
    p_eval.add_argument("--direct-decisive-mode", action="store_true", default=None, help="Override config to enable direct decisive mode for this eval run")
    p_eval.add_argument("--no-direct-decisive-mode", dest="direct_decisive_mode", action="store_false", help="Override config to disable direct decisive mode for this eval run")
    p_eval.set_defaults(_run=cmd_eval_adversarial)

    try:
        import argcomplete
        argcomplete.autocomplete(parser)
    except ImportError:
        pass
    args = parser.parse_args()
    return args._run(args)

if __name__ == "__main__":
    sys.exit(main())
