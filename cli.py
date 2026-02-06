#!/usr/bin/env python3
"""
llmLibrarian (llmli) CLI: add, ask, ls, index, rm, log.
Deterministic, locally-hosted context engine for high-stakes personal data.
Run from project root; uses archetypes.yaml and ./my_brain_db by default.
"""
import argparse
import os
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

def cmd_add(args: argparse.Namespace) -> int:
    """Index a folder into the unified llmli collection; silo name = basename(path)."""
    from indexer import run_add, DB_PATH
    path = Path(args.path).resolve()
    db = args.db or DB_PATH
    if not path.is_dir():
        print(f"Error: not a directory: {path}", file=sys.stderr)
        return 1
    try:
        run_add(path, db_path=db, no_color=args.no_color)
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

def cmd_ask(args: argparse.Namespace) -> int:
    """Query an archetype's collection via Ollama."""
    from query import run_ask
    config = _config_path(args)
    out = run_ask(
        args.archetype,
        " ".join(args.query),
        config_path=config,
        n_results=getattr(args, "n_results", 12),
        model=getattr(args, "model", None) or os.environ.get("LLMLIBRARIAN_MODEL", "llama3.1:8b"),
        no_color=args.no_color,
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
        path = s.get("path", "?")
        files = s.get("files_indexed", 0)
        chunks = s.get("chunks_count", 0)
        updated = s.get("updated", "")[:19]
        print(f"  {slug}\t{path}\t{files} files, {chunks} chunks\t{updated}")
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
        )
        return 0
    except KeyError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

def cmd_rm(args: argparse.Namespace) -> int:
    """Remove a silo from the registry and delete its chunks from the llmli collection."""
    silo = getattr(args, "silo", None)
    if not silo:
        print("Error: rm requires silo name as positional argument. Example: llmli rm tax", file=sys.stderr)
        return 1
    from state import remove_silo
    from indexer import LLMLI_COLLECTION, DB_PATH
    import chromadb
    from chromadb.config import Settings
    db = _db_path(args)
    if not remove_silo(db, silo):
        print(f"Error: silo not found: {silo}", file=sys.stderr)
        return 1
    try:
        client = chromadb.PersistentClient(path=str(db), settings=Settings(anonymized_telemetry=False))
        coll = client.get_or_create_collection(name=LLMLI_COLLECTION)
        coll.delete(where={"silo": silo})
    except Exception as e:
        print(f"Warning: could not delete chunks from DB: {e}", file=sys.stderr)
    print(f"Removed silo: {silo}")
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

def main() -> int:
    parser = argparse.ArgumentParser(prog="llmli", description="llmLibrarian CLI: add, ask, ls, index, rm, log")
    parser.add_argument("--db", default=os.environ.get("LLMLIBRARIAN_DB", "./my_brain_db"), help="DB path")
    parser.add_argument("--config", help="Path to archetypes.yaml")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color")
    sub = parser.add_subparsers(dest="command", required=True)

    # add <path>
    p_add = sub.add_parser("add", help="Index folder into llmli (silo = basename)")
    p_add.add_argument("path", help="Folder path to index")
    p_add.set_defaults(db=None)
    p_add.set_defaults(_run=cmd_add)

    # ask [--archetype X] <query...>
    p_ask = sub.add_parser("ask", help="Ask using an archetype's collection")
    p_ask.add_argument("--archetype", "-a", required=True, help="Archetype id (e.g. tax, infra)")
    p_ask.add_argument("--model", "-m", help="Ollama model (default: LLMLIBRARIAN_MODEL or llama3.1:8b)")
    p_ask.add_argument("--n-results", type=int, default=12, help="Retrieval count")
    p_ask.add_argument("query", nargs="+", help="Question")
    p_ask.set_defaults(_run=cmd_ask)

    # ls
    p_ls = sub.add_parser("ls", help="List silos")
    p_ls.set_defaults(_run=cmd_ls)

    # index --archetype X
    p_index = sub.add_parser("index", help="Rebuild archetype collection from config folders")
    p_index.add_argument("--archetype", "-a", required=True, help="Archetype id")
    p_index.add_argument("--log", help="Log file path (or set LLMLIBRARIAN_LOG=1)")
    p_index.add_argument("--mode", choices=["fast", "normal", "deep"], default="normal")
    p_index.set_defaults(_run=cmd_index)

    # rm <silo>
    p_rm = sub.add_parser("rm", help="Remove silo (registry + chunks)")
    p_rm.add_argument("silo", help="Silo name (slug)")
    p_rm.set_defaults(_run=cmd_rm)

    # log [--last]
    p_log = sub.add_parser("log", help="Show last add failures")
    p_log.set_defaults(_run=cmd_log)

    args = parser.parse_args()
    return args._run(args)

if __name__ == "__main__":
    sys.exit(main())
