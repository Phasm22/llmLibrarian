"""Agent-facing CLI helpers: JSON envelopes, schema, version, confirm, lock errors.

Human text output stays the default in cli.py. This module is for machine-readable
modes and discoverability so automation does not scrape tables.
"""
from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
import warnings
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any, Iterable

# Exit codes used by llmli for structured failures.
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_BUSY = 3
EXIT_CONFIRM = 4

SCHEMA_VERSION = 1

# Metadata filters shared with MCP query_personal_knowledge / multi_query_knowledge.
META_FILTER_FLAGS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("--category", {"dest": "category", "help": "Exact frontmatter category match"}),
    ("--main-protein", {"dest": "main_protein", "help": "Exact frontmatter main_protein match"}),
    ("--course", {"dest": "course", "help": "Exact frontmatter course match"}),
    ("--era", {"dest": "era", "help": "Exact frontmatter era match"}),
    ("--tags", {"dest": "tags", "help": "Comma-separated tags; every tag must match"}),
    ("--min-servings", {"dest": "min_servings", "type": int, "help": "Inclusive min servings"}),
    ("--max-servings", {"dest": "max_servings", "type": int, "help": "Inclusive max servings"}),
    ("--max-total-time", {"dest": "max_total_time", "type": int, "help": "total_time (minutes) ≤ this"}),
    ("--section", {"dest": "section", "help": "Post-filter chunks to a document section"}),
    ("--doc-type", {"dest": "doc_type", "help": "Restrict by chunk doc_type (e.g. transcript, code)"}),
    ("--source", {"dest": "source", "help": "Rank chunks within one indexed file path"}),
)


def package_version_str() -> str:
    try:
        return package_version("llmlibrarian")
    except PackageNotFoundError:
        return "0.1.0"


def git_commit(repo_root: Path | None = None) -> str | None:
    root = repo_root or Path(__file__).resolve().parents[1]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        if out.returncode == 0:
            return (out.stdout or "").strip() or None
    except Exception:
        return None
    return None


def version_info(repo_root: Path | None = None, cli_file: Path | None = None) -> dict[str, Any]:
    """Install/version facts agents can use to detect a stale global llmli."""
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    cli_path = (cli_file or (root / "cli.py")).resolve()
    src_path = (root / "src").resolve()
    commit = git_commit(root)
    return {
        "ok": True,
        "name": "llmli",
        "version": package_version_str(),
        "git_commit": commit,
        "cli_path": str(cli_path),
        "src_path": str(src_path) if src_path.is_dir() else None,
        "python": sys.executable,
        "cwd": str(Path.cwd()),
        "db_env": os.environ.get("LLMLIBRARIAN_DB"),
        "chroma_host": os.environ.get("LLMLIBRARIAN_CHROMA_HOST"),
        "chroma_port": os.environ.get("LLMLIBRARIAN_CHROMA_PORT"),
    }


def format_version_line(info: dict[str, Any] | None = None) -> str:
    info = info or version_info()
    commit = info.get("git_commit") or "unknown"
    return (
        f"llmli {info.get('version')} (commit {commit})\n"
        f"cli: {info.get('cli_path')}\n"
        f"python: {info.get('python')}"
    )


def suggest_close_matches(value: str, choices: Iterable[str], n: int = 3, cutoff: float = 0.5) -> list[str]:
    return difflib.get_close_matches(value, list(choices), n=n, cutoff=cutoff)


def quiet_machine_noise() -> None:
    """Reduce HF/tqdm/deprecation chatter that would pollute --json stdout."""
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    warnings.filterwarnings("ignore", category=FutureWarning)
    try:
        from tqdm import tqdm as _tqdm

        _tqdm.__init__.__kwdefaults__ = {**(_tqdm.__init__.__kwdefaults__ or {}), "disable": True}
    except Exception:
        pass


def emit_json(payload: Any, *, stream=None) -> None:
    out = stream or sys.stdout
    print(json.dumps(payload, indent=2, default=str, ensure_ascii=False), file=out)


def ok_payload(data: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": True}
    if data:
        body.update(data)
    body.update(extra)
    return body


def error_payload(
    message: str,
    *,
    code: str = "error",
    retryable: bool = False,
    busy: bool = False,
    exit_code: int = EXIT_ERROR,
    **extra: Any,
) -> dict[str, Any]:
    err: dict[str, Any] = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "busy": busy,
        },
        "exit_code": exit_code,
    }
    err.update(extra)
    return err


def is_lock_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    try:
        from chroma_lock import ChromaLockTimeoutError

        return isinstance(exc, ChromaLockTimeoutError)
    except Exception:
        return False


def busy_payload(exc: BaseException, operation: str) -> dict[str, Any]:
    retry_after = 5
    raw = os.environ.get("LLMLIBRARIAN_CLI_RETRY_AFTER_SECONDS", "").strip()
    if raw:
        try:
            retry_after = max(1, int(float(raw)))
        except ValueError:
            pass
    return error_payload(
        f"ChromaDB is busy during {operation}: {exc}. Retry when the writer finishes.",
        code="busy",
        retryable=True,
        busy=True,
        exit_code=EXIT_BUSY,
        retry_after_seconds=retry_after,
        operation=operation,
    )


def require_yes(args: Any, *, action: str, detail: str) -> dict[str, Any] | None:
    """Return an error payload when a destructive action lacks --yes."""
    if getattr(args, "yes", False) or getattr(args, "dry_run", False):
        return None
    return error_payload(
        f"{action} requires --yes (or --dry-run to preview). {detail}",
        code="confirmation_required",
        exit_code=EXIT_CONFIRM,
        action=action,
    )


def meta_filter_kwargs(args: Any) -> dict[str, Any]:
    """Extract MCP-parity metadata filters from an argparse namespace."""
    out: dict[str, Any] = {}
    for key in (
        "category",
        "main_protein",
        "course",
        "era",
        "section",
        "doc_type",
        "source",
        "min_servings",
        "max_servings",
        "max_total_time",
    ):
        val = getattr(args, key, None)
        if val is not None and val != "":
            out[key] = val
    tags = getattr(args, "tags", None)
    if tags:
        out["tags"] = tags
    return out


def add_meta_filter_arguments(parser: Any) -> None:
    for flag, kwargs in META_FILTER_FLAGS:
        parser.add_argument(flag, **kwargs)


def command_schema() -> dict[str, Any]:
    """Self-describing surface for agents (stable contract; tested)."""
    commands = [
        {
            "name": "schema",
            "summary": "Emit this command/arg/output schema as JSON",
            "mcp_equivalent": None,
            "destructive": False,
            "outputs": {"default": "json"},
            "args": [],
        },
        {
            "name": "version",
            "summary": "Show package version, git commit, and install paths",
            "mcp_equivalent": None,
            "destructive": False,
            "outputs": {"default": "text", "json": "version_info object"},
            "args": [{"name": "--json", "type": "flag"}],
            "note": "Also available as global `llmli --version`",
        },
        {
            "name": "ls",
            "summary": "List silos (slug, display name, files, chunks, private scope)",
            "mcp_equivalent": "list_silos",
            "destructive": False,
            "outputs": {"default": "table", "json": "{ok, silos[]}"},
            "args": [{"name": "--json", "type": "flag"}],
        },
        {
            "name": "status",
            "summary": "Write-in-progress markers and Chroma lock holders",
            "mcp_equivalent": "mcp_runtime_status (partial)",
            "destructive": False,
            "outputs": {"default": "text", "json": "{ok, write_in_progress, lock}"},
            "args": [{"name": "--json", "type": "flag"}],
        },
        {
            "name": "query",
            "summary": "Retrieve chunks (no LLM) — MCP query_personal_knowledge parity",
            "mcp_equivalent": "query_personal_knowledge",
            "destructive": False,
            "outputs": {"default": "json", "json": "{ok, chunks[], ...}"},
            "args": [
                {"name": "query", "type": "string+", "required": True},
                {"name": "--in", "type": "silo", "help": "slug or display name; private silos need exact slug"},
                {"name": "--n-results", "type": "int", "default": 12},
                {"name": "--json", "type": "flag", "default": True},
                *[{"name": flag, "type": "meta"} for flag, _ in META_FILTER_FLAGS],
            ],
        },
        {
            "name": "multi-query",
            "summary": "Parallel retrieve + merge/dedup chunks",
            "mcp_equivalent": "multi_query_knowledge",
            "destructive": False,
            "outputs": {"default": "json"},
            "args": [
                {"name": "queries", "type": "string+", "required": True, "help": "Pass multiple --q or free args"},
                {"name": "--q", "type": "string", "repeatable": True},
                {"name": "--in", "type": "silo"},
                {"name": "--n-results", "type": "int", "default": 10},
                {"name": "--max-total-chunks", "type": "int", "default": 30},
                *[{"name": flag, "type": "meta"} for flag, _ in META_FILTER_FLAGS],
            ],
        },
        {
            "name": "ask",
            "summary": "Retrieve + local Ollama answer (human/CLI synthesis)",
            "mcp_equivalent": None,
            "destructive": False,
            "outputs": {"default": "text", "json": "{ok, answer, ...}"},
            "args": [
                {"name": "query", "type": "string+", "required": True},
                {"name": "--in", "type": "silo"},
                {"name": "--unified", "type": "flag"},
                {"name": "--strict", "type": "flag"},
                {"name": "--quiet", "type": "flag"},
                {"name": "--json", "type": "flag"},
            ],
        },
        {
            "name": "find",
            "summary": "Manifest-only path/date search",
            "mcp_equivalent": "find_files",
            "destructive": False,
            "outputs": {"default": "table", "json": "op_find_files result"},
            "args": [
                {"name": "--in", "type": "silo", "repeatable": True},
                {"name": "--name", "type": "glob"},
                {"name": "--date", "type": "YYYY-MM-DD|START:END"},
                {"name": "--field", "type": "name_date|mtime|either"},
                {"name": "--json", "type": "flag"},
            ],
        },
        {
            "name": "read",
            "summary": "Read one indexed file from the index (paged)",
            "mcp_equivalent": "read_document",
            "destructive": False,
            "outputs": {"default": "json"},
            "args": [
                {"name": "path", "type": "string", "required": True},
                {"name": "--in", "type": "silo", "help": "exact slug for private silos"},
                {"name": "--start-chunk", "type": "int", "default": 0},
                {"name": "--max-chars", "type": "int", "default": 6000},
            ],
        },
        {
            "name": "inspect",
            "summary": "Per-file chunk counts for a silo",
            "mcp_equivalent": "inspect_silo",
            "destructive": False,
            "outputs": {"default": "text", "json": "op_inspect_silo result"},
            "args": [
                {"name": "silo", "type": "silo", "required": True},
                {"name": "--top", "type": "int", "default": 20},
                {"name": "--json", "type": "flag"},
            ],
        },
        {
            "name": "verify",
            "summary": "Report indexed files that match silo exclude patterns",
            "mcp_equivalent": None,
            "destructive": "with --purge",
            "outputs": {"default": "text", "json": "op_verify_silo result"},
            "args": [
                {"name": "silo", "type": "silo", "required": True},
                {"name": "--purge", "type": "flag", "requires": "--yes"},
                {"name": "--yes", "type": "flag"},
                {"name": "--dry-run", "type": "flag"},
                {"name": "--json", "type": "flag"},
            ],
        },
        {
            "name": "add",
            "summary": "Index a folder or file into a silo",
            "mcp_equivalent": "add_silo",
            "destructive": "with --full",
            "outputs": {"default": "text", "json": "{ok, ...}"},
            "args": [
                {"name": "path", "type": "path", "required": True},
                {"name": "--full", "type": "flag", "requires": "--yes"},
                {"name": "--yes", "type": "flag"},
                {"name": "--dry-run", "type": "flag"},
                {"name": "--exclude", "type": "pattern", "repeatable": True},
                {"name": "--json", "type": "flag"},
            ],
        },
        {
            "name": "rm",
            "summary": "Remove a silo (registry + chunks)",
            "mcp_equivalent": None,
            "destructive": True,
            "outputs": {"default": "text", "json": "op_remove_silo result"},
            "args": [
                {"name": "silo", "type": "silo+", "required": True},
                {"name": "--yes", "type": "flag", "required_for_apply": True},
                {"name": "--dry-run", "type": "flag"},
                {"name": "--json", "type": "flag"},
            ],
        },
        {
            "name": "repair",
            "summary": "Wipe and fully re-index a silo",
            "mcp_equivalent": "repair_silo",
            "destructive": True,
            "outputs": {"default": "text", "json": "op_repair_silo result"},
            "args": [
                {"name": "silo", "type": "silo+", "required": True},
                {"name": "--yes", "type": "flag", "required_for_apply": True},
                {"name": "--dry-run", "type": "flag"},
                {"name": "--json", "type": "flag"},
            ],
        },
        {
            "name": "private",
            "summary": "Mark or clear a silo as local-only (excluded from unscoped retrieval)",
            "mcp_equivalent": "set_silo_privacy",
            "destructive": "with --off (widens exposure)",
            "outputs": {"default": "text", "json": "{ok, slug, private}"},
            "args": [
                {"name": "silo", "type": "silo", "required": True},
                {"name": "--off", "type": "flag"},
                {"name": "--yes", "type": "flag", "required_for_apply": True},
                {"name": "--dry-run", "type": "flag"},
                {"name": "--json", "type": "flag"},
            ],
        },
        {
            "name": "capabilities",
            "summary": "Supported file types / extractors (content ingest)",
            "mcp_equivalent": "capabilities",
            "destructive": False,
            "outputs": {"default": "text", "json": "{ok, text}"},
            "args": [{"name": "--json", "type": "flag"}],
        },
        {
            "name": "rehydrate",
            "summary": "Rebuild silos from registry into current DB path",
            "mcp_equivalent": None,
            "destructive": True,
            "outputs": {"default": "text", "json": "op_rehydrate_registry result"},
            "args": [
                {"name": "silos", "type": "silo*"},
                {"name": "--dry-run", "type": "flag"},
                {"name": "--json", "type": "flag"},
            ],
        },
        {
            "name": "repair-ladder",
            "summary": "Read-only Chroma diagnostics + L1→L3 guidance",
            "mcp_equivalent": "health (partial)",
            "destructive": False,
            "outputs": {"default": "text", "json": "diagnostics object"},
            "args": [{"name": "--json", "type": "flag"}],
        },
        {
            "name": "log",
            "summary": "Show last add/index failures",
            "mcp_equivalent": None,
            "destructive": False,
            "outputs": {"default": "text", "json": "{ok, failures[]}"},
            "args": [{"name": "--json", "type": "flag"}],
        },
    ]
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "program": "llmli",
        "global_flags": [
            {"name": "--db", "type": "path"},
            {"name": "--config", "type": "path"},
            {"name": "--json", "type": "flag", "help": "Machine-readable JSON on stdout; errors are objects; exit ≠ 0 on failure"},
            {"name": "--no-color", "type": "flag"},
            {"name": "--version / -V", "type": "flag"},
        ],
        "conventions": {
            "silo_addressing": "Most commands accept slug, display name, or slug prefix. Private silos open only by exact slug on query/read/find (same rule as MCP).",
            "confirmation": "Destructive apply paths require --yes. Prefer --dry-run first.",
            "busy": "ChromaLockTimeoutError → exit 3, error.code=busy, retryable=true",
            "json_stdout": "With --json, only the JSON document is written to stdout; progress/warnings go to stderr or are suppressed.",
        },
        "commands": commands,
        "recipes": [
            {"goal": "list silos", "cmd": "llmli --json ls"},
            {"goal": "query with filters", "cmd": "llmli query --in recipes-xxxx --main-protein chicken --max-total-time 45 \"weeknight dinner\""},
            {"goal": "read a file", "cmd": "llmli read README.md --in my-silo"},
            {"goal": "verify excludes", "cmd": "llmli --json verify my-silo"},
            {"goal": "safe reindex preview", "cmd": "llmli add /path --full --dry-run"},
            {"goal": "check install", "cmd": "llmli --version"},
        ],
    }


def resolve_indexed_file(
    db_path: str | Path,
    file: str,
    silo: str | None = None,
    *,
    suffixes: set[str] | None = None,
) -> tuple[str | None, str | None, list[str], str | None]:
    """Map a filename/path to an indexed file. Private silos require exact slug."""
    from file_registry import read_visible_manifest
    from state import private_silo_slugs

    wanted = (file or "").strip()
    if not wanted:
        return None, None, [], "file is required"
    target_slug = (silo or "").strip()
    manifest = read_visible_manifest(db_path, silo=target_slug or None) or {}
    silos = manifest.get("silos") or {}
    private = set(private_silo_slugs(db_path))

    matches: list[tuple[str, str]] = []
    roots: dict[str, str] = {}
    for slug, entry in silos.items():
        if target_slug and slug != target_slug and (entry or {}).get("path") != target_slug:
            continue
        if slug in private and slug != target_slug:
            continue
        roots[slug] = str((entry or {}).get("path") or "")
        for path in ((entry or {}).get("files") or {}):
            if suffixes is not None and Path(path).suffix.lower() not in suffixes:
                continue
            if path == wanted or Path(path).name == wanted or Path(path).name.lower() == wanted.lower():
                matches.append((path, slug))
    if not matches:
        return None, None, [], f"no indexed file matches {file!r}" + (f" in silo {silo!r}" if silo else "")
    if len(matches) > 1 and target_slug:
        at_root = [
            (path, slug)
            for path, slug in matches
            if roots.get(slug) and str(Path(path).parent) == roots[slug]
        ]
        if len(at_root) == 1:
            return at_root[0][0], at_root[0][1], [], None
    if len(matches) > 1:
        return None, None, sorted(path for path, _slug in matches), "multiple indexed files match; pass a full path"
    return matches[0][0], matches[0][1], [], None


def run_multi_query(
    queries: list[str],
    *,
    db_path: str | Path,
    silo: str | None = None,
    n_results: int = 10,
    max_total_chunks: int = 30,
    section: str | None = None,
    doc_type: str | None = None,
    source: str | None = None,
    category: str | None = None,
    main_protein: str | None = None,
    min_servings: int | None = None,
    max_servings: int | None = None,
    max_total_time: int | None = None,
    tags: str | list[str] | None = None,
    course: str | None = None,
    era: str | None = None,
) -> dict[str, Any]:
    """CLI/MCP-shared multi-query merge without importing mcp_server."""
    from query.core import annotate_write_state, run_retrieve

    meta = {
        "category": category,
        "main_protein": main_protein,
        "min_servings": min_servings,
        "max_servings": max_servings,
        "max_total_time": max_total_time,
        "tags": tags,
        "course": course,
        "era": era,
        "source": source,
    }
    seen: set[str] = set()
    all_chunks: list[dict] = []
    errors: list[str] = []
    busy = False
    write_states: list[dict] = []
    deterministic_intent: str | None = None
    for q in queries:
        try:
            res = run_retrieve(
                query=q,
                silo=silo,
                n_results=n_results,
                section=section,
                doc_type=doc_type,
                db_path=db_path,
                **{k: v for k, v in meta.items() if v is not None},
            )
            if res.get("error"):
                errors.append(f"{q!r}: {res['error']}")
                continue
            if res.get("write_in_progress"):
                write_states.append(res["write_in_progress"])
            if res.get("deterministic_intent") and not deterministic_intent:
                deterministic_intent = res["deterministic_intent"]
            for chunk in res.get("chunks", []):
                key = (chunk.get("text") or "")[:200]
                if key and key not in seen:
                    seen.add(key)
                    tagged = dict(chunk)
                    tagged["query"] = q
                    all_chunks.append(tagged)
        except Exception as e:
            if is_lock_timeout(e):
                busy = True
            errors.append(f"{q!r}: {type(e).__name__}: {e}")
    all_chunks.sort(key=lambda c: c.get("score") or 0, reverse=True)
    truncated = len(all_chunks) > max_total_chunks
    if truncated:
        all_chunks = all_chunks[:max_total_chunks]
    from ingest_journal import merge_write_states

    merged_write = merge_write_states(write_states)
    response: dict[str, Any] = {
        "ok": not (busy and not all_chunks),
        "queries": queries,
        "total_chunks": len(all_chunks),
        "truncated": truncated,
        "chunks": all_chunks,
    }
    if errors:
        response["errors"] = errors
    if deterministic_intent:
        response["deterministic_intent"] = deterministic_intent
    if busy and not all_chunks:
        response.update(busy_payload(TimeoutError("lock busy"), "multi-query"))
    if merged_write:
        response = annotate_write_state(response, merged_write, None)
    return response


def status_payload(db_path: str | Path) -> dict[str, Any]:
    from chroma_lock import chroma_lock_snapshot
    from ingest_journal import active_writes

    writes = active_writes(str(db_path))
    lock = chroma_lock_snapshot(db_path)
    return ok_payload(
        {
            "db_path": str(db_path),
            "write_in_progress": writes,
            "lock": lock,
            "busy": bool(lock.get("holder_pids")),
        }
    )
