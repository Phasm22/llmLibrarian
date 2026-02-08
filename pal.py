#!/usr/bin/env python3
"""
pal — Agent CLI that orchestrates tools (e.g. llmli). Control-plane: route, registry.
Not a replacement for llmli; pal add/ask/ls/log delegate to llmli. Use pal tool llmli ... for passthrough.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PAL_HOME = Path(os.environ.get("PAL_HOME", os.path.expanduser("~/.pal")))
REGISTRY_PATH = PAL_HOME / "registry.json"


def _ensure_pal_home() -> None:
    PAL_HOME.mkdir(parents=True, exist_ok=True)


def _read_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"sources": [], "tools": {"llmli": {"last_ok": None, "last_failures": None}}}
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sources": [], "tools": {"llmli": {"last_ok": None, "last_failures": None}}}


def _write_registry(data: dict) -> None:
    _ensure_pal_home()
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _run_llmli(args: list[str]) -> int:
    """Run llmli as subprocess; return exit code."""
    root = Path(__file__).resolve().parent
    env = os.environ.copy()
    venv_llmli = root / ".venv" / "bin" / "llmli"
    if venv_llmli.exists():
        cmd = [str(venv_llmli)] + args
    else:
        cmd = [sys.executable, str(root / "cli.py")] + args
    r = subprocess.run(cmd, env=env)
    return r.returncode


def cmd_add(args: argparse.Namespace) -> int:
    """Delegate to llmli add <path>; optionally record in pal registry. Cloud-sync paths blocked unless --allow-cloud."""
    path = Path(args.path).resolve()
    if not path.is_dir():
        print(f"Error: not a directory: {path}", file=sys.stderr)
        return 1
    llmli_args = ["add"]
    if getattr(args, "allow_cloud", False):
        llmli_args.append("--allow-cloud")
    llmli_args.append(str(path))
    code = _run_llmli(llmli_args)
    if code == 0:
        reg = _read_registry()
        sources = reg.get("sources", [])
        entry = {"path": str(path), "tool": "llmli", "name": path.name}
        if not any(s.get("path") == str(path) for s in sources):
            sources.append(entry)
            reg["sources"] = sources
            _write_registry(reg)
    return code


def cmd_ask(args: argparse.Namespace) -> int:
    """Delegate to llmli ask (unified by default). --in <silo> for scoped ask; --strict for conservative answers."""
    llmli_args = ["ask"]
    if getattr(args, "unified", False):
        llmli_args.append("--unified")
    if getattr(args, "in_silo", None) and not getattr(args, "unified", False):
        llmli_args.extend(["--in", args.in_silo])
    if getattr(args, "strict", False):
        llmli_args.append("--strict")
    llmli_args.extend(args.query)
    return _run_llmli(llmli_args)


def cmd_ls(args: argparse.Namespace) -> int:
    """Aggregate view: delegate to llmli ls."""
    return _run_llmli(["ls"])


def cmd_log(args: argparse.Namespace) -> int:
    """Unified log view: delegate to llmli log --last."""
    return _run_llmli(["log", "--last"])


def cmd_capabilities(args: argparse.Namespace) -> int:
    """Delegate to llmli capabilities (supported file types and extractors)."""
    return _run_llmli(["capabilities"])


def cmd_inspect(args: argparse.Namespace) -> int:
    """Show silo details and top files by chunk count (llmli inspect)."""
    silo = getattr(args, "silo", None)
    if not silo:
        print("Error: inspect requires silo name. Example: pal inspect stuff", file=sys.stderr)
        return 1
    llmli_args = ["inspect", silo]
    top = getattr(args, "top", None)
    if top is not None:
        llmli_args.extend(["--top", str(top)])
    filt = getattr(args, "filter", None)
    if filt:
        llmli_args.extend(["--filter", filt])
    return _run_llmli(llmli_args)


def cmd_pull(args: argparse.Namespace) -> int:
    """Refresh all registered silos (incremental by default)."""
    reg = _read_registry()
    sources = reg.get("sources", [])
    if not sources:
        print("No registered silos. Use: pal add <path>", file=sys.stderr)
        return 1
    failures = 0
    for src in sources:
        path = src.get("path")
        if not path:
            continue
        name = src.get("name") or Path(path).name
        print(f"Pulling silo: {name} ({path})")
        llmli_args = ["add"]
        if getattr(args, "full", False):
            llmli_args.append("--full")
        if getattr(args, "allow_cloud", False):
            llmli_args.append("--allow-cloud")
        if getattr(args, "follow_symlinks", False):
            llmli_args.append("--follow-symlinks")
        llmli_args.append(path)
        code = _run_llmli(llmli_args)
        if code != 0:
            failures += 1
    return 0 if failures == 0 else 1


def cmd_tool(args: argparse.Namespace) -> int:
    """Passthrough: pal tool <name> <args...> -> run underlying tool."""
    name = getattr(args, "tool_name", None)
    rest = list(getattr(args, "tool_args", []) or [])
    while rest and rest[0] == "--":
        rest.pop(0)
    if name == "llmli":
        return _run_llmli(rest)
    print(f"Error: unknown tool '{name}'. Use: pal tool llmli <args...>", file=sys.stderr)
    return 1


KNOWN_COMMANDS = frozenset({"add", "ask", "ls", "inspect", "log", "capabilities", "pull", "tool"})


def main() -> int:
    # Default to ask when first arg is not a known subcommand: pal "who is X" or pal who is X → pal ask ...
    if len(sys.argv) >= 2 and sys.argv[1] not in KNOWN_COMMANDS and not sys.argv[1].startswith("-"):
        sys.argv.insert(1, "ask")

    # Natural "ask in <silo> ..." → treat as --in <silo> so silo scope isn't eaten as query words
    if len(sys.argv) >= 4 and sys.argv[1] == "ask" and sys.argv[2].lower() == "in":
        third = sys.argv[3]
        if not third.startswith("-") and third:
            sys.argv[2:4] = ["--in", third]

    parser = argparse.ArgumentParser(
        prog="pal",
        description="Agent CLI: add, ask, ls, log (delegate to llmli); pal <question> runs ask.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Index folder (llmli add); register in ~/.pal; cloud paths blocked unless --allow-cloud")
    p_add.add_argument("path", help="Folder path to index")
    p_add.add_argument("--allow-cloud", action="store_true", help="Allow OneDrive/iCloud/Dropbox/Google Drive")
    p_add.set_defaults(_run=cmd_add)

    p_ask = sub.add_parser("ask", help="Ask (llmli ask; use --unified to search all silos for comparison)")
    p_ask.add_argument("--in", dest="in_silo", metavar="SILO", help="Limit to one silo")
    p_ask.add_argument("--unified", action="store_true", help="Search across all silos (for compare/thematic questions)")
    p_ask.add_argument("--strict", action="store_true", help="Conservative: never conclude absence from partial evidence; say 'unknown' + sources when unsure")
    p_ask.add_argument("query", nargs="+", help="Question")
    p_ask.set_defaults(_run=cmd_ask)

    p_ls = sub.add_parser("ls", help="List silos (llmli ls)")
    p_ls.set_defaults(_run=cmd_ls)

    p_inspect = sub.add_parser("inspect", help="Silo details and top files by chunk count (llmli inspect)")
    p_inspect.add_argument("silo", help="Silo slug or display name")
    p_inspect.add_argument("--top", type=int, default=None, help="Show top N files (default: 20; pass to llmli)")
    p_inspect.add_argument("--filter", choices=["pdf", "docx", "code"], help="Show only pdf, docx, or code")
    p_inspect.set_defaults(_run=cmd_inspect)

    p_log = sub.add_parser("log", help="Last failures (llmli log --last)")
    p_log.set_defaults(_run=cmd_log)

    p_capabilities = sub.add_parser("capabilities", help="Supported file types and extractors (llmli capabilities)")
    p_capabilities.set_defaults(_run=cmd_capabilities)

    p_pull = sub.add_parser("pull", help="Refresh all registered silos (incremental by default)")
    p_pull.add_argument("--full", action="store_true", help="Full reindex (delete + add) instead of incremental")
    p_pull.add_argument("--allow-cloud", action="store_true", help="Allow OneDrive/iCloud/Dropbox/Google Drive")
    p_pull.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks inside folders")
    p_pull.set_defaults(_run=cmd_pull)

    p_tool = sub.add_parser("tool", help="Passthrough to tool: pal tool llmli <args...>")
    p_tool.add_argument("tool_name", help="Tool name (e.g. llmli)")
    p_tool.add_argument("tool_args", nargs=argparse.REMAINDER, default=[], help="Arguments for the tool")
    p_tool.set_defaults(_run=cmd_tool)

    args = parser.parse_args()
    return args._run(args)


if __name__ == "__main__":
    sys.exit(main())
