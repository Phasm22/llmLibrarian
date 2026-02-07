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
    # Prefer venv llmli if present
    venv_llmli = root / ".venv" / "bin" / "llmli"
    if venv_llmli.exists():
        cmd = [str(venv_llmli)] + args
    else:
        cmd = [sys.executable, str(root / "cli.py")] + args
    r = subprocess.run(cmd, env=env)
    return r.returncode


def cmd_add(args: argparse.Namespace) -> int:
    """Delegate to llmli add <path>; optionally record in pal registry."""
    path = Path(args.path).resolve()
    if not path.is_dir():
        print(f"Error: not a directory: {path}", file=sys.stderr)
        return 1
    code = _run_llmli(["add", str(path)])
    if code == 0:
        reg = _read_registry()
        # Record as source owned by llmli (idempotent by path)
        sources = reg.get("sources", [])
        entry = {"path": str(path), "tool": "llmli", "name": path.name}
        if not any(s.get("path") == str(path) for s in sources):
            sources.append(entry)
            reg["sources"] = sources
            _write_registry(reg)
    return code


def cmd_ask(args: argparse.Namespace) -> int:
    """Delegate to llmli ask (unified by default). --in <silo> for scoped ask."""
    llmli_args = ["ask"]
    if getattr(args, "in_silo", None):
        llmli_args.extend(["--in", args.in_silo])
    llmli_args.extend(args.query)
    return _run_llmli(llmli_args)


def cmd_ls(args: argparse.Namespace) -> int:
    """Aggregate view: delegate to llmli ls."""
    return _run_llmli(["ls"])


def cmd_log(args: argparse.Namespace) -> int:
    """Unified log view: delegate to llmli log --last."""
    return _run_llmli(["log", "--last"])


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


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pal",
        description="Agent CLI: add, ask, ls, log (delegate to llmli); pal tool <name> <args> for passthrough.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Index folder (llmli add); register in ~/.pal")
    p_add.add_argument("path", help="Folder path to index")
    p_add.set_defaults(_run=cmd_add)

    p_ask = sub.add_parser("ask", help="Ask (llmli ask, unified by default)")
    p_ask.add_argument("--in", dest="in_silo", metavar="SILO", help="Limit to silo")
    p_ask.add_argument("query", nargs="+", help="Question")
    p_ask.set_defaults(_run=cmd_ask)

    p_ls = sub.add_parser("ls", help="List silos (llmli ls)")
    p_ls.set_defaults(_run=cmd_ls)

    p_log = sub.add_parser("log", help="Last failures (llmli log --last)")
    p_log.set_defaults(_run=cmd_log)

    p_tool = sub.add_parser("tool", help="Passthrough to tool: pal tool llmli <args...>")
    p_tool.add_argument("tool_name", help="Tool name (e.g. llmli)")
    p_tool.add_argument("tool_args", nargs=argparse.REMAINDER, default=[], help="Arguments for the tool")
    p_tool.set_defaults(_run=cmd_tool)

    args = parser.parse_args()
    return args._run(args)


if __name__ == "__main__":
    sys.exit(main())
