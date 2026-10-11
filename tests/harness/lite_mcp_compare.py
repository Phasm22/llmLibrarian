"""Compare full vs lite MCP surfaces on scripted small-model tasks.

Run:
  uv run python -m tests.harness.lite_mcp_compare [--json PATH]

Uses in-process tool functions (not HTTP) so the harness stays deterministic and
does not need chroma run / Ollama. Catalog size is measured from FastMCP schemas.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

# Ensure repo root is importable when run as a module.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@dataclass
class TaskResult:
    task: str
    profile: str  # full | lite
    ok: bool
    detail: str
    notes: list[str] = field(default_factory=list)
    response_bytes: int = 0


@dataclass
class CatalogMeasure:
    profile: str
    tool_names: list[str]
    schema_bytes: int
    approx_tokens: int
    instructions_bytes: int


def _json_bytes(obj: Any) -> int:
    return len(json.dumps(obj, default=str, ensure_ascii=False).encode("utf-8"))


def _approx_tokens(n_bytes: int) -> int:
    # Rough OpenAI-ish estimate used in the small-model contract notes (~4 chars/token).
    return max(1, n_bytes // 4) if n_bytes else 0


def _has_param(fn: Callable[..., Any], name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


async def measure_catalog(profile: str) -> CatalogMeasure:
    """Spawn a subprocess-shaped import of mcp_server with the given profile."""
    script = r"""
import asyncio, json, os, sys
import mcp_server

async def main():
    tools = await mcp_server.mcp.list_tools()
    # Wire-facing catalog fields only (fn/serializer objects are not JSON-serializable).
    payload = [
        {
            "name": t.name,
            "description": t.description or "",
            "parameters": getattr(t, "parameters", None) or {},
        }
        for t in tools
    ]
    out = {
        "names": sorted(t.name for t in tools),
        "schema_bytes": len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
        "instructions_bytes": len((mcp_server.mcp.instructions or "").encode("utf-8")),
    }
    print(json.dumps(out))

asyncio.run(main())
"""
    env = os.environ.copy()
    env["LLMLIBRARIAN_MCP_PROFILE"] = profile
    env["LLMLIBRARIAN_ENV_BOOTSTRAPPED"] = "1"
    # Avoid the missing-DB warning noise.
    env.setdefault("LLMLIBRARIAN_DB", str(_ROOT / "my_brain_db"))
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    return CatalogMeasure(
        profile=profile,
        tool_names=data["names"],
        schema_bytes=data["schema_bytes"],
        approx_tokens=_approx_tokens(data["schema_bytes"]),
        instructions_bytes=data["instructions_bytes"],
    )


def _bind_mcp(db_path: Path, monkey_env: dict[str, str] | None = None):
    """Point the already-imported mcp_server module at ``db_path``."""
    import mcp_server

    mcp_server._DB_PATH = str(db_path)
    mcp_server._release_chroma = lambda: None  # type: ignore[assignment]
    mcp_server._emit_query_audit = lambda **_k: None  # type: ignore[assignment]
    if monkey_env:
        for key, value in monkey_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return mcp_server


def run_tasks(index, mcp) -> list[TaskResult]:
    """Execute scripted tasks against full and lite tool surfaces."""
    from state import set_silo_private

    results: list[TaskResult] = []
    recipes = index.recipes_slug
    journal = index.journal_slug
    private = index.private_slug

    def record(task: str, profile: str, ok: bool, detail: str, response: Any = None, notes: list[str] | None = None):
        results.append(
            TaskResult(
                task=task,
                profile=profile,
                ok=ok,
                detail=detail,
                notes=notes or [],
                response_bytes=_json_bytes(response) if response is not None else 0,
            )
        )

    # --- 1. Filtered recipe lookup (frontmatter) ---
    task = "filtered_recipe_bison_dinner"
    full = mcp.query_personal_knowledge(
        "high protein dinner",
        silo=recipes,
        n_results=8,
        main_protein="bison",
        category="dinner",
        max_total_time=60,
    )
    full_ok = any("bison" in (c.get("text") or "").lower() or "bison" in str(c.get("metadata") or {}).lower() for c in full.get("chunks") or [])
    # Prefer structured metadata when present.
    if full.get("chunks"):
        meta_hit = any(
            (c.get("metadata") or {}).get("main_protein") == "bison"
            for c in full["chunks"]
        )
        full_ok = full_ok or meta_hit
    record(task, "full", bool(full_ok and "error" not in full), f"chunks={len(full.get('chunks') or [])}", full)

    lite_kwargs: dict[str, Any] = {"query": "high protein dinner", "silo": recipes, "n_results": 8}
    if _has_param(mcp.retrieve_knowledge, "main_protein"):
        lite_kwargs.update(main_protein="bison", category="dinner", max_total_time=60)
        lite_notes = ["filters available on retrieve_knowledge"]
    else:
        lite_notes = ["GAP: retrieve_knowledge has no frontmatter filter params"]
    lite = mcp.retrieve_knowledge(**lite_kwargs)
    lite_chunks = lite.get("chunks") or []
    if _has_param(mcp.retrieve_knowledge, "main_protein"):
        lite_ok = any(
            (c.get("metadata") or {}).get("main_protein") == "bison"
            or "bison" in (c.get("text") or "").lower()
            for c in lite_chunks
        ) and "error" not in lite
        # Also require chicken-only hits are not dominating when filters work.
        chicken_only = lite_chunks and all("chicken" in (c.get("text") or "").lower() and "bison" not in (c.get("text") or "").lower() for c in lite_chunks)
        if chicken_only:
            lite_ok = False
            lite_notes.append("filters did not exclude chicken-soup noise")
    else:
        # Without filters, semantic search may still mention bison — that is not a reliable completion.
        lite_ok = False
        lite_notes.append("cannot constrain by main_protein/category/max_total_time")
    # Metadata presence for citation
    if lite_chunks and not any(isinstance(c.get("metadata"), dict) and c.get("metadata") for c in lite_chunks):
        lite_notes.append("GAP: compact chunks drop frontmatter metadata")
    record(task, "lite", lite_ok, f"chunks={len(lite_chunks)}", lite, lite_notes)

    # --- 2. What did I eat on a date ---
    task = "meal_on_date_2026_10_10"
    full_ff = mcp.find_files(silos=[journal], date_start="2026-10-10", date_end="2026-10-10", name_glob="*meal*")
    full_files = full_ff.get("files") or []
    full_ok = any("bison" in (f.get("path") or f.get("name") or "").lower() for f in full_files)
    record(task, "full", full_ok, f"files={len(full_files)}", full_ff)

    if hasattr(mcp, "find_files") and "find_files" in {n for n, _ in getattr(mcp, "_lite_tools", lambda: ())()}:
        lite_ff = mcp.find_files(silos=[journal], date_start="2026-10-10", date_end="2026-10-10", name_glob="*meal*")
        lite_files = lite_ff.get("files") or []
        lite_ok = any("bison" in (f.get("path") or f.get("name") or "").lower() for f in lite_files)
        record(task, "lite", lite_ok, f"files={len(lite_files)}", lite_ff, ["find_files in lite catalog"])
    else:
        # Fall back: semantic retrieve only — filenames may not be in chunk text reliably enough.
        lite_r = mcp.retrieve_knowledge("what did I eat on 2026-10-10 meal bison", silo=journal, n_results=8)
        texts = " ".join(c.get("text") or "" for c in lite_r.get("chunks") or [])
        sources = " ".join(c.get("source") or "" for c in lite_r.get("chunks") or [])
        lite_ok = "bison" in texts.lower() or "bison" in sources.lower()
        record(
            task,
            "lite",
            False,  # treat as gap: no date/file listing tool
            f"semantic_fallback chunks={len(lite_r.get('chunks') or [])} hit={lite_ok}",
            lite_r,
            ["GAP: no find_files / date-range listing on lite"],
        )

    # --- 3. Last 7 days summary (relative to 2026-10-10) ---
    task = "last_7_days_meals"
    full_ff = mcp.find_files(silos=[journal], date_start="2026-10-04", date_end="2026-10-10", name_glob="*meal*")
    names = [(f.get("path") or f.get("name") or "") for f in (full_ff.get("files") or [])]
    full_ok = (
        any("2026-10-10" in n for n in names)
        and any("2026-10-08" in n for n in names)
        and not any("2026-10-03" in n for n in names)
    )
    record(task, "full", full_ok, f"files={len(names)}", full_ff)

    lite_tools = {n for n, _ in mcp._lite_tools()}
    if "find_files" in lite_tools:
        lite_ff = mcp.find_files(silos=[journal], date_start="2026-10-04", date_end="2026-10-10", name_glob="*meal*")
        names = [(f.get("path") or f.get("name") or "") for f in (lite_ff.get("files") or [])]
        lite_ok = (
            any("2026-10-10" in n for n in names)
            and any("2026-10-08" in n for n in names)
            and not any("2026-10-03" in n for n in names)
        )
        record(task, "lite", lite_ok, f"files={len(names)}", lite_ff)
    else:
        record(task, "lite", False, "impossible without date-range file listing", None, ["GAP: no find_files"])

    # --- 4. Read a cited file ---
    task = "read_cited_recipe"
    cited = str(index.recipes_root / "bison-burger.md")
    full_doc = mcp.read_document(path=cited, silo=recipes, max_chars=4000)
    full_ok = "bison" in (full_doc.get("text") or "").lower() or any(
        "bison" in (c.get("text") or "").lower() for c in (full_doc.get("chunks") or [])
    )
    record(task, "full", bool(full_ok and "error" not in full_doc), "read_document", full_doc)

    lite_doc = mcp.read_document(path=cited, silo=recipes, max_chars=4000)
    lite_ok = "bison" in (lite_doc.get("text") or "").lower() or any(
        "bison" in (c.get("text") or "").lower() for c in (lite_doc.get("chunks") or [])
    )
    record(task, "lite", bool(lite_ok and "error" not in lite_doc), "read_document", lite_doc)

    # --- 5. Image question guidance (no vision model required) ---
    task = "ask_image_guidance"
    try:
        img_out = mcp.ask_image(question="what does the meal plate photo show?", silo=index.photos_slug)
        action = img_out.get("recommended_action") or {}
        notes = []
        # Tool must exist and return structured output. When no image matches, the
        # recommended next step must stay inside the lite catalog.
        lite_ok = img_out.get("status") in {"ok", "error"}
        detail = img_out.get("status") or "no-status"
        if action.get("tool") == "find_files" and "find_files" not in lite_tools:
            notes.append("GAP: ask_image recommends find_files which lite lacks")
            lite_ok = False
        elif action.get("tool") and action["tool"] not in lite_tools:
            notes.append(f"GAP: recommended_action names {action['tool']!r}")
            lite_ok = False
        record(task, "lite", lite_ok, detail, img_out, notes)
        record(task, "full", True, "tool present", {"status": "assumed"})
    except Exception as e:
        record(task, "lite", False, f"{type(e).__name__}: {e}", None, ["ask_image raised"])
        record(task, "full", False, f"{type(e).__name__}: {e}")

    # --- 6. Private refusal / allow-list ---
    task = "private_silo_policy"
    # Default named: exact slug opens private.
    os.environ.pop("LLMLIBRARIAN_MCP_PRIVATE_READS", None)
    os.environ.pop("LLMLIBRARIAN_MCP_PRIVATE_READS_ALLOW", None)
    set_silo_private(str(index.db_path), private, True)

    named_open = mcp.retrieve_knowledge("PRIVATE-LAB-MARKER-9c2e", silo=private, n_results=3)
    named_ok = bool(named_open.get("chunks"))
    roster = mcp.silo_roster()
    roster_slugs = {r["slug"] for r in roster.get("silos") or []}
    notes = []
    if private in roster_slugs:
        notes.append("silo_roster lists private slug under named (consent key exposed)")
    elif roster.get("private_silos_hidden"):
        notes.append("silo_roster hides all private under named (model cannot discover allowed ones)")

    # none policy
    os.environ["LLMLIBRARIAN_MCP_PRIVATE_READS"] = "none"
    refused = mcp.retrieve_knowledge("PRIVATE-LAB-MARKER-9c2e", silo=private, n_results=3)
    refused_ok = not refused.get("chunks") and bool(refused.get("error"))

    # allow-list: only this private slug is readable and listable
    os.environ["LLMLIBRARIAN_MCP_PRIVATE_READS"] = "named"
    os.environ["LLMLIBRARIAN_MCP_PRIVATE_READS_ALLOW"] = private
    allowed = mcp.retrieve_knowledge("PRIVATE-LAB-MARKER-9c2e", silo=private, n_results=3)
    allowed_roster = {r["slug"] for r in mcp.silo_roster().get("silos") or []}
    roster_lists_allowed = private in allowed_roster
    # Flip allow to junk — the only private silo must refuse.
    os.environ["LLMLIBRARIAN_MCP_PRIVATE_READS_ALLOW"] = "not-a-real-slug"
    blocked = mcp.retrieve_knowledge("PRIVATE-LAB-MARKER-9c2e", silo=private, n_results=3)
    allow_works = (
        bool(allowed.get("chunks"))
        and not blocked.get("chunks")
        and bool(blocked.get("error"))
        and roster_lists_allowed
    )
    if not allow_works:
        notes.append("GAP: LLMLIBRARIAN_MCP_PRIVATE_READS_ALLOW not enforced")
    elif roster_lists_allowed:
        notes.append("allow-list surfaces selected private slug on silo_roster")

    # Restore
    os.environ.pop("LLMLIBRARIAN_MCP_PRIVATE_READS", None)
    os.environ.pop("LLMLIBRARIAN_MCP_PRIVATE_READS_ALLOW", None)

    full_list = mcp.list_silos()
    full_private_visible = private in {s["slug"] for s in full_list.get("silos") or []}
    record(
        task,
        "full",
        named_ok and refused_ok,
        f"named_open={named_ok} none_refused={refused_ok} roster_shows_private={full_private_visible}",
        {"named": named_open.get("error"), "none": refused.get("error")},
        notes,
    )
    record(
        task,
        "lite",
        named_ok and refused_ok and allow_works,
        f"named_open={named_ok} none_refused={refused_ok} allow_list={allow_works}",
        {"blocked_error": blocked.get("error")},
        notes,
    )

    # --- 7. recommended_action names only lite tools ---
    task = "recommended_action_lite_safe"
    empty = mcp.retrieve_knowledge("zzzz-no-such-topic-xyz", silo=recipes, n_results=3)
    action = (empty.get("recommended_action") or {}).get("tool")
    lite_ok = action in lite_tools or action is None
    if action and action not in lite_tools:
        record(task, "lite", False, f"names {action}", empty, ["GAP: recommended_action outside lite catalog"])
    else:
        record(task, "lite", True, f"action={action}", empty)
    record(task, "full", True, "n/a")

    # --- 8. Compact metadata for citation ---
    task = "compact_metadata_fields"
    hit = mcp.retrieve_knowledge("bison burger weeknight", silo=recipes, n_results=5)
    chunks = hit.get("chunks") or []
    has_meta = any(isinstance(c.get("metadata"), dict) and c["metadata"] for c in chunks)
    has_mtime = any(c.get("mtime_iso") for c in chunks)
    notes = []
    if not has_meta:
        notes.append("GAP: no metadata on lite chunks")
    if not has_mtime:
        notes.append("note: mtime_iso absent on lite chunks")
    record(task, "lite", has_meta, f"meta={has_meta} mtime={has_mtime}", hit, notes)

    full_hit = mcp.query_personal_knowledge("bison burger weeknight", silo=recipes, n_results=5)
    full_meta = any(isinstance(c.get("metadata"), dict) and c["metadata"] for c in (full_hit.get("chunks") or []))
    record(task, "full", full_meta, f"meta={full_meta}", full_hit)

    # --- 9. Multi-query coverage (informational) ---
    task = "multi_angle_recipe_survey"
    full_mq = mcp.multi_query_knowledge(
        ["bison high protein", "quick lunch under 30 minutes"],
        silo=recipes,
        n_results=5,
        max_total_chunks=12,
    )
    full_ok = len(full_mq.get("chunks") or []) >= 2
    record(task, "full", full_ok, f"chunks={len(full_mq.get('chunks') or [])}", full_mq)
    if "multi_query_knowledge" in lite_tools or _has_param(mcp.retrieve_knowledge, "queries"):
        # After fix path if added
        if _has_param(mcp.retrieve_knowledge, "queries"):
            lite_mq = mcp.retrieve_knowledge(
                query="bison",
                silo=recipes,
                n_results=5,
                queries=["bison high protein", "quick lunch under 30 minutes"],
            )
        else:
            lite_mq = {"chunks": []}
        record(task, "lite", bool(lite_mq.get("chunks")), "multi supported", lite_mq)
    else:
        # Sequential calls — acceptable workaround if both succeed
        a = mcp.retrieve_knowledge("bison high protein", silo=recipes, n_results=5)
        b = mcp.retrieve_knowledge("quick lunch salmon salad", silo=recipes, n_results=5)
        ok = bool(a.get("chunks")) and bool(b.get("chunks"))
        record(
            task,
            "lite",
            ok,
            "sequential retrieve_knowledge workaround",
            {"a": len(a.get("chunks") or []), "b": len(b.get("chunks") or [])},
            [] if ok else ["GAP: no multi-query; sequential failed"],
        )

    return results


def summarize(results: list[TaskResult], catalogs: list[CatalogMeasure]) -> dict[str, Any]:
    by_task: dict[str, dict[str, Any]] = {}
    for r in results:
        row = by_task.setdefault(r.task, {})
        row[r.profile] = {
            "ok": r.ok,
            "detail": r.detail,
            "notes": r.notes,
            "response_bytes": r.response_bytes,
        }
    gaps = [
        t
        for t, profiles in by_task.items()
        if profiles.get("full", {}).get("ok") and not profiles.get("lite", {}).get("ok")
    ]
    return {
        "catalogs": [asdict(c) for c in catalogs],
        "tasks": by_task,
        "lite_gaps_vs_full": gaps,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, help="Write full report JSON here")
    parser.add_argument("--tmpdir", type=Path, default=None)
    args = parser.parse_args(argv)

    catalogs = [asyncio.run(measure_catalog("full")), asyncio.run(measure_catalog("lite"))]

    tmp_ctx = tempfile.TemporaryDirectory(prefix="lite-mcp-harness-")
    tmp = Path(args.tmpdir) if args.tmpdir else Path(tmp_ctx.name)
    if args.tmpdir:
        tmp.mkdir(parents=True, exist_ok=True)

    from tests.harness.lite_mcp_fixtures import build_index

    index = build_index(tmp)
    mcp = _bind_mcp(index.db_path)
    results = run_tasks(index, mcp)
    report = summarize(results, catalogs)
    report["slugs"] = {
        "recipes": index.recipes_slug,
        "journal": index.journal_slug,
        "private": index.private_slug,
        "photos": index.photos_slug,
    }

    print("=== Catalog size ===")
    for c in catalogs:
        print(
            f"  {c.profile:4} tools={len(c.tool_names)} "
            f"schema={c.schema_bytes}B (~{c.approx_tokens} tok) "
            f"names={c.tool_names}"
        )
    print("=== Tasks (full vs lite) ===")
    for task, profiles in report["tasks"].items():
        f = profiles.get("full", {})
        l = profiles.get("lite", {})
        mark = "OK " if l.get("ok") else "GAP"
        print(f"  [{mark}] {task}")
        print(f"        full: ok={f.get('ok')} {f.get('detail')}")
        print(f"        lite: ok={l.get('ok')} {l.get('detail')} notes={l.get('notes')}")
    print("=== Lite gaps vs full ===")
    print(" ", report["lite_gaps_vs_full"] or "(none)")

    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")

    if not args.tmpdir:
        tmp_ctx.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
