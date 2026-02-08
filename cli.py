#!/usr/bin/env python3
"""
llmLibrarian (llmli) CLI: add, ask, ls, index, rm, log.
Deterministic, locally-hosted context engine for high-stakes personal data.
Run from project root; uses archetypes.yaml and ./my_brain_db by default.
"""
import argparse
import os
import re
import sys
from pathlib import Path

# Ensure src is on path when running from project root
_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

def _db_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "db", None) or os.environ.get("LLMLIBRARIAN_DB", "./my_brain_db"))

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

def cmd_add(args: argparse.Namespace) -> int:
    """Index a folder into the unified llmli collection; silo name = basename(path)."""
    from indexer import run_add, DB_PATH
    from ingest import CloudSyncPathError
    path = Path(args.path).resolve()
    db = args.db or DB_PATH
    if not path.is_dir():
        print(f"Error: not a directory: {path}", file=sys.stderr)
        return 1
    try:
        run_add(
            path,
            db_path=db,
            no_color=args.no_color,
            allow_cloud=getattr(args, "allow_cloud", False),
            follow_symlinks=getattr(args, "follow_symlinks", False),
            incremental=not getattr(args, "full", False),
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
    from query import run_ask
    from state import resolve_silo_to_slug
    config = _config_path(args)
    archetype = getattr(args, "archetype", None)
    in_silo = getattr(args, "in_silo", None)
    if archetype and in_silo:
        print("Error: use either --archetype or --in, not both.", file=sys.stderr)
        return 1
    unified = getattr(args, "unified", False)
    if in_silo and not unified:
        db = _db_path(args)
        silo_slug = resolve_silo_to_slug(db, in_silo) or in_silo  # accept slug or display name
        archetype = None  # unified collection, filter by silo
    else:
        silo_slug = None  # query all silos (unified)
        if not archetype:
            archetype = None
    query = _sanitize_query(args.query)
    if query is None:
        print("Error: empty or invalid query (or too long).", file=sys.stderr)
        return 1
    out = run_ask(
        archetype,
        query,
        config_path=config,
        n_results=getattr(args, "n_results", 12),
        model=getattr(args, "model", None) or os.environ.get("LLMLIBRARIAN_MODEL", "llama3.1:8b"),
        no_color=args.no_color,
        silo=silo_slug,
        db_path=_db_path(args),
        strict=getattr(args, "strict", False),
    )
    print(out)
    return 0

def cmd_ls(args: argparse.Namespace) -> int:
    """List silos in the llmli registry."""
    from state import list_silos
    from style import dim
    db = _db_path(args)
    silos = list_silos(db)
    if not silos:
        print(dim(args.no_color, "No silos. Use: llmli add <path>"))
        return 0
    for s in silos:
        slug = s.get("slug", "?")
        display = s.get("display_name", slug)
        path = s.get("path", "?")
        files = s.get("files_indexed", 0)
        chunks = s.get("chunks_count", 0)
        updated = s.get("updated", "")[:19]
        print(f"  {display} ({slug})\t{path}\t{files} files, {chunks} chunks\t{updated}")
    return 0

def cmd_index(args: argparse.Namespace) -> int:
    """Rebuild an archetype's collection from config folders."""
    from indexer import run_index
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
        print("Error: rm requires silo name as positional argument. Example: llmli rm tax", file=sys.stderr)
        return 1
    from state import remove_silo, slugify
    from indexer import LLMLI_COLLECTION, DB_PATH
    from ingest import _file_registry_remove_silo
    import chromadb
    from chromadb.config import Settings
    db = _db_path(args)
    removed_slug = remove_silo(db, silo)
    slug_to_clean = removed_slug if removed_slug is not None else slugify(silo)
    try:
        client = chromadb.PersistentClient(path=str(db), settings=Settings(anonymized_telemetry=False))
        coll = client.get_or_create_collection(name=LLMLI_COLLECTION)
        coll.delete(where={"silo": slug_to_clean})
    except Exception as e:
        print(f"Warning: could not delete chunks from DB: {e}", file=sys.stderr)
    _file_registry_remove_silo(db, slug_to_clean)
    if removed_slug is not None:
        print(f"Removed silo: {removed_slug}")
    else:
        print(f"Removed chunks and file registry for silo: {slug_to_clean} (was not in silo list)")
    return 0

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
    from state import list_silos, resolve_silo_to_slug
    from indexer import LLMLI_COLLECTION
    import chromadb
    from chromadb.config import Settings
    db = _db_path(args)
    silo_arg = getattr(args, "silo", None)
    if not silo_arg:
        print("Error: inspect requires silo name. Example: llmli inspect stuff", file=sys.stderr)
        return 1
    slug = resolve_silo_to_slug(db, silo_arg)
    if slug is None:
        print(f"Error: silo not found: {silo_arg}", file=sys.stderr)
        return 1
    silos = list_silos(db)
    info = next((s for s in silos if s.get("slug") == slug), None)
    display = (info or {}).get("display_name", slug)
    path = (info or {}).get("path", "?")
    total_chunks = (info or {}).get("chunks_count", 0)
    print(f"Silo: {display} ({slug})")
    print(f"Path: {path}")
    print(f"Total chunks (registry): {total_chunks}")
    try:
        client = chromadb.PersistentClient(path=str(db), settings=Settings(anonymized_telemetry=False))
        coll = client.get_or_create_collection(name=LLMLI_COLLECTION)
        # Get all chunk metadatas for this silo to aggregate by source file
        result = coll.get(where={"silo": slug}, include=["metadatas"])
        metas = result.get("metadatas") or []
        chroma_count = len(metas)
        if chroma_count != total_chunks:
            print(f"[llmli] registry mismatch: Chroma has {chroma_count} chunks for this silo, registry says {total_chunks}. Re-run add to fix.", file=sys.stderr)
        by_source: dict[str, int] = {}
        source_to_hash: dict[str, str] = {}
        for m in metas:
            meta = m or {}
            src = meta.get("source") or "?"
            by_source[src] = by_source.get(src, 0) + 1
            if src not in source_to_hash and meta.get("file_hash"):
                source_to_hash[src] = meta["file_hash"]
        if not by_source:
            print("No indexed files (chunks) found for this silo in the store.")
            return 0
        # Group by file_hash to mark duplicates (same content, different path)
        hash_to_sources: dict[str, list[str]] = {}
        for s, h in source_to_hash.items():
            if h:
                hash_to_sources.setdefault(h, []).append(s)
        ext_filter = getattr(args, "filter", None)
        if ext_filter == "pdf":
            by_source = {s: c for s, c in by_source.items() if s.lower().endswith(".pdf")}
        elif ext_filter == "docx":
            by_source = {s: c for s, c in by_source.items() if s.lower().endswith(".docx")}
        elif ext_filter == "code":
            code_exts = (".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".c", ".cpp", ".cs", ".rb", ".sh", ".md", ".txt", ".yml", ".yaml")
            by_source = {s: c for s, c in by_source.items() if any(s.lower().endswith(e) for e in code_exts)}
        top_n = max(1, getattr(args, "top", 20))
        total_files = len(by_source)
        print(f"Indexed files: {total_files}" + (f" (showing top {top_n})" if total_files > top_n else ""))
        sorted_sources = sorted(by_source.items(), key=lambda x: -x[1])[:top_n]
        for src, count in sorted_sources:
            short = src if len(src) <= 72 else "..." + src[-69:]
            dup_note = ""
            h = source_to_hash.get(src)
            if h and len(hash_to_sources.get(h, [])) > 1:
                others = [s for s in hash_to_sources[h] if s != src]
                dup_note = f"  [same content as {len(others)} other file(s)]"
            print(f"  {count:6d} chunks  {short}{dup_note}")
    except Exception as e:
        print(f"Error reading store: {e}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="llmli", description="llmLibrarian CLI: add, ask, ls, inspect, index, rm, capabilities, log")
    parser.add_argument("--db", default=os.environ.get("LLMLIBRARIAN_DB", "./my_brain_db"), help="DB path")
    parser.add_argument("--config", help="Path to archetypes.yaml")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color")
    sub = parser.add_subparsers(dest="command", required=True)

    # add <path> [--allow-cloud]
    p_add = sub.add_parser("add", help="Index folder into llmli (silo = basename); cloud-sync paths blocked unless --allow-cloud")
    p_add.add_argument("path", help="Folder path to index")
    p_add.add_argument("--allow-cloud", action="store_true", help="Allow OneDrive/iCloud/Dropbox/Google Drive (ingestion may be unreliable)")
    p_add.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks inside the target folder")
    p_add.add_argument("--full", action="store_true", help="Full reindex (delete + add) instead of incremental")
    p_add.set_defaults(db=None)
    p_add.set_defaults(_run=cmd_add)

    # ask [--archetype X | --in <silo>] [--strict] <query...>  (default: unified llmli collection)
    p_ask = sub.add_parser("ask", help="Ask (unified llmli by default; or archetype / --in silo)")
    p_ask.add_argument("--archetype", "-a", help="Archetype id (e.g. tax, infra)")
    p_ask.add_argument("--in", dest="in_silo", metavar="SILO", help="Limit to one silo (slug or display name)")
    p_ask.add_argument("--unified", action="store_true", help="Search across all silos (compare/analyze across indexed content)")
    p_ask.add_argument("--strict", action="store_true", help="Never conclude absence from partial evidence; say unknown + sources when unsure")
    p_ask.add_argument("--model", "-m", help="Ollama model (default: LLMLIBRARIAN_MODEL or llama3.1:8b)")
    p_ask.add_argument("--n-results", type=int, default=12, help="Retrieval count")
    p_ask.add_argument("query", nargs="+", help="Question")
    p_ask.set_defaults(_run=cmd_ask)

    # ls
    p_ls = sub.add_parser("ls", help="List silos")
    p_ls.set_defaults(_run=cmd_ls)

    # inspect <silo> [--top N] [--filter pdf|docx|code]
    p_inspect = sub.add_parser("inspect", help="Show silo details and top files by chunk count (default: top 20)")
    p_inspect.add_argument("silo", help="Silo slug or display name")
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

    # rm <silo>
    p_rm = sub.add_parser("rm", help="Remove silo (registry + chunks)")
    p_rm.add_argument("silo", help="Silo slug or display name")
    p_rm.set_defaults(_run=cmd_rm)

    # capabilities
    p_capabilities = sub.add_parser("capabilities", help="Supported file types and document extractors (source of truth)")
    p_capabilities.set_defaults(_run=cmd_capabilities)

    # log [--last]
    p_log = sub.add_parser("log", help="Show last add failures")
    p_log.add_argument("--last", action="store_true", help="Show last add failures (default)")
    p_log.set_defaults(_run=cmd_log)

    args = parser.parse_args()
    return args._run(args)

if __name__ == "__main__":
    sys.exit(main())
