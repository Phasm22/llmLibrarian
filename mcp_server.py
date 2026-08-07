"""
llmLibrarian MCP server — the tool surface.

Process-level concerns (startup, PID lock, auth, the Chroma mutex, background
jobs, health roll-ups) live in ``src/mcp_runtime/``. This module owns the
FastMCP instance and the ``@mcp.tool()`` functions, and nothing else.
"""

import logging
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Order matters: put src/ on the path, then silence third-party stdout noise
# before anything that could import huggingface/tqdm. Under stdio this process
# speaks JSON-RPC over stdout and one stray progress bar corrupts the stream.
_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mcp_runtime import bootstrap  # noqa: E402

bootstrap.silence_stdio_noise()
bootstrap.bootstrap_env(_ROOT)

from constants import env_flag  # noqa: E402
from mcp_runtime import diagnostics, jobs, runtime  # noqa: E402

_logger = logging.getLogger("llmLibrarian.mcp")

_DB_PATH = bootstrap.resolve_db_path(_ROOT)
_CONFIG_PATH = str(Path(os.environ.get("LLMLIBRARIAN_CONFIG", str(_ROOT / "archetypes.yaml"))).resolve())
_SERVER_STARTED_AT: str | None = None

if not Path(_DB_PATH).exists():
    print(
        f"[llmLibrarian WARNING] DB path does not exist: {_DB_PATH}\n"
        f"Set LLMLIBRARIAN_DB to your my_brain_db directory.",
        file=sys.stderr,
    )

from fastmcp import FastMCP  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, Response  # noqa: E402

# Names the tool bodies and tests bind to. Tests monkeypatch several of these,
# so they must stay module-level attributes of mcp_server.
_package_version = runtime.package_version_string
_release_chroma = jobs.release_chroma
_mcp_chroma_lock = jobs.mcp_chroma_lock
_compute_answer_confidence = diagnostics.compute_answer_confidence


def _collect_health_summary(*, include_audit: bool = False) -> dict:
    return diagnostics.collect_health_summary(_DB_PATH, include_audit=include_audit)


def _derive_recommended_actions(silos: list[dict], summary: dict) -> list[str]:
    return diagnostics.derive_recommended_actions(silos, summary)


def _db_missing_error() -> dict:
    return {
        "db_path": _DB_PATH,
        "db_exists": False,
        "error": (
            f"LLMLIBRARIAN_DB does not exist: {_DB_PATH}. "
            "Fix the MCP environment/config or create/re-index the DB before using knowledge retrieval."
        ),
    }


def _require_confirm(confirm: bool, what: str) -> dict | None:
    """Guard for every mutating tool. Returns a result dict to hand back, or None.

    A soft guard, not a permission boundary: it makes a destructive call
    deliberate rather than incidental. See docs/MCP_TAILSCALE_FUNNEL.md for the
    remote-exposure caveats.
    """
    if confirm:
        return None
    return {
        "status": "not_started",
        "message": f"Pass confirm=True to {what}",
    }


mcp = FastMCP(
    name="llmLibrarian",
    instructions=(
        "Use these tools when a task requires context from the user's personal knowledge base. "
        "At session start, call session_context(check_staleness=True) for roster + health summary + actions. "
        "If you only need a quick roster, call list_silos. Do not assume a silo's topic from its slug alone; names "
        "can drift or be reused, so verify with list_silos metadata and retrieved sources. "
        ""
        "If retrieval returns zero chunks with no error, cross-check list_silos before treating "
        "that as evidence of absence: chunks_count > 0, has_index_errors, or has_ingest_failures "
        "means the empty result may be an index/tool problem. Call session_context or health for diagnostics. "
        ""
        "For any task involving the user's past thinking, decisions, habits, or writing, "
        "call query_personal_knowledge before responding. Use multi_query_knowledge when "
        "a task needs multiple angles of context simultaneously. "
        ""
        "multi_query_knowledge caps merged output at max_total_chunks (default 50) — if "
        "truncated=True is returned, lower n_results or reduce the number of queries. "
        "Pass section= to scope retrieval to a document section. Pass doc_type= to "
        "restrict by file type (e.g. 'transcript', 'resume', 'tax_return', 'code'). "
        "For tax queries, query_personal_knowledge also returns a tax_ledger field with structured "
        "extracted values (AGI, total tax, W-2 boxes). Prefer tax_ledger over raw "
        "chunks for precise figures. "
        ""
        "Use inspect_silo to diagnose coverage gaps. Use watch_coverage to check whether "
        "registered sources have watcher jobs, but do not assume watchers are active. "
        "Use mcp_runtime_status when lock/process/runtime visibility is unclear (multiple MCP PIDs, stale pid lock, or transport confusion). "
        "Write tools (add_silo, trigger_reindex, repair_silo, update_file, remove_file) all "
        "require confirm=True. Use trigger_reindex after file changes or when list_silos shows an old updated "
        "timestamp for a silo with active source changes. Use repair_silo for Chroma "
        "corruption. Use health for server diagnostics. Use capabilities for supported "
        "file types."
    ),
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def query_personal_knowledge(
    query: str,
    silo: str | None = None,
    n_results: int = 40,
    section: str | None = None,
    doc_type: str | None = None,
) -> dict:
    """
    Use when: answering a content/meaning question from indexed files.
    Do not use when: the user wants filenames/date filtering only (`find_files`) or ranking diagnostics only (`explain_retrieval`).
    Pairs with: `session_context`/`list_silos` first, then optional `inspect_silo`/`health` if coverage looks weak.

    Call this when a task requires the user's personal context — past writing,
    decisions, reflections, or domain knowledge. Returns semantically ranked
    chunks from indexed silos.

    Specify silo to scope retrieval by slug or display name; call list_silos first
    rather than inferring a silo's domain from its slug. Returns chunks with text, score (0–1),
    confidence, section heading, source path, date, doc_type, and position.

    Pass section= to restrict to a document section.
    Pass doc_type= to restrict by file type.
    Intent routing is applied automatically.
    For tax queries, also returns a tax_ledger field with structured extracted values.
    Response includes answer_confidence and coverage_note to calibrate hedging.
    When no silo filter is passed, also returns chunks_by_silo grouped by silo.
    """
    from query.core import run_retrieve
    if not Path(_DB_PATH).is_dir():
        return {**_db_missing_error(), "chunks": []}
    try:
        with _mcp_chroma_lock("query_personal_knowledge"):
            result = run_retrieve(
                query=query,
                silo=silo,
                n_results=n_results,
                section=section,
                doc_type=doc_type,
                db_path=_DB_PATH,
                config_path=_CONFIG_PATH,
            )
            chunks = result.get("chunks", [])

            # Answer-level confidence signal
            conf_level, conf_score, coverage_note = _compute_answer_confidence(chunks)
            result["answer_confidence"] = conf_level
            result["answer_confidence_score"] = conf_score
            result["coverage_note"] = coverage_note

            # Retrieval signal summary — helps LLM calibrate how to weight chunks
            lexical_hits = sum(1 for c in chunks if (c.get("_signals") or {}).get("lexical_rank") is not None)
            vector_hits = sum(1 for c in chunks if (c.get("_signals") or {}).get("vector_rank") is not None)
            result["lexical_hit_count"] = lexical_hits
            result["vector_hit_count"] = vector_hits

            # Cross-silo grouping (only when unscoped)
            if not silo and chunks:
                by_silo: dict[str, list] = {}
                for c in chunks:
                    by_silo.setdefault(c.get("silo", ""), []).append(c)
                result["chunks_by_silo"] = by_silo

            return {"db_path": _DB_PATH, **result}
    except Exception as e:
        return {"db_path": _DB_PATH, "error": f"{type(e).__name__}: {e}", "chunks": []}
    finally:
        _release_chroma()


@mcp.tool()
def multi_query_knowledge(
    queries: list[str],
    silo: str | None = None,
    n_results: int = 20,
    section: str | None = None,
    doc_type: str | None = None,
    max_total_chunks: int = 50,
) -> dict:
    """
    Use when: one task needs multiple retrieval angles merged in one response.
    Do not use when: a single focused query is enough (`query_personal_knowledge`) or when you need silo discovery (`session_context`/`list_silos`).
    Pairs with: `session_context` first; follow up with `query_personal_knowledge` if truncation hides details.

    Call this when a task needs multiple angles of context from personal knowledge.
    Fire multiple semantic queries and return merged, deduplicated chunks ranked
    by best score. Use when a topic requires several retrieval angles — for example,
    to cover the user's thinking on related topics in one call instead of many.
    Each chunk is tagged with the query that retrieved it. Pass section= to restrict
    all queries to a document section. Pass doc_type= to restrict by file type
    (e.g. 'transcript', 'resume', 'tax_return', 'code', 'other').
    max_total_chunks caps the merged output (default 50) to avoid context overflow.
    If the cap is hit, response includes truncated=True — lower n_results or reduce queries.
    Response includes answer_confidence, answer_confidence_score, and coverage_note.
    """
    from query.core import run_retrieve
    if not Path(_DB_PATH).is_dir():
        return {**_db_missing_error(), "queries": queries, "total_chunks": 0, "chunks": []}
    seen: set[str] = set()
    all_chunks: list[dict] = []
    errors: list[str] = []
    for q in queries:
        try:
            with _mcp_chroma_lock("multi_query_knowledge"):
                res = run_retrieve(
                    query=q,
                    silo=silo,
                    n_results=n_results,
                    section=section,
                    doc_type=doc_type,
                    db_path=_DB_PATH,
                    config_path=_CONFIG_PATH,
                )
            for chunk in res.get("chunks", []):
                key = (chunk.get("text") or "")[:200]
                if key and key not in seen:
                    seen.add(key)
                    chunk["query"] = q
                    all_chunks.append(chunk)
        except Exception as e:
            errors.append(f"{q!r}: {type(e).__name__}: {e}")
    all_chunks.sort(key=lambda c: c.get("score") or 0, reverse=True)
    truncated = len(all_chunks) > max_total_chunks
    if truncated:
        all_chunks = all_chunks[:max_total_chunks]

    # Feature 6: answer-level confidence on merged results
    conf_level, conf_score, coverage_note = _compute_answer_confidence(all_chunks)

    _release_chroma()
    return {
        "db_path": _DB_PATH,
        "queries": queries,
        "total_chunks": len(all_chunks),
        "truncated": truncated,
        "answer_confidence": conf_level,
        "answer_confidence_score": conf_score,
        "coverage_note": coverage_note,
        "chunks": all_chunks,
        **({"errors": errors} if errors else {}),
    }


@mcp.tool()
def explain_retrieval(
    query: str,
    silo: str | None = None,
    n_results: int = 20,
) -> dict:
    """
    Use when: debugging retrieval quality (hybrid/vector signal breakdown).
    Do not use when: generating user-facing answers; use `query_personal_knowledge`.
    Pairs with: `query_personal_knowledge`, `inspect_silo`, `health`.

    Return a structured breakdown of how retrieval results were ranked for a query.
    Useful for diagnosing missed results, unexpected rankings, or low confidence answers
    before deciding to re-query with different parameters.

    Returns:
    - retrieval_method: 'hybrid' (vector + lexical RRF) or 'vector_only'
    - lexical_hit_count: chunks with lexical_rank set (exact-text match)
    - vector_only_chunk_count: chunks with no lexical_rank (semantic-only in this result set)
    - chunk_with_vector_rank_count: chunks that participated in vector ranking (includes hybrid rows)
    - vector_hit_count: deprecated alias for vector_only_chunk_count
    - ranked_chunks: each chunk with its _signals (vector_rank, lexical_rank, rrf_score),
      score, source, and a short text preview
    - signal_summary: plain-text explanation of what signals fired and why
    """
    from query.core import run_retrieve
    if not Path(_DB_PATH).is_dir():
        return {**_db_missing_error(), "query": query, "ranked_chunks": []}
    try:
        with _mcp_chroma_lock("explain_retrieval"):
            result = run_retrieve(
                query=query,
                silo=silo,
                n_results=n_results,
                db_path=_DB_PATH,
                config_path=_CONFIG_PATH,
            )
            chunks = result.get("chunks", [])
            method = result.get("retrieval_method", "unknown")

            lexical_hits = [c for c in chunks if (c.get("_signals") or {}).get("lexical_rank") is not None]
            vector_only_hits = [c for c in chunks if (c.get("_signals") or {}).get("lexical_rank") is None]
            with_vector_rank = sum(
                1 for c in chunks if (c.get("_signals") or {}).get("vector_rank") is not None
            )

            ranked_chunks = []
            for c in chunks:
                sig = c.get("_signals") or {}
                ranked_chunks.append({
                    "rank": c.get("rank"),
                    "score": c.get("score"),
                    "source": c.get("source", ""),
                    "silo": c.get("silo", ""),
                    "text_preview": (c.get("text") or "")[:200],
                    "vector_rank": sig.get("vector_rank"),
                    "lexical_rank": sig.get("lexical_rank"),
                    "rrf_score": sig.get("rrf_score"),
                })

            # Build human-readable signal summary
            if method == "hybrid":
                summary_parts = [
                    f"Hybrid retrieval fired: {len(lexical_hits)} chunk(s) had exact-text matches (lexical_rank set); "
                    f"{with_vector_rank} chunk(s) have vector_rank (semantic ranking); "
                    f"{len(vector_only_hits)} chunk(s) are semantic-only in this set (no lexical_rank).",
                ]
                if lexical_hits:
                    top_lex = lexical_hits[0]
                    summary_parts.append(
                        f"Top lexical hit (rank {top_lex['rank']}): {top_lex.get('source','?')} "
                        f"— score {top_lex.get('score')}"
                    )
            else:
                summary_parts = [
                    f"Vector-only retrieval: no exact-text terms were extracted from the query or lexical search returned no results. "
                    f"All {len(chunks)} chunk(s) matched semantically."
                ]

            return {
                "db_path": _DB_PATH,
                "query": query,
                "intent": result.get("intent"),
                "retrieval_method": method,
                "lexical_hit_count": len(lexical_hits),
                "vector_only_chunk_count": len(vector_only_hits),
                "chunk_with_vector_rank_count": with_vector_rank,
                "vector_hit_count": len(vector_only_hits),
                "signal_summary": " ".join(summary_parts),
                "ranked_chunks": ranked_chunks,
            }
    except Exception as e:
        return {"db_path": _DB_PATH, "error": f"{type(e).__name__}: {e}", "ranked_chunks": []}
    finally:
        _release_chroma()


@mcp.tool()
def watch_coverage() -> dict:
    """
    Use when: checking whether pal bookmarks map to daemon watch jobs/services.
    Do not use when: diagnosing index corruption/staleness (`session_context`, `health`, `trigger_reindex`, `repair_silo`).
    Pairs with: `list_silos` for source-to-silo sanity checks.

    Read-only: pal bookmarks (~/.pal/registry.json) vs indexed silos vs derived watch jobs.
    Returns bookmark rows (path, resolved_path, silo_slug, path_exists, indexed, would_watch),
    watch_jobs (what `pal daemon` would sync), service unit file presence when daemon is installed,
    warnings (skipped bookmarks), and indexed_not_bookmarked (llmli silos with no matching bookmark path).
    Does not start watchers or modify launchd/systemd.
    Unrelated to ChromaDB health: for corrupt/zero-chunk index issues use `repair_silo`; for stale content after edits use `trigger_reindex`.
    """
    from operations import op_watch_coverage
    return op_watch_coverage(_DB_PATH)


@mcp.tool()
def list_silos(check_staleness: bool = False) -> dict:
    """
    Use when: you need a live roster of registered silos before retrieval.
    Do not use when: you also need health diagnostics/action guidance — use `session_context`.
    Pairs with: `query_personal_knowledge`, `multi_query_knowledge`, `trigger_reindex`.

    List all indexed silos. Returns db_path (the database this server is using),
    db_exists (false means LLMLIBRARIAN_DB is misconfigured), silo_count, and
    a silos array with slug, display name, path, file count, chunk count, last-indexed
    timestamp, and doc_type_breakdown (counts by category: pdf/code/docx/xlsx/pptx/other).
    Use slugs with `query_personal_knowledge`/`multi_query_knowledge` to scope queries.
    Pass check_staleness=True to also get is_stale, stale_file_count, and
    newest_source_mtime_iso per silo (walks source directory — may be slow for large silos).
    """
    if not Path(_DB_PATH).is_dir():
        return {**_db_missing_error(), "silo_count": 0, "silos": []}
    from operations import op_list_silos
    return op_list_silos(_DB_PATH, check_staleness=check_staleness)


@mcp.tool()
def session_context(check_staleness: bool = True, include_audit: bool = False) -> dict:
    """
    Use when: starting a personal-knowledge task and you need one bootstrap call.
    Do not use when: you only need a quick silo roster (`list_silos`) or deep diagnostics (`health`).
    Pairs with: `query_personal_knowledge` / `multi_query_knowledge` after reviewing actions.

    Returns:
    - silo roster (same shape as list_silos)
    - compact health summary (or richer summary when include_audit=True)
    - recommended_actions derived from stale/index/error signals
    - ready_for_retrieval boolean to help agents gate first query
    """
    if not Path(_DB_PATH).is_dir():
        return {
            **_db_missing_error(),
            "silo_count": 0,
            "silos": [],
            "recommended_actions": ["Fix LLMLIBRARIAN_DB for this MCP process before retrieval."],
            "ready_for_retrieval": False,
        }

    from operations import op_list_silos

    silos_result = op_list_silos(_DB_PATH, check_staleness=check_staleness)
    silos = silos_result.get("silos", []) if isinstance(silos_result, dict) else []
    summary = _collect_health_summary(include_audit=include_audit)
    actions = _derive_recommended_actions(silos, summary)
    hnsw = summary.get("hnsw_consistency") or {}
    hnsw_desynced = int(hnsw.get("desynced_count", 0) or 0) if isinstance(hnsw, dict) else 0
    ready_for_retrieval = bool(
        summary.get("db_exists")
        and (summary.get("chroma_transport") != "http" or summary.get("chroma_server_ok", True))
        and hnsw_desynced == 0
    )

    return {
        **silos_result,
        "health_summary": summary,
        "recommended_actions": actions,
        "ready_for_retrieval": ready_for_retrieval,
    }


@mcp.tool()
def inspect_silo(silo: str, top: int = 50) -> dict:
    """
    Use when: diagnosing coverage inside one silo (file-level chunk distribution).
    Do not use when: you need content retrieval (`query_personal_knowledge`) or stale diagnostics (`session_context`/`health`).
    Pairs with: `query_personal_knowledge` and `repair_silo`.

    Show per-file chunk counts for a silo. Returns total chunks (registry vs ChromaDB),
    a registry_match flag, and a files list sorted by chunk count descending.
    Useful to diagnose zero-chunk files (likely failed to parse), detect duplicate content,
    or verify coverage after indexing. top= limits how many files are returned (default 50).
    """
    if not Path(_DB_PATH).is_dir():
        return _db_missing_error()
    from operations import op_inspect_silo
    try:
        with _mcp_chroma_lock("inspect_silo"):
            result = op_inspect_silo(_DB_PATH, silo, top=top)
        return result
    except Exception as e:
        return {"db_path": _DB_PATH, "error": f"{type(e).__name__}: {e}"}
    finally:
        _release_chroma()


@mcp.tool()
def find_files(
    silos: list[str] | None = None,
    name_glob: str | None = None,
    date_start: str | None = None,
    date_end: str | None = None,
    date_field: str = "either",
    include_chunk_count: bool = False,
    limit: int = 50,
) -> dict:
    """
    Use when: the user asks for files by filename/date rather than document meaning.
    Do not use when: you need content-based answers (`query_personal_knowledge`/`multi_query_knowledge`).
    Pairs with: `query_personal_knowledge` after selecting a target file/silo.

    Find files by name and/or date against the manifest — no embeddings, no LLM.
    Use this for filename/date lookups like "today's journal entry" or "files from May 2026"
    where the user wants the *file*, not content discussion. Returns each hit with both
    name_date (parsed from filename) and mtime, plus a date_source field showing which
    one matched the filter ("name_date" | "mtime" | "both"). Filename precedence is the
    default because mtime is unreliable across syncs/restores. Set include_chunk_count=True
    to also report chunks per file (touches ChromaDB; otherwise this is metadata-only).
    Pass date_start / date_end as ISO strings (YYYY-MM-DD); same value for both means
    a single-day query. date_field accepts "name_date", "mtime", or "either" (default).
    """
    from datetime import date as _date

    from operations_find import op_find_files

    if not Path(_DB_PATH).is_dir():
        return _db_missing_error()

    def _parse(raw: str | None) -> _date | None:
        if not raw:
            return None
        return _date.fromisoformat(raw)

    try:
        lo = _parse(date_start)
        hi = _parse(date_end)
    except ValueError as e:
        return {"db_path": _DB_PATH, "error": f"invalid date: {e}"}

    if date_field not in ("name_date", "mtime", "either"):
        return {"db_path": _DB_PATH, "error": f"invalid date_field: {date_field}"}

    try:
        if include_chunk_count:
            with _mcp_chroma_lock("find_files"):
                result = op_find_files(
                    _DB_PATH,
                    silos=silos,
                    name_glob=name_glob,
                    date_start=lo,
                    date_end=hi,
                    date_field=date_field,  # type: ignore[arg-type]
                    include_chunk_count=True,
                    limit=int(limit),
                )
        else:
            result = op_find_files(
                _DB_PATH,
                silos=silos,
                name_glob=name_glob,
                date_start=lo,
                date_end=hi,
                date_field=date_field,  # type: ignore[arg-type]
                include_chunk_count=False,
                limit=int(limit),
            )
        return result
    except Exception as e:
        return {"db_path": _DB_PATH, "error": f"{type(e).__name__}: {e}"}
    finally:
        if include_chunk_count:
            _release_chroma()


@mcp.tool()
def trigger_reindex(silo: str, confirm: bool = False) -> dict:
    """
    Use when: a registered silo is stale after source-file edits.
    Do not use when: immediately after `add_silo` or when fixing corruption (`repair_silo`).
    Pairs with: `session_context`/`list_silos` to validate staleness first.

    Re-index a registered silo from its source path in a background thread.
    Only works on already-registered silos — cannot add new paths.
    Requires confirm=True to proceed (safety guard against accidental calls).
    Returns immediately; the reindex runs in-process (uses the shared ChromaDB client,
    no concurrent-write crash risk). Concurrent calls are serialized via a lock.
    Check list_silos() for updated timestamp after completion.
    This updates indexed chunks from files; it is not `repair_silo` (no wipe) and not `watch_coverage` (not about pal bookmarks/daemons).

    IMPORTANT: Do NOT call immediately after add_silo. add_silo already runs its own
    full index in a background thread — a redundant trigger_reindex will queue a second
    reindex that can corrupt the HNSW index and cause lock contention.
    Only call when list_silos shows a stale timestamp AND source files have changed.
    """
    if not Path(_DB_PATH).is_dir():
        return {"status": "error", **_db_missing_error()}
    guard = _require_confirm(confirm, "start the reindex. This will re-crawl the silo's source folder.")
    if guard:
        return guard

    from state import list_silos as _list_silos, resolve_silo_to_slug

    slug = resolve_silo_to_slug(_DB_PATH, silo)
    if slug is None:
        return {"status": "error", "error": f"silo not found: {silo!r}"}

    all_silos = _list_silos(_DB_PATH)
    info = next((s for s in all_silos if s.get("slug") == slug), None)
    if not info:
        return {"status": "error", "error": f"silo registry entry missing for slug: {slug}"}

    source_path = info.get("path", "")
    if not source_path:
        return {"status": "error", "error": "silo has no registered source path"}

    if not Path(source_path).exists():
        return {"status": "error", "error": f"source path does not exist: {source_path}"}

    from orchestration.ingest import IngestRequest

    job_key = jobs.start_ingest_job(
        key=slug,
        kind="trigger_reindex",
        silo=slug,
        request=IngestRequest(
            path=source_path,
            db_path=_DB_PATH,
            incremental=True,
        ),
    )
    return {
        "status": "started",
        "job_key": job_key,
        "silo": slug,
        "display_name": info.get("display_name", slug),
        "path": source_path,
        "message": (
            "Reindex running in background thread (in-process, serialized). "
            "Call list_silos() after a few minutes to see the updated timestamp. "
            f"Call health() and read last_background_reindex[{job_key!r}] for the outcome."
        ),
    }


@mcp.tool()
def repair_silo(silo: str, confirm: bool = False) -> dict:
    """
    Use when: index corruption/zero-chunk inconsistencies are suspected.
    Do not use when: routine freshness updates are needed (`trigger_reindex`).
    Pairs with: `session_context`/`health` before and after repair.

    Hard-wipe and fully re-index a silo to fix ChromaDB index corruption or 0-chunk inconsistencies.
    Runs inside this process (safe even when the MCP server has the DB open).
    Equivalent to `llmli repair <silo>` but avoids the concurrent-write crash that happens
    when a second process opens the same ChromaDB path.
    Requires confirm=True to proceed (safety guard).
    This is synchronous — it blocks until complete (may take a few minutes for large silos).
    Unrelated to pal auto-watch: for bookmarks/daemon job coverage use `watch_coverage` (read-only).
    """
    if not Path(_DB_PATH).is_dir():
        return {"status": "error", **_db_missing_error()}
    guard = _require_confirm(confirm, "start the repair. This wipes and fully re-indexes the silo.")
    if guard:
        return guard

    from operations import op_repair_silo
    try:
        with _mcp_chroma_lock("repair_silo"):
            result = op_repair_silo(_DB_PATH, silo, verbose=False)
        if result.get("status") == "completed":
            result["message"] = (
                f"Repair complete. {result['files_indexed']} file(s) re-indexed, "
                f"{result['failures']} failure(s)."
            )
        return result
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    finally:
        _release_chroma()


def _resolve_silo_under_path(silo: str, path: str) -> tuple[str | None, str | None, dict | None]:
    """Resolve silo slug and validate that path is inside the silo's registered source.

    Returns (slug, abs_path, error_dict). If error_dict is non-None, return it as the tool result.
    """
    from pathlib import Path as _Path
    from state import list_silos as _list_silos, resolve_silo_to_slug

    if not _Path(_DB_PATH).is_dir():
        return (None, None, {"status": "error", **_db_missing_error()})
    slug = resolve_silo_to_slug(_DB_PATH, silo)
    if slug is None:
        return (None, None, {"status": "error", "error": f"silo not found: {silo!r}"})
    info = next((s for s in _list_silos(_DB_PATH) if s.get("slug") == slug), None)
    if not info:
        return (None, None, {"status": "error", "error": f"silo registry entry missing for slug: {slug}"})
    silo_root = info.get("path") or ""
    if not silo_root:
        return (None, None, {"status": "error", "error": "silo has no registered source path"})
    abs_p = _Path(path).expanduser().resolve()
    silo_root_p = _Path(silo_root).resolve()
    try:
        abs_p.relative_to(silo_root_p)
    except ValueError:
        return (None, None, {
            "status": "error",
            "error": f"path is not under silo source: path={abs_p} silo_root={silo_root_p}",
        })
    return (slug, str(abs_p), None)


@mcp.tool()
def update_file(silo: str, path: str, confirm: bool = False) -> dict:
    """
    Use when: applying a single-file change inside an existing silo (watcher-style delta).
    Do not use when: onboarding/updating a full folder (`add_silo`) or fixing corruption (`repair_silo`).
    Pairs with: `remove_file` and `watch_coverage`.

    Re-index a single file in an already-registered silo. Used by `pal pull --watch`
    to push individual file changes through the shared Chroma client without
    spawning a separate writer process. Synchronous: blocks until the file is indexed.
    Path must resolve to a location under the silo's registered source folder.
    Requires confirm=True (safety guard, mirrors trigger_reindex / repair_silo).
    """
    guard = _require_confirm(confirm, "update the file.")
    if guard:
        return guard
    slug, abs_path, err = _resolve_silo_under_path(silo, path)
    if err:
        return err
    try:
        with _mcp_chroma_lock("update_file"):
            from ingest import update_single_file

            status, resolved = update_single_file(
                abs_path,
                db_path=_DB_PATH,
                silo_slug=slug,
                allow_cloud=True,  # path was already validated under the registered silo root
            )
        return {"status": status, "silo": slug, "path": resolved}
    except Exception as e:
        _logger.exception("update_file failed silo=%s path=%s", slug, abs_path)
        err_msg = f"{type(e).__name__}: {e}"
        from state import append_last_failures

        append_last_failures(_DB_PATH, [{"path": abs_path, "error": err_msg}])
        return {"status": "error", "error": err_msg}
    finally:
        _release_chroma()


@mcp.tool()
def remove_file(silo: str, path: str, confirm: bool = False) -> dict:
    """
    Use when: deleting one file from an already-registered silo.
    Do not use when: deleting/rebuilding an entire silo (`repair_silo` / remove+add flow).
    Pairs with: `update_file` and `watch_coverage`.

    Remove a single file's chunks and manifest entry from an already-registered silo.
    Used by `pal pull --watch` for delete events. Synchronous.
    Path must resolve to a location under the silo's registered source folder.
    Requires confirm=True (safety guard).
    """
    guard = _require_confirm(confirm, "remove the file.")
    if guard:
        return guard
    slug, abs_path, err = _resolve_silo_under_path(silo, path)
    if err:
        return err
    try:
        with _mcp_chroma_lock("remove_file"):
            from ingest import remove_single_file

            status, resolved = remove_single_file(
                abs_path,
                db_path=_DB_PATH,
                silo_slug=slug,
            )
        return {"status": status, "silo": slug, "path": resolved}
    except Exception as e:
        _logger.exception("remove_file failed silo=%s path=%s", slug, abs_path)
        err_msg = f"{type(e).__name__}: {e}"
        from state import append_last_failures

        append_last_failures(_DB_PATH, [{"path": abs_path, "error": err_msg}])
        return {"status": "error", "error": err_msg}
    finally:
        _release_chroma()


@mcp.tool()
def add_silo(
    path: str,
    silo: str | None = None,
    display_name: str | None = None,
    allow_cloud: bool = False,
    exclude_patterns: list[str] | None = None,
    full: bool = False,
    confirm: bool = False,
) -> dict:
    """
    Use when: indexing a new file/folder or refreshing a silo from its source path.
    Do not use when: you only need stale refresh after edits (`trigger_reindex`) or corruption repair (`repair_silo`).
    Pairs with: `session_context`/`list_silos` before and after indexing.

    Index a file or folder as a new silo (or update an existing one). Equivalent to `llmli add <path>`.
    silo: optional slug override (default: basename, slugified).
    display_name: optional human-readable name override.
    allow_cloud: set True to allow OneDrive/iCloud/Dropbox paths (blocked by default).
    full: set True to force a full non-incremental reindex (default: incremental).
    Requires confirm=True — this reads and indexes an arbitrary filesystem path.
    Returns immediately; indexing runs in a background thread (same process, serialized via lock).
    The returned job_key is the key to look up under health()["last_background_reindex"].

    IMPORTANT: Do NOT call trigger_reindex immediately after add_silo.
    add_silo already runs background indexing; a second immediate reindex can
    create lock contention and increase corruption risk.
    """
    from pathlib import Path as _Path

    guard = _require_confirm(confirm, "start add_silo. This indexes the given filesystem path.")
    if guard:
        return guard

    p = _Path(path).resolve()
    if not p.exists():
        return {"status": "error", "error": f"path does not exist: {path}"}
    if not p.is_dir() and not p.is_file():
        return {"status": "error", "error": f"path must be a file or directory: {path}"}

    # Best-guess key; the thread records the resolved slug in the outcome. The
    # caller gets this key back so it can find its own job even when the derived
    # slug differs from the path basename.
    from orchestration.ingest import IngestRequest

    job_key = jobs.start_ingest_job(
        key=silo if silo else p.name,
        kind="add_silo",
        silo=silo,
        request=IngestRequest(
            path=str(p),
            db_path=_DB_PATH,
            forced_silo_slug=silo,
            display_name=display_name,
            allow_cloud=allow_cloud,
            incremental=not full,
            exclude_patterns=exclude_patterns,
        ),
    )
    return {
        "status": "started",
        "job_key": job_key,
        "path": str(p),
        "message": (
            "Indexing running in background thread (in-process, serialized). "
            "Call list_silos() after a minute or two to confirm the silo appears. "
            f"Call health() and read last_background_reindex[{job_key!r}] for the outcome."
        ),
    }


@mcp.tool()
def mcp_runtime_status(verbose: bool = False) -> dict:
    """
    Use when: lock/process/runtime visibility is unclear for MCP or Chroma.
    Do not use when: you need content retrieval (`query_personal_knowledge`) or per-file coverage (`inspect_silo`).
    Pairs with: `session_context` for bootstrap and `health` for deep diagnostics.

    Returns a compact operator snapshot for agents:
    - mcp_http: pid lock visibility + mcp_server process multiplicity
    - chroma: transport and server reachability
    - jobs: active background jobs and last reindex outcomes
    - health_counts: query/ingest/HNSW counts
    - recommended_actions: short operational guidance
    """
    summary = _collect_health_summary(include_audit=False)
    mcp_lock = runtime.pid_lock_snapshot()
    proc = runtime.process_snapshot(verbose=verbose)

    mcp_http: dict = {**mcp_lock, **proc}
    chroma = {
        "transport": summary.get("chroma_transport"),
        "server_ok": summary.get("chroma_server_ok"),
        "host": summary.get("chroma_server_host"),
        "port": summary.get("chroma_server_port"),
    }
    job_state = diagnostics.compact_runtime_jobs(summary, verbose=verbose)
    hnsw = summary.get("hnsw_consistency") or {}
    health_counts = {
        "query_error_count": int(((summary.get("query_health") or {}).get("recent_error_count", 0) or 0)),
        "ingest_failure_count": int(((summary.get("ingest_failures") or {}).get("last_failure_count", 0) or 0)),
        "hnsw_desynced_count": int((hnsw.get("desynced_count", 0) if isinstance(hnsw, dict) else 0) or 0),
    }

    actions = _derive_recommended_actions([], summary)
    if bool(mcp_http.get("multiple_mcp_processes")):
        actions.append("Multiple mcp_server.py processes detected; stop orphan MCP processes and keep one service instance.")
    if bool(mcp_http.get("lock_file_exists")) and mcp_http.get("lock_holder_pid") and not bool(mcp_http.get("lock_holder_alive")):
        actions.append("MCP PID lock file points to a dead process; restart MCP service to refresh lock state.")
    actions = diagnostics.dedupe_lines(actions)

    out: dict = {
        "db_path": _DB_PATH,
        "db_exists": bool(summary.get("db_exists")),
        "mcp_http": mcp_http,
        "chroma": chroma,
        "jobs": job_state,
        "health_counts": health_counts,
        "recommended_actions": actions,
    }
    if verbose:
        out["summary_raw"] = summary
    return out


@mcp.tool()
def health() -> dict:
    """
    Use when: you need deep diagnostics (transport, query errors, ingest failures, HNSW, storage).
    Do not use when: you only need a routine session bootstrap (`session_context`).
    Pairs with: `session_context`, `repair_silo`, `trigger_reindex`.

    Diagnostic check. Returns db_path, db_exists, embedding model, Python version,
    and on-disk Chroma layout stats (including HNSW link_lists.bin bloat detection).
    Call this first if tools are failing, the disk is filling, or Python keeps spawning.
    """
    return _collect_health_summary(include_audit=True)


@mcp.tool()
def capabilities() -> str:
    """
    Use when: checking supported file types/extractors or smoke-testing MCP connectivity.
    Do not use when: diagnosing runtime/index health (`session_context`/`health`).
    Pairs with: `add_silo`.

    Return a plain-text report of all supported file types and extractors.
    No Ollama call required. Useful as a connectivity smoke test.
    """
    from ingest import get_capabilities_text
    return get_capabilities_text()


@mcp.resource("silos://list")
def resource_silos() -> str:
    """Read-only JSON snapshot of all registered silos. May be stale — call list_silos tool for live data."""
    import json
    if not Path(_DB_PATH).is_dir():
        return json.dumps({**_db_missing_error(), "silos": []}, indent=2)
    from state import list_silos as _list_silos
    return json.dumps({
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "silos": _list_silos(_DB_PATH),
    }, indent=2)


@mcp.resource("silos://{slug}")
def get_silo(slug: str) -> str:
    """
    Fetch live silo metadata by slug (e.g. silos://much-thinks, silos://hot_seat).
    Returns JSON with silo details: slug, display_name, path, file_count, chunk_count,
    last_indexed, and doc_type_breakdown. Resolved dynamically at request time.
    """
    import json
    if not Path(_DB_PATH).is_dir():
        return json.dumps(_db_missing_error(), indent=2)
    from state import list_silos as _list_silos
    all_silos = _list_silos(_DB_PATH)
    silo_info = next((s for s in all_silos if s.get("slug") == slug), None)
    if silo_info is None:
        raise ValueError(f"silo not found: {slug}")
    return json.dumps(silo_info, indent=2)


def _healthz_payload() -> dict:
    """Liveness payload for supervisors, reverse proxies, and the CLI's
    embedded-write guard.

    ``chroma_transport`` is what lets another process decide whether writing to
    this DB directly is safe, and ``transport`` makes the deployment mode an
    observable fact rather than something inferred from config on disk.

    ``db_path`` is an absolute filesystem path, so it is withheld when this
    server is both reachable off-host and unauthenticated — /healthz has no auth
    check of its own and may be published (see scripts/publish_mcp_funnel.sh).
    """
    from chroma_client import chroma_transport_mode

    transport = os.environ.get("LLMLIBRARIAN_MCP_TRANSPORT", "stdio").strip().lower()
    payload = {
        "ok": True,
        "service": "llmLibrarian-mcp",
        "version": _package_version(),
        "transport": transport,
        "chroma_transport": chroma_transport_mode(),
        "db_exists": Path(_DB_PATH).exists(),
        "started_at": _SERVER_STARTED_AT,
    }
    exposed = runtime.is_loopback_bind() or env_flag("LLMLIBRARIAN_MCP_REQUIRE_AUTH", False)
    if exposed:
        payload["db_path"] = _DB_PATH
    else:
        payload["db_path_withheld"] = (
            "bound off-loopback without LLMLIBRARIAN_MCP_REQUIRE_AUTH"
        )
    return payload


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(_: Request) -> Response:
    """Simple liveness check for process supervisors and reverse proxies."""
    return JSONResponse(_healthz_payload())


def _startup_banner(transport: str) -> str:
    from chroma_client import chroma_transport_mode

    return (
        f"[llmLibrarian] mcp transport={transport} "
        f"chroma={chroma_transport_mode()} db={_DB_PATH}"
    )


if __name__ == "__main__":
    transport = runtime.resolve_transport()

    # Acquire single-instance lock for persistent HTTP server processes.
    # Stdio processes are ephemeral (one per host conversation) and must not
    # acquire this lock — they'd block each other needlessly. mcp_runtime_status
    # scans /proc so stdio servers are still visible.
    if transport != "stdio":
        runtime.acquire_server_lock()
        _SERVER_STARTED_AT = datetime.now(timezone.utc).isoformat()
        # stdout is the JSON-RPC stream under stdio; only announce on HTTP.
        print(_startup_banner(transport), file=sys.stderr, flush=True)

    auth_provider = runtime.auth_for_transport(transport)
    if auth_provider is not None:
        mcp.auth = auth_provider

    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport=transport,
            host=os.environ.get("LLMLIBRARIAN_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("LLMLIBRARIAN_MCP_PORT", "8765")),
            path=os.environ.get("LLMLIBRARIAN_MCP_PATH", "/mcp"),
            log_level=os.environ.get("LLMLIBRARIAN_MCP_LOG_LEVEL", "warning"),
            stateless_http=env_flag("LLMLIBRARIAN_MCP_STATELESS_HTTP", True),
        )
