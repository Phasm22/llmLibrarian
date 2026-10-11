#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""
llmLibrarian (llmli) CLI: add, ask, ls, query, read, index, rm, log, …
Deterministic, locally-hosted context engine for high-stakes personal data.
Run from project root; uses archetypes.yaml and ./my_brain_db by default.

Agents: prefer `llmli --json …`, `llmli schema`, and `docs/cli-for-agents.md`.
Human text output remains the default unless `--json` is set (query/read/schema
default to JSON).
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent

_EPILOG = """\
examples (agents — always prefer --json):
  llmli --version
  llmli schema
  llmli --json ls
  llmli status --json
  llmli query --in recipes-xxxx --main-protein chicken "weeknight dinner"
  llmli multi-query --q "sourdough" --q "starter feeding" --in recipes-xxxx
  llmli read README.md --in my-silo
  llmli find --name "*.pdf" --date 2024-01-01:2024-12-31 --json
  llmli verify my-silo --json
  llmli rm my-silo --dry-run
  llmli repair my-silo --yes

Private silos are excluded from unscoped query/find; pass the exact slug via --in.
Destructive apply paths require --yes (use --dry-run to preview).
"""


class _LlmliArgumentParser(argparse.ArgumentParser):
    """ArgumentParser with 'did you mean' hints for bad subcommands."""

    def error(self, message: str) -> None:  # type: ignore[override]
        from cli_agent import suggest_close_matches

        hint = ""
        # argparse messages look like: invalid choice: 'verfy' (choose from ...)
        m = re.search(r"invalid choice: '([^']+)'", message)
        if m and hasattr(self, "_llmli_subcommands"):
            suggestions = suggest_close_matches(m.group(1), self._llmli_subcommands)
            if suggestions:
                hint = f"\nDid you mean: {', '.join(suggestions)}?"
        self.print_usage(sys.stderr)
        self.exit(2, f"error: {message}{hint}\n")


class _VersionAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):  # noqa: ANN001
        from cli_agent import emit_json, format_version_line, version_info

        info = version_info(repo_root=_ROOT, cli_file=Path(__file__))
        if getattr(namespace, "json", False) or "--json" in sys.argv:
            emit_json(info)
        else:
            print(format_version_line(info))
        parser.exit(0)


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


def _wants_json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _emit_result(args: argparse.Namespace, payload: dict[str, Any], *, human: str | None = None) -> int:
    from cli_agent import emit_json

    if _wants_json(args):
        emit_json(payload)
        if payload.get("ok") is False:
            return int(payload.get("exit_code") or 1)
        return 0
    if human is not None:
        print(human, end="" if human.endswith("\n") else "\n")
    if payload.get("ok") is False:
        msg = (payload.get("error") or {}).get("message") or "error"
        print(f"Error: {msg}", file=sys.stderr)
        return int(payload.get("exit_code") or 1)
    return 0


def _handle_busy(args: argparse.Namespace, exc: BaseException, operation: str) -> int:
    from cli_agent import busy_payload, emit_json, is_lock_timeout

    if not is_lock_timeout(exc):
        return -1
    payload = busy_payload(exc, operation)
    if _wants_json(args):
        emit_json(payload)
    else:
        print(f"Error: {payload['error']['message']}", file=sys.stderr)
        print("Hint: llmli status --json  # see write-in-progress / lock holders", file=sys.stderr)
    return int(payload.get("exit_code") or 3)


def cmd_add(args: argparse.Namespace) -> int:
    """Index a folder into the unified llmli collection; silo name = basename(path)."""
    from cli_agent import error_payload, ok_payload, require_yes
    from ingest import CloudSyncPathError
    from orchestration.ingest import IngestRequest, run_ingest

    path = Path(args.path).resolve()
    db = _db_path(args)
    if not path.is_dir() and not path.is_file():
        return _emit_result(args, error_payload(f"not a file or directory: {path}", code="not_found"))
    full = bool(getattr(args, "full", False))
    if full:
        blocked = require_yes(
            args,
            action="add --full",
            detail="Full reindex deletes existing chunks for the silo before rewriting them.",
        )
        if blocked:
            return _emit_result(args, blocked)
    if getattr(args, "dry_run", False):
        excludes = getattr(args, "exclude_patterns", None) or []
        plan = ok_payload(
            {
                "dry_run": True,
                "would": "full_reindex" if full else "incremental_ingest",
                "path": str(path),
                "db_path": str(db),
                "exclude_patterns": excludes,
            }
        )
        human = f"Would {'full reindex' if full else 'incrementally index'} {path} into {db}"
        if excludes:
            human += f" (excludes: {excludes})"
        return _emit_result(args, plan, human=human)
    try:
        result = run_ingest(
            IngestRequest(
                path=path,
                db_path=db,
                no_color=args.no_color or _wants_json(args),
                allow_cloud=getattr(args, "allow_cloud", False),
                follow_symlinks=getattr(args, "follow_symlinks", False),
                incremental=not full,
                forced_silo_slug=getattr(args, "silo", None),
                display_name=getattr(args, "display_name", None),
                exclude_patterns=getattr(args, "exclude_patterns", None),
                image_vision_enabled=getattr(args, "image_vision", None),
                workers=getattr(args, "workers", None),
                embedding_workers=getattr(args, "embedding_workers", None),
            )
        )
        if _wants_json(args):
            return _emit_result(
                args,
                ok_payload(
                    {
                        "files_indexed": getattr(result, "files_indexed", None),
                        "failures": getattr(result, "failures", None),
                        "silo_slug": getattr(result, "silo_slug", None),
                        "path": str(path),
                    }
                ),
            )
        return 0
    except CloudSyncPathError as e:
        return _emit_result(args, error_payload(str(e), code="cloud_path"))
    except Exception as e:
        busy_rc = _handle_busy(args, e, "add")
        if busy_rc >= 0:
            return busy_rc
        return _emit_result(args, error_payload(str(e)))

def cmd_ask(args: argparse.Namespace) -> int:
    """Query an archetype's collection or unified llmli collection (default) via Ollama."""
    from cli_agent import error_payload, ok_payload
    from query.core import run_ask, QueryPolicyError
    from state import resolve_silo_to_slug, resolve_silo_prefix

    config = _config_path(args)
    archetype = getattr(args, "archetype", None)
    in_silo = getattr(args, "in_silo", None)
    if archetype and in_silo:
        return _emit_result(
            args,
            error_payload("use either --archetype or --in, not both", code="usage", exit_code=2),
        )
    unified = getattr(args, "unified", False)
    if in_silo and not unified:
        db = _db_path(args)
        silo_slug = resolve_silo_to_slug(db, in_silo) or resolve_silo_prefix(db, in_silo) or in_silo
        archetype = None
    else:
        silo_slug = None
        if not archetype:
            archetype = None
    query = _sanitize_query(args.query)
    if query is None:
        return _emit_result(
            args,
            error_payload("empty or invalid query (or too long)", code="usage", exit_code=2),
        )
    try:
        out = run_ask(
            archetype,
            query,
            config_path=config,
            n_results=getattr(args, "n_results", 12),
            model=getattr(args, "model", None) or os.environ.get("LLMLIBRARIAN_MODEL", "llama3.2:latest"),
            no_color=args.no_color or getattr(args, "quiet", False) or _wants_json(args),
            silo=silo_slug,
            db_path=_db_path(args),
            strict=getattr(args, "strict", False),
            quiet=getattr(args, "quiet", False) or _wants_json(args),
            explain=getattr(args, "explain", False) and not _wants_json(args),
            force=getattr(args, "force", False),
            explicit_unified=bool(getattr(args, "unified", False)),
        )
    except QueryPolicyError as e:
        return _emit_result(
            args,
            error_payload(str(e), code="policy", exit_code=int(getattr(e, "exit_code", 2) or 2)),
        )
    except Exception as e:
        busy_rc = _handle_busy(args, e, "ask")
        if busy_rc >= 0:
            return busy_rc
        return _emit_result(args, error_payload(str(e)))
    if _wants_json(args):
        return _emit_result(args, ok_payload({"answer": out, "silo": silo_slug, "query": query}))
    print(out)
    return 0

def cmd_ls(args: argparse.Namespace) -> int:
    """List silos in the llmli registry."""
    from cli_agent import ok_payload
    from state import list_silos
    from style import dim, link_style

    db = _db_path(args)
    silos = list_silos(db)
    if _wants_json(args):
        return _emit_result(
            args,
            ok_payload(
                {
                    "db_path": str(db),
                    "silo_count": len(silos),
                    "silos": [
                        {
                            "slug": s.get("slug"),
                            "display_name": s.get("display_name", s.get("slug")),
                            "path": s.get("path"),
                            "files_indexed": int(s.get("files_indexed", 0) or 0),
                            "chunks_count": int(s.get("chunks_count", 0) or 0),
                            "updated": s.get("updated"),
                            "private": bool(s.get("private")),
                            "host": s.get("host"),
                        }
                        for s in silos
                    ],
                }
            ),
        )
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
        private = bool(s.get("private"))
        rows.append((display, slug, files, chunks, updated, path, private))

    name_w = min(max(len(r[0]) for r in rows), 24)
    slug_w = min(max(len(r[1]) for r in rows), 20)
    path_w = 56
    header = f"{'Name':<{name_w}}  {'Slug':<{slug_w}}  {'Files':>5}  {'Chunks':>7}  {'Updated':<19}  {'Scope':<7}  Path"
    print(header)
    print("-" * len(header))
    for display, slug, files, chunks, updated, path, private in rows:
        display = _truncate_mid(display, name_w)
        slug = _truncate_mid(slug, slug_w)
        short_path = _truncate_tail(path, path_w)
        url = f"file://{path}" if path and path != "?" else ""
        path_cell = link_style(args.no_color, url, short_path)
        scope = "private" if private else "-"
        print(
            f"{display:<{name_w}}  {slug:<{slug_w}}  {files:>5}  {chunks:>7}  "
            f"{updated:<19}  {scope:<7}  {path_cell}"
        )
    n_private = sum(1 for r in rows if r[6])
    if n_private:
        print()
        print(dim(args.no_color, (
            f"{n_private} private silo(s): excluded from unscoped retrieval; "
            "reach them with --in <slug>."
        )))
    return 0

def cmd_private(args: argparse.Namespace) -> int:
    """Set or clear a silo's private flag."""
    from cli_agent import error_payload, ok_payload, require_yes
    from state import is_silo_private, list_silos, resolve_silo_to_slug, resolve_silo_prefix, set_silo_private

    db = _db_path(args)
    raw = args.silo
    slug = resolve_silo_to_slug(db, raw) or resolve_silo_prefix(db, raw)
    if not slug:
        known = sorted(s.get("slug", "") for s in list_silos(db) if s.get("slug"))
        return _emit_result(
            args,
            error_payload(f"silo not found: {raw}", code="not_found", known_silos=known),
        )
    want_private = not bool(args.off)
    currently = is_silo_private(db, slug)
    blocked = require_yes(
        args,
        action="private --off" if args.off else "private",
        detail=(
            "Clearing private widens unscoped retrieval exposure."
            if args.off
            else "Marks the silo local-only (excluded from unscoped retrieval)."
        ),
    )
    if blocked:
        return _emit_result(args, blocked)
    if currently == want_private:
        msg = f"{slug} is already {'private' if want_private else 'not private'}."
        return _emit_result(
            args,
            ok_payload({"slug": slug, "private": currently, "unchanged": True}),
            human=msg,
        )
    if getattr(args, "dry_run", False):
        plan = ok_payload({"dry_run": True, "slug": slug, "would_set_private": want_private})
        return _emit_result(args, plan, human=f"Would set private={want_private} on {slug}")
    set_silo_private(db, slug, want_private)
    if want_private:
        human = f"{slug}: private set — excluded from unscoped retrieval; use --in {slug} to query it."
    else:
        human = f"{slug}: private cleared — it can now be returned by unscoped queries."
    return _emit_result(args, ok_payload({"slug": slug, "private": want_private}), human=human)


def cmd_query(args: argparse.Namespace) -> int:
    """Retrieve chunks without LLM synthesis (MCP query_personal_knowledge parity)."""
    from cli_agent import error_payload, meta_filter_kwargs, ok_payload
    from query.core import run_retrieve

    if not getattr(args, "text", False):
        args.json = True
    query = _sanitize_query(getattr(args, "query", None) or [])
    if query is None:
        return _emit_result(
            args,
            error_payload("empty or invalid query (or too long)", code="usage", exit_code=2),
        )
    try:
        result = run_retrieve(
            query=query,
            silo=getattr(args, "in_silo", None),
            n_results=getattr(args, "n_results", 12),
            db_path=_db_path(args),
            config_path=_config_path(args),
            **meta_filter_kwargs(args),
        )
    except Exception as e:
        busy_rc = _handle_busy(args, e, "query")
        if busy_rc >= 0:
            return busy_rc
        return _emit_result(args, error_payload(str(e)))
    if result.get("error"):
        extra = {k: result[k] for k in ("chunks", "query", "intent", "silo_filter") if k in result}
        return _emit_result(args, error_payload(str(result["error"]), code="retrieve", **extra))
    payload = ok_payload(result)
    if getattr(args, "text", False):
        args.json = False
        lines = [
            f"[{c.get('score', '?')}] {c.get('source', '?')}\n{(c.get('text') or '')[:400]}"
            for c in result.get("chunks") or []
        ]
        return _emit_result(args, payload, human="\n\n".join(lines) if lines else "(no chunks)")
    return _emit_result(args, payload)


def cmd_multi_query(args: argparse.Namespace) -> int:
    """Parallel retrieve + merge (MCP multi_query_knowledge parity)."""
    from cli_agent import error_payload, meta_filter_kwargs, run_multi_query

    if not getattr(args, "text", False):
        args.json = True
    queries = list(getattr(args, "q", None) or [])
    for item in getattr(args, "query", None) or []:
        s = _sanitize_query([item])
        if s:
            queries.append(s)
    cleaned: list[str] = []
    for q in queries:
        if isinstance(q, str):
            s = _sanitize_query([q])
            if s:
                cleaned.append(s)
    if not cleaned:
        return _emit_result(
            args,
            error_payload("provide one or more queries via --q or positional args", code="usage", exit_code=2),
        )
    try:
        result = run_multi_query(
            cleaned,
            db_path=_db_path(args),
            silo=getattr(args, "in_silo", None),
            n_results=getattr(args, "n_results", 10),
            max_total_chunks=getattr(args, "max_total_chunks", 30),
            **meta_filter_kwargs(args),
        )
    except Exception as e:
        busy_rc = _handle_busy(args, e, "multi-query")
        if busy_rc >= 0:
            return busy_rc
        return _emit_result(args, error_payload(str(e)))
    if result.get("ok") is False and result.get("busy"):
        return _emit_result(args, result)
    result.setdefault("ok", True)
    return _emit_result(args, result)


def cmd_read(args: argparse.Namespace) -> int:
    """Read one indexed file from the index (MCP read_document parity)."""
    from cli_agent import error_payload, ok_payload, resolve_indexed_file
    from chroma_client import get_client, release
    from chroma_lock import chroma_shared_lock
    from constants import LLMLI_COLLECTION
    from document_read import read_indexed_document

    if not getattr(args, "text", False):
        args.json = True
    db = _db_path(args)
    path = getattr(args, "path", None) or ""
    silo = getattr(args, "in_silo", None)
    resolved, slug, candidates, err = resolve_indexed_file(db, path, silo)
    if err:
        return _emit_result(
            args,
            error_payload(err, code="not_found", candidates=candidates[:20] if candidates else []),
        )
    try:
        with chroma_shared_lock(str(db)):
            collection = get_client(str(db)).get_or_create_collection(name=LLMLI_COLLECTION)
            result = read_indexed_document(
                collection,
                silo=str(slug),
                source=str(resolved),
                start_chunk=int(getattr(args, "start_chunk", 0) or 0),
                max_chars=int(getattr(args, "max_chars", 6000) or 6000),
            )
    except Exception as e:
        busy_rc = _handle_busy(args, e, "read")
        if busy_rc >= 0:
            return busy_rc
        return _emit_result(args, error_payload(str(e)))
    finally:
        release()
    if getattr(args, "text", False):
        args.json = False
        return _emit_result(args, ok_payload(result), human=result.get("text") or "")
    return _emit_result(args, ok_payload(result))


def cmd_status(args: argparse.Namespace) -> int:
    """Show write-in-progress markers and Chroma lock holders."""
    from cli_agent import status_payload

    payload = status_payload(_db_path(args))
    if _wants_json(args):
        return _emit_result(args, payload)
    writes = payload.get("write_in_progress") or {}
    lock = payload.get("lock") or {}
    print(f"DB: {payload.get('db_path')}")
    if writes:
        print("Write in progress:")
        for slug, info in writes.items():
            print(f"  {slug}: kind={info.get('kind')} pid={info.get('pid')} started={info.get('started_at')}")
    else:
        print("Write in progress: (none)")
    holders = lock.get("holder_pids") or []
    print(f"Chroma lock: {lock.get('path')} holders={holders or 'none'}")
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    """Emit the agent-facing command schema (always JSON)."""
    from cli_agent import command_schema, emit_json

    emit_json(command_schema())
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    from cli_agent import emit_json, format_version_line, version_info

    info = version_info(repo_root=_ROOT, cli_file=Path(__file__))
    if _wants_json(args):
        emit_json(info)
    else:
        print(format_version_line(info))
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
    """Remove a silo from the registry and delete its chunks. Accepts slug or display name."""
    from cli_agent import error_payload, ok_payload, require_yes
    from operations import op_remove_silo
    from state import list_silos, resolve_silo_by_path, resolve_silo_prefix, resolve_silo_to_slug

    silo = getattr(args, "silo", None)
    if not silo:
        return _emit_result(
            args,
            error_payload('remove requires silo name. Example: llmli rm "Tax" --yes', code="usage", exit_code=2),
        )
    db = _db_path(args)
    raw = " ".join(silo) if isinstance(silo, list) else str(silo)
    blocked = require_yes(
        args,
        action="rm",
        detail=f"Would remove registry entry + Chroma chunks for {raw!r}.",
    )
    if blocked:
        # Enrich dry-run-style preview even on confirm failure
        return _emit_result(args, blocked)
    slug = (
        resolve_silo_to_slug(db, raw)
        or resolve_silo_prefix(db, raw)
        or (resolve_silo_by_path(db, raw) if Path(raw).exists() else None)
    )
    if getattr(args, "dry_run", False):
        info = next((s for s in list_silos(db) if s.get("slug") == slug), None) if slug else None
        plan = ok_payload(
            {
                "dry_run": True,
                "would": "remove_silo",
                "target": raw,
                "resolved_slug": slug,
                "path": (info or {}).get("path"),
                "chunks_count": (info or {}).get("chunks_count"),
            }
        )
        return _emit_result(
            args,
            plan,
            human=f"Would remove silo {slug or raw}" + (f" ({plan['path']})" if plan.get("path") else ""),
        )
    try:
        result = op_remove_silo(str(db), raw)
    except Exception as e:
        busy_rc = _handle_busy(args, e, "rm")
        if busy_rc >= 0:
            return busy_rc
        return _emit_result(args, error_payload(str(e)))
    payload = ok_payload(result)
    if result.get("chroma_warning") and not _wants_json(args):
        print(f"Warning: could not delete chunks from DB: {result['chroma_warning']}", file=sys.stderr)
    if not result["not_found"]:
        human = f"Removed silo: {result['removed_slug']}"
    else:
        human = f"Removed chunks and file manifest for silo: {result['cleaned_slug']} (was not in silo list)"
    return _emit_result(args, payload, human=human)

def cmd_repair(args: argparse.Namespace) -> int:
    """Wipe and re-index a silo to fix ChromaDB index inconsistencies ('Error finding id')."""
    from cli_agent import error_payload, ok_payload, require_yes
    from operations import op_repair_silo
    from state import list_silos, resolve_silo_prefix, resolve_silo_to_slug

    silo_arg = getattr(args, "silo", None)
    if not silo_arg:
        return _emit_result(
            args,
            error_payload("repair requires a silo slug or display name", code="usage", exit_code=2),
        )
    db = _db_path(args)
    raw = " ".join(silo_arg) if isinstance(silo_arg, list) else str(silo_arg)
    blocked = require_yes(
        args,
        action="repair",
        detail="Wipes all chunks for the silo then fully re-indexes from its source path.",
    )
    if blocked:
        return _emit_result(args, blocked)
    slug = resolve_silo_to_slug(db, raw) or resolve_silo_prefix(db, raw)
    info = next((s for s in list_silos(db) if s.get("slug") == slug), None) if slug else None
    if getattr(args, "dry_run", False):
        plan = ok_payload(
            {
                "dry_run": True,
                "would": "wipe_and_reindex",
                "target": raw,
                "resolved_slug": slug,
                "path": (info or {}).get("path"),
            }
        )
        return _emit_result(
            args,
            plan,
            human=f"Would wipe + re-index {slug or raw}" + (f" from {plan['path']}" if plan.get("path") else ""),
        )
    try:
        result = op_repair_silo(str(db), raw, verbose=not _wants_json(args))
    except Exception as e:
        busy_rc = _handle_busy(args, e, "repair")
        if busy_rc >= 0:
            return busy_rc
        return _emit_result(args, error_payload(str(e)))
    if result.get("status") == "error":
        return _emit_result(args, error_payload(result.get("error", "repair failed"), code="repair"))
    return _emit_result(args, ok_payload(result), human=None if _wants_json(args) else f"Repair completed: {result.get('slug')}")

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
    from cli_agent import ok_payload
    from ingest import get_capabilities_text

    text = get_capabilities_text()
    if _wants_json(args):
        return _emit_result(args, ok_payload({"text": text}))
    print(text)
    return 0

def cmd_log(args: argparse.Namespace) -> int:
    """Show last index/add failures (from llmli add)."""
    from cli_agent import ok_payload
    from state import get_last_failures

    db = _db_path(args)
    failures = get_last_failures(db)
    if _wants_json(args):
        return _emit_result(args, ok_payload({"failures": failures or []}))
    if not failures:
        print("No recent failures.")
        return 0
    for f in failures:
        print(f"  {f.get('path', '?')}: {f.get('error', '?')}")
    return 0

def cmd_verify(args: argparse.Namespace) -> int:
    """Report (and optionally purge) indexed files that match a silo's exclude patterns."""
    from cli_agent import error_payload, ok_payload, require_yes
    from operations import op_verify_silo

    db = _db_path(args)
    silo_arg = getattr(args, "silo", None)
    if not silo_arg:
        return _emit_result(
            args,
            error_payload("verify requires silo name. Example: llmli verify recipes-55cf8f91", code="usage", exit_code=2),
        )
    purge = bool(getattr(args, "purge", False))
    dry_run = bool(getattr(args, "dry_run", False))
    if purge and not dry_run:
        blocked = require_yes(
            args,
            action="verify --purge",
            detail="Removes violating files from the index (manifest + chunks).",
        )
        if blocked:
            return _emit_result(args, blocked)
    try:
        result = op_verify_silo(str(db), silo_arg, purge=purge and not dry_run)
    except Exception as e:
        busy_rc = _handle_busy(args, e, "verify")
        if busy_rc >= 0:
            return busy_rc
        return _emit_result(args, error_payload(str(e)))
    if result.get("status") == "error":
        return _emit_result(args, error_payload(result.get("error", "verify failed"), code="verify"))
    if dry_run and purge:
        result = dict(result)
        result["dry_run"] = True
        result["would_purge"] = [v.get("source") for v in (result.get("violations") or [])]
    slug = result["slug"]
    display = result.get("display_name", slug)
    viol_n = int(result.get("violation_count") or 0)
    payload = ok_payload(result) if viol_n == 0 and not (result.get("purge") or {}).get("errors") else dict(result)
    if "ok" not in payload:
        # violations are a successful report with non-zero exit for agents when not purged
        payload = {"ok": True, **result} if (purge and result.get("purge")) or viol_n == 0 else {"ok": True, **result}
    if _wants_json(args):
        # Exit 1 when violations remain unpurged (same as human mode)
        emit_rc = 0
        if viol_n and not (purge and not dry_run):
            emit_rc = 1
        errors = ((result.get("purge") or {}).get("errors")) or []
        if errors and not (result.get("purge") or {}).get("purged"):
            emit_rc = 1
        from cli_agent import emit_json
        emit_json(payload)
        return emit_rc
    print(f"Silo: {display} ({slug})")
    print(f"Effective excludes: {result.get('effective_exclude_count', 0)} "
          f"(persisted {result.get('persisted_exclude_count', 0)})")
    if viol_n == 0:
        print("No indexed files match this silo's exclude patterns.")
        return 0
    print(f"Violations: {viol_n}")
    for hit in result.get("violations") or []:
        src = str(hit.get("source") or "")
        short = src if len(src) <= 72 else "..." + src[-69:]
        print(f"  matched {hit.get('matched_pattern')!r}: {short}")
    if dry_run and purge:
        print(f"Dry-run: would purge {viol_n} file(s). Pass --yes with --purge to apply.")
        return 1
    purge_info = result.get("purge")
    if purge_info is not None:
        purged = purge_info.get("purged") or []
        errors = purge_info.get("errors") or []
        print(f"Purged: {len(purged)}")
        for err in errors[:10]:
            print(f"  purge error: {err}", file=sys.stderr)
        return 1 if errors and not purged else 0
    print(f"Run `llmli verify {slug} --purge --yes` to remove them.")
    return 1

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
        from cli_agent import error_payload
        return _emit_result(args, error_payload(result["error"], code="inspect"))
    if _wants_json(args):
        from cli_agent import ok_payload
        return _emit_result(args, ok_payload(result))

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

    viol_n = int(result.get("exclude_violation_count") or 0)
    if viol_n:
        print(
            f"[llmli] exclude violations: {viol_n} indexed file(s) match this silo's "
            f"exclude patterns. Run `llmli verify {slug} --purge` to remove them.",
            file=sys.stderr,
        )
        for hit in (result.get("exclude_violations") or [])[:5]:
            src = str(hit.get("source") or "")
            short = src if len(src) <= 72 else "..." + src[-69:]
            print(f"  matched {hit.get('matched_pattern')!r}: {short}", file=sys.stderr)

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
        from cli_agent import error_payload
        return _emit_result(args, error_payload(str(e), code="usage", exit_code=2))

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
        busy_rc = _handle_busy(args, e, "find")
        if busy_rc >= 0:
            return busy_rc
        from cli_agent import error_payload
        return _emit_result(args, error_payload(str(e)))

    if getattr(args, "json", False):
        from cli_agent import ok_payload
        return _emit_result(args, ok_payload(result))

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
    model = getattr(args, "model", None) or os.environ.get("LLMLIBRARIAN_MODEL", "llama3.2:latest")
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
    from cli_agent import add_meta_filter_arguments, quiet_machine_noise

    parser = _LlmliArgumentParser(
        prog="llmli",
        description=(
            "llmLibrarian CLI for humans and agents. "
            "Prefer --json for automation; see `llmli schema` and docs/cli-for-agents.md."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=os.environ.get("LLMLIBRARIAN_DB", str(_ROOT / "my_brain_db")), help="DB path")
    parser.add_argument("--config", help="Path to archetypes.yaml")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable JSON on stdout (stable envelopes; errors are objects; non-zero exit on failure)",
    )
    parser.add_argument("-V", "--version", action=_VersionAction, nargs=0, help="Show version, git commit, and install path")
    sub = parser.add_subparsers(dest="command", required=True)

    def _json_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    def _yes_dry(p: argparse.ArgumentParser) -> None:
        p.add_argument("--yes", "-y", action="store_true", help="Confirm a destructive apply")
        p.add_argument("--dry-run", action="store_true", help="Preview what would change without applying")

    p_add = sub.add_parser("add", help="Index folder (or single file) into llmli (silo = basename); cloud-sync paths blocked unless --allow-cloud")
    p_add.add_argument("path", help="Folder or file path to index (single files bypass include/exclude filters)")
    p_add.add_argument("--allow-cloud", action="store_true", help="Allow OneDrive/iCloud/Dropbox/Google Drive (ingestion may be unreliable)")
    p_add.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks inside the target folder")
    p_add.add_argument("--full", action="store_true", help="Full reindex (delete + add) instead of incremental; requires --yes")
    p_add.add_argument("--exclude", action="append", dest="exclude_patterns", help="Extra path exclusion pattern (repeatable)")
    p_add.add_argument("--image-vision", action="store_true", default=None, help="Enable multimodal image summaries for this silo (default: off unless previously enabled)")
    p_add.add_argument("--workers", type=int, help="Override file/extraction worker count for this run")
    p_add.add_argument("--embedding-workers", type=int, help="Override embedding worker count for this run")
    p_add.add_argument("--fast", action="store_true", help="Auto-tune ingest for this host (device, batch sizes, workers); explicit env vars still win")
    p_add.add_argument("--silo", dest="silo", help=argparse.SUPPRESS)
    p_add.add_argument("--display-name", dest="display_name", help=argparse.SUPPRESS)
    _yes_dry(p_add)
    _json_flag(p_add)
    p_add.set_defaults(db=None)
    p_add.set_defaults(_run=cmd_add)

    p_ask = sub.add_parser("ask", help="Ask via local Ollama (unified llmli by default; or archetype / --in silo)")
    p_ask.add_argument("--archetype", "-a", help="Archetype id (e.g. tax, infra)")
    in_silo_arg = p_ask.add_argument("--in", dest="in_silo", metavar="SILO", help="Limit to one silo (slug or display name)")
    in_silo_arg.completer = _silo_completer  # type: ignore[attr-defined]
    p_ask.add_argument("--unified", action="store_true", help="Search across all silos (compare/analyze across indexed content)")
    p_ask.add_argument("--strict", action="store_true", help="Never conclude absence from partial evidence; say unknown + sources when unsure")
    p_ask.add_argument("--quiet", "-q", action="store_true", help="Answer only (no source footer); useful for scripting")
    p_ask.add_argument("--explain", action="store_true", help="Print deterministic catalog diagnostics to stderr when applicable")
    p_ask.add_argument("--force", action="store_true", help="Allow deterministic catalog queries to run on stale scope")
    p_ask.add_argument("--model", "-m", help="Ollama model (default: LLMLIBRARIAN_MODEL or llama3.2:latest)")
    p_ask.add_argument("--n-results", type=int, default=12, help="Retrieval count")
    p_ask.add_argument("query", nargs="+", help="Question")
    _json_flag(p_ask)
    p_ask.set_defaults(_run=cmd_ask)

    p_query = sub.add_parser(
        "query",
        help="Retrieve chunks (no LLM) — MCP query_personal_knowledge parity; JSON by default",
    )
    q_in = p_query.add_argument("--in", dest="in_silo", metavar="SILO", help="Limit to one silo (exact slug for private)")
    q_in.completer = _silo_completer  # type: ignore[attr-defined]
    p_query.add_argument("--n-results", type=int, default=12, help="Retrieval count")
    p_query.add_argument("--text", action="store_true", help="Human-readable chunk dump instead of JSON")
    add_meta_filter_arguments(p_query)
    p_query.add_argument("query", nargs="+", help="Search query")
    _json_flag(p_query)
    p_query.set_defaults(_run=cmd_query)

    p_mq = sub.add_parser(
        "multi-query",
        help="Parallel retrieve + merge — MCP multi_query_knowledge parity; JSON by default",
    )
    mq_in = p_mq.add_argument("--in", dest="in_silo", metavar="SILO", help="Limit to one silo (exact slug for private)")
    mq_in.completer = _silo_completer  # type: ignore[attr-defined]
    p_mq.add_argument("--q", action="append", dest="q", metavar="QUERY", help="Query (repeatable)")
    p_mq.add_argument("--n-results", type=int, default=10, help="Per-query retrieval count")
    p_mq.add_argument("--max-total-chunks", type=int, default=30, help="Cap merged chunks")
    p_mq.add_argument("--text", action="store_true", help="Human-readable output instead of JSON")
    add_meta_filter_arguments(p_mq)
    p_mq.add_argument("query", nargs="*", help="Additional queries")
    _json_flag(p_mq)
    p_mq.set_defaults(_run=cmd_multi_query)

    p_read = sub.add_parser("read", help="Read one indexed file from the index — MCP read_document parity; JSON by default")
    p_read.add_argument("path", help="Indexed path or unique filename")
    r_in = p_read.add_argument("--in", dest="in_silo", metavar="SILO", help="Silo slug (exact slug required for private)")
    r_in.completer = _silo_completer  # type: ignore[attr-defined]
    p_read.add_argument("--start-chunk", type=int, default=0, help="Paging offset")
    p_read.add_argument("--max-chars", type=int, default=6000, help="Page size in characters")
    p_read.add_argument("--text", action="store_true", help="Print document text only")
    _json_flag(p_read)
    p_read.set_defaults(_run=cmd_read)

    p_ls = sub.add_parser("ls", help="List silos")
    _json_flag(p_ls)
    p_ls.set_defaults(_run=cmd_ls)

    p_status = sub.add_parser("status", help="Write-in-progress markers and Chroma lock holders")
    _json_flag(p_status)
    p_status.set_defaults(_run=cmd_status)

    p_schema = sub.add_parser("schema", help="Emit agent-facing command/arg/output schema as JSON")
    p_schema.set_defaults(_run=cmd_schema)

    p_version = sub.add_parser("version", help="Show version, git commit, and install path")
    _json_flag(p_version)
    p_version.set_defaults(_run=cmd_version)

    p_private = sub.add_parser(
        "private",
        help="Mark a silo local-only (excluded from unscoped retrieval), or clear the mark",
    )
    p_private.add_argument("silo", help="Silo slug, slug prefix, or display name")
    p_private.add_argument(
        "--off",
        action="store_true",
        help="Clear the flag — the silo becomes reachable from unscoped queries again",
    )
    _yes_dry(p_private)
    _json_flag(p_private)
    p_private.set_defaults(_run=cmd_private)

    p_inspect = sub.add_parser("inspect", help="Show silo details and top files by chunk count (default: top 20)")
    inspect_silo_arg = p_inspect.add_argument("silo", help="Silo slug or display name")
    inspect_silo_arg.completer = _silo_completer  # type: ignore[attr-defined]
    p_inspect.add_argument("--top", type=int, default=20, help="Show top N chunkiest files (default: 20)")
    p_inspect.add_argument("--filter", choices=["pdf", "docx", "code"], help="Show only this type (pdf, docx, or code)")
    _json_flag(p_inspect)
    p_inspect.set_defaults(_run=cmd_inspect)

    p_verify = sub.add_parser(
        "verify",
        help="Report indexed files that match the silo's exclude patterns (optionally purge them)",
    )
    verify_silo_arg = p_verify.add_argument("silo", help="Silo slug or display name")
    verify_silo_arg.completer = _silo_completer  # type: ignore[attr-defined]
    p_verify.add_argument(
        "--purge",
        action="store_true",
        help="Remove violating files from the index (manifest + chunks); requires --yes",
    )
    _yes_dry(p_verify)
    _json_flag(p_verify)
    p_verify.set_defaults(_run=cmd_verify)

    p_index = sub.add_parser("index", help="Rebuild archetype collection from config folders")
    p_index.add_argument("--archetype", "-a", required=True, help="Archetype id")
    p_index.add_argument("--log", help="Log file path (or set LLMLIBRARIAN_LOG=1)")
    p_index.add_argument("--mode", choices=["fast", "normal", "deep"], default="normal")
    p_index.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks in config folders")
    p_index.set_defaults(_run=cmd_index)

    p_rm = sub.add_parser("rm", help="Remove silo (registry + chunks); requires --yes")
    rm_silo_arg = p_rm.add_argument("silo", nargs="+", help="Silo slug, display name, or path")
    rm_silo_arg.completer = _silo_completer  # type: ignore[attr-defined]
    _yes_dry(p_rm)
    _json_flag(p_rm)
    p_rm.set_defaults(_run=cmd_rm)

    p_repair = sub.add_parser("repair", help="Fix ChromaDB index errors by wiping and re-indexing a silo; requires --yes")
    repair_silo_arg = p_repair.add_argument("silo", nargs="+", help="Silo slug or display name")
    repair_silo_arg.completer = _silo_completer  # type: ignore[attr-defined]
    _yes_dry(p_repair)
    _json_flag(p_repair)
    p_repair.set_defaults(_run=cmd_repair)

    p_repair_ladder = sub.add_parser(
        "repair-ladder",
        help="Read-only Chroma diagnostics and L1→L3 repair guidance",
    )
    _json_flag(p_repair_ladder)
    p_repair_ladder.set_defaults(_run=cmd_repair_ladder)

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
    p_rehydrate.add_argument("--fast", action="store_true", help="Auto-tune ingest for this host (device, batch sizes, workers); explicit env vars still win")
    p_rehydrate.add_argument("--quiet", action="store_true", help="Suppress per-silo progress output")
    _json_flag(p_rehydrate)
    p_rehydrate.set_defaults(_run=cmd_rehydrate)

    p_capabilities = sub.add_parser("capabilities", help="Supported file types and document extractors (source of truth)")
    _json_flag(p_capabilities)
    p_capabilities.set_defaults(_run=cmd_capabilities)

    p_log = sub.add_parser("log", help="Show last add failures")
    p_log.add_argument("--last", action="store_true", help="Show last add failures (default)")
    _json_flag(p_log)
    p_log.set_defaults(_run=cmd_log)

    p_find = sub.add_parser("find", help="Find files by name/date (manifest-only; no embeddings)")
    find_in_arg = p_find.add_argument("--in", dest="in_silo", action="append", metavar="SILO", help="Restrict to silo (repeatable)")
    find_in_arg.completer = _silo_completer  # type: ignore[attr-defined]
    p_find.add_argument("--name", metavar="GLOB", help="fnmatch-style glob applied to relative path or filename")
    p_find.add_argument("--date", metavar="DATE", help="YYYY-MM-DD or START:END (either bound may be empty)")
    p_find.add_argument("--field", choices=["name_date", "mtime", "either"], default="either", help="Which date signal to filter on (default: either)")
    p_find.add_argument("--with-chunks", action="store_true", help="Include chunk_count per file (touches Chroma)")
    p_find.add_argument("--limit", type=int, default=50, help="Cap result count (default: 50)")
    _json_flag(p_find)
    p_find.set_defaults(_run=cmd_find)

    p_reindex = sub.add_parser("reindex", help="One-shot maintenance commands for the manifest")
    reindex_sub = p_reindex.add_subparsers(dest="reindex_subcommand", required=True)
    p_reindex_names = reindex_sub.add_parser("names", help="Backfill name_date / name_date_precision in the manifest")
    reindex_in_arg = p_reindex_names.add_argument("--in", dest="in_silo", action="append", metavar="SILO", help="Restrict to silo (repeatable)")
    reindex_in_arg.completer = _silo_completer  # type: ignore[attr-defined]
    p_reindex_names.set_defaults(_run=cmd_reindex_names)

    p_eval = sub.add_parser("eval-adversarial", help="Run synthetic adversarial trustfulness eval")
    p_eval.add_argument("--model", "-m", help="Ollama model (default: LLMLIBRARIAN_MODEL or llama3.2:latest)")
    p_eval.add_argument("--out", help="Write JSON report to this path")
    p_eval.add_argument("--limit", type=int, help="Run only first N queries from the fixed suite")
    p_eval.add_argument("--strict-mode", dest="strict_mode", action="store_true", default=True, help="Run eval queries with strict ask mode (default: on)")
    p_eval.add_argument("--no-strict-mode", dest="strict_mode", action="store_false", help="Run eval queries without strict ask mode")
    p_eval.add_argument("--direct-decisive-mode", action="store_true", default=None, help="Override config to enable direct decisive mode for this eval run")
    p_eval.add_argument("--no-direct-decisive-mode", dest="direct_decisive_mode", action="store_false", help="Override config to disable direct decisive mode for this eval run")
    p_eval.set_defaults(_run=cmd_eval_adversarial)

    parser._llmli_subcommands = sorted(sub.choices.keys())  # type: ignore[attr-defined]

    try:
        import argcomplete
        argcomplete.autocomplete(parser)
    except ImportError:
        pass
    args = parser.parse_args()
    if "--json" in sys.argv:
        args.json = True
        quiet_machine_noise()
    if getattr(args, "fast", False):
        src = _ROOT / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        from perf_profile import apply_fast_profile

        apply_fast_profile()
    try:
        src = _ROOT / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        from proctitle import set_process_title

        target = (
            getattr(args, "silo", None)
            or getattr(args, "in_silo", None)
            or getattr(args, "path", None)
        )
        if isinstance(target, str) and "/" in target:
            target = Path(target).name
        set_process_title("cli", args.command, target)
    except Exception:
        pass
    return args._run(args)


if __name__ == "__main__":
    sys.exit(main())
