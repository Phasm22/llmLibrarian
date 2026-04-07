import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_logger = logging.getLogger("llmLibrarian.mcp")


def _resolve_db_path() -> str:
    """Resolve DB path, falling back gracefully when env vars contain unresolved templates."""
    raw = os.environ.get("LLMLIBRARIAN_DB", "")
    # Detect unresolved Claude Desktop template variables like ${user.db_path}
    if raw and not raw.startswith("${"):
        return str(Path(raw).resolve())

    # Try to read from Claude Desktop extension settings file
    _settings_candidates = [
        Path.home() / "Library/Application Support/Claude/Claude Extensions Settings/local.mcpb.tjm4.llmlibrarian.json",
    ]
    for settings_file in _settings_candidates:
        if settings_file.exists():
            try:
                import json
                cfg = json.loads(settings_file.read_text())
                db_path = cfg.get("userConfig", {}).get("db_path", "")
                if db_path and not db_path.startswith("${"):
                    return str(Path(db_path).resolve())
            except Exception:
                pass

    # Fall back: look for my_brain_db relative to script root or common locations
    fallback_candidates = [
        _ROOT / "my_brain_db",
        Path.home() / "llmLibrarian" / "my_brain_db",
    ]
    for candidate in fallback_candidates:
        if candidate.exists():
            return str(candidate.resolve())

    return str((_ROOT / "my_brain_db").resolve())


_DB_PATH = _resolve_db_path()
_CONFIG_PATH = str(Path(os.environ.get("LLMLIBRARIAN_CONFIG", str(_ROOT / "archetypes.yaml"))).resolve())

import threading

# Serializes concurrent trigger_reindex calls in-process (no subprocess races).
_reindex_lock = threading.Lock()

if not Path(_DB_PATH).exists():
    print(
        f"[llmLibrarian WARNING] DB path does not exist: {_DB_PATH}\n"
        f"Set LLMLIBRARIAN_DB to your my_brain_db directory.",
        file=sys.stderr,
    )

from fastmcp import FastMCP

mcp = FastMCP(
    name="llmLibrarian",
    instructions=(
        "ALWAYS use `retrieve` for document questions — returns raw chunks for you to reason over. "
        "Use `retrieve_bulk` when a topic needs multiple retrieval angles (e.g. all risk factor categories). "
        "`retrieve_bulk` caps merged output at `max_total_chunks` (default 50) to prevent context overflow — if `truncated=True` is returned, lower `n_results` or reduce the number of queries. "
        "Pass `section=` to scope retrieval to a document section (e.g. 'Item 1A', 'Risk Factors'). "
        "Pass `doc_type=` to restrict retrieval to a specific document type — useful when you want only "
        "resumes, transcripts, tax returns, or code files (e.g. doc_type='transcript', 'resume', 'tax_return', 'code', 'other'). "
        "For tax questions (`TAX_QUERY` intent), `retrieve` also returns a `tax_ledger` field alongside chunks — "
        "this contains structured, ingest-time extracted values (AGI, total tax, W-2 boxes, etc.) with exact amounts. "
        "Prefer `tax_ledger` rows over raw chunk text for precise tax figures; fall back to chunks for context. "
        "Use `list_silos` to discover silo slugs before scoping a query. Pass `check_staleness=True` to also get "
        "is_stale/stale_file_count per silo (compares source file mtimes against last index time). "
        "Use `inspect_silo` to see per-file chunk counts — useful to diagnose coverage gaps or zero-chunk files. "
        "Use `trigger_reindex(silo=..., confirm=True)` to re-index a registered silo in the background when "
        "source files have changed. Only works on already-registered paths. "
        "WARNING: reindex/repair re-crawl from disk — missing files at the original path are dropped from the silo. "
        "Use `repair_silo(silo=..., confirm=True)` for Chroma corruption ('Error finding id', 0-chunk silo); use `trigger_reindex` for normal refresh after file changes. "
        "`retrieve` responses include `answer_confidence` (high/medium/low), `answer_confidence_score`, and "
        "`coverage_note` — use these to calibrate how much to hedge your answer. When no silo filter is passed, "
        "`retrieve` also returns `chunks_by_silo` grouping results by silo for provenance reasoning. "
        "Each chunk includes `_signals` with retrieval attribution: `vector_rank` (position in semantic results), "
        "`lexical_rank` (position in exact-text results, null if not matched), and `rrf_score` (combined score). "
        "When `lexical_rank` is non-null, the chunk matched the query text exactly — weight it highly for precise "
        "factual lookups (IDs, error codes, config keys, exact names). When only `vector_rank` is set, the chunk "
        "matched semantically. The top-level `retrieval_method` field ('hybrid' or 'vector_only') tells you which "
        "path fired overall; `lexical_hit_count` counts chunks with an exact-text match (`lexical_rank`); "
        "`vector_hit_count` counts chunks that appeared in semantic ranking (`vector_rank`). "
        "For hybrid diagnostics, prefer `explain_retrieval` (`vector_only_chunk_count` vs `chunk_with_vector_rank_count`). "
        "Use `explain_retrieval` to get a structured breakdown of why results ranked as they did — useful for "
        "diagnosing missed results or unexpected rankings before re-querying. "
        "Use `add_silo(path=...)` to index a path as a silo (equivalent to `llmli add`). "
        "Prefer this over the CLI — no PYTHONPATH setup required. path may be a directory or a single file (same rules as `llmli add`). "
        "Use `watch_coverage` when the user asks what is auto-watched vs indexed-only — read-only summary of pal bookmarks, derived daemon jobs, and silos not in bookmarks. "
        "Do not substitute `watch_coverage` for index problems: it does not read or fix ChromaDB. "
        "`repair_silo` = hard reset of vector index data for one silo when the DB/registry is corrupt or wildly inconsistent (then full re-index from disk). "
        "`trigger_reindex` = incremental re-crawl from disk when files changed; not a bookmark/daemon diagnostic. "
        "Call `health` first if tools are failing — it reports db and model status. "
        "Use `capabilities` to see supported file types."
    ),
)


# ---------------------------------------------------------------------------
# Serializes ALL ChromaDB client use across concurrent MCP tool calls (in-process).
# Cross-process safety uses flock in src/chroma_lock.py (shared reads, exclusive writes).
# ChromaDB's Rust HNSW writer is not safe for concurrent use — simultaneous
# reads and background reindex writes corrupted link_lists.bin to 680 GB.
_chroma_lock = threading.Lock()

# Last background reindex outcome per silo (for health / debugging).
_reindex_outcome_lock = threading.Lock()
_last_reindex_outcome: dict[str, dict] = {}


def _release_chroma() -> None:
    pass  # kept for call-site compatibility; no-op — singleton client persists


# Helper: answer-level confidence signal
# ---------------------------------------------------------------------------

def _compute_answer_confidence(chunks: list[dict]) -> tuple[str, float, str]:
    scores = [c["score"] for c in chunks[:10] if c.get("score") is not None]
    if not scores:
        return "low", 0.0, "no chunks returned"
    top = scores[0]
    mean = sum(scores) / len(scores)
    high_count = sum(1 for s in scores if s >= 0.5)
    sources = len({c.get("source", "") for c in chunks[:10]})
    if top >= 0.6 and mean >= 0.4:
        level = "high"
        note = f"{high_count} high-confidence chunk(s) from {sources} source(s)"
    elif top >= 0.4:
        level = "medium"
        note = f"moderate match — top score {top:.2f}, {sources} source(s)"
    else:
        level = "low"
        note = "sparse match — consider a broader or rephrased query"
    return level, round(mean, 4), note


# ---------------------------------------------------------------------------
# Helper: doc-type breakdown (delegated to operations module)
# ---------------------------------------------------------------------------

from operations import _doc_type_breakdown, _inject_staleness


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def retrieve(
    query: str,
    silo: str | None = None,
    n_results: int = 40,
    section: str | None = None,
    doc_type: str | None = None,
) -> dict:
    """
    Retrieve raw document chunks without LLM synthesis. Returns ranked chunks with
    text, score (0–1), confidence (high/medium/low), section heading, source path,
    date, doc type, and position metadata.
    Pass section= to restrict results to a specific document section
    (e.g. section='Item 1A' or section='Risk Factors').
    Pass doc_type= to restrict to a specific document type
    (e.g. doc_type='transcript', 'resume', 'pdf', 'code', 'other').
    Intent routing is applied automatically to govern retrieval scope.
    Response includes answer_confidence (high/medium/low), answer_confidence_score,
    and coverage_note to help calibrate how much to hedge the answer.
    When no silo filter is passed, also returns chunks_by_silo grouping results by silo.
    """
    from query.core import run_retrieve
    try:
        with _chroma_lock:
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
def retrieve_bulk(
    queries: list[str],
    silo: str | None = None,
    n_results: int = 20,
    section: str | None = None,
    doc_type: str | None = None,
    max_total_chunks: int = 50,
) -> dict:
    """
    Fire multiple semantic queries and return merged, deduplicated chunks ranked
    by best score. Use when a topic requires several retrieval angles — for example,
    to cover all risk factor categories in a 10-K with one call instead of many.
    Each chunk is tagged with the query that retrieved it. Pass section= to restrict
    all queries to a specific document section. Pass doc_type= to restrict all queries
    to a specific document type (e.g. 'transcript', 'resume', 'pdf', 'code', 'other').
    max_total_chunks caps the merged output (default 50) to avoid context window overflow.
    If the cap is hit, response includes truncated=True — lower n_results or reduce queries.
    Response includes answer_confidence, answer_confidence_score, and coverage_note.
    """
    from query.core import run_retrieve
    seen: set[str] = set()
    all_chunks: list[dict] = []
    errors: list[str] = []
    with _chroma_lock:
        for q in queries:
            try:
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
    try:
        with _chroma_lock:
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
    List all indexed silos. Returns db_path (the database this server is using),
    db_exists (false means LLMLIBRARIAN_DB is misconfigured), silo_count, and
    a silos array with slug, display name, path, file count, chunk count, last-indexed
    timestamp, and doc_type_breakdown (counts by category: pdf/code/docx/xlsx/pptx/other).
    Use slugs with `retrieve`/`retrieve_bulk` to scope queries.
    Pass check_staleness=True to also get is_stale, stale_file_count, and
    newest_source_mtime_iso per silo (walks source directory — may be slow for large silos).
    """
    from operations import op_list_silos
    return op_list_silos(_DB_PATH, check_staleness=check_staleness)


@mcp.tool()
def inspect_silo(silo: str, top: int = 50) -> dict:
    """
    Show per-file chunk counts for a silo. Returns total chunks (registry vs ChromaDB),
    a registry_match flag, and a files list sorted by chunk count descending.
    Useful to diagnose zero-chunk files (likely failed to parse), detect duplicate content,
    or verify coverage after indexing. top= limits how many files are returned (default 50).
    """
    from operations import op_inspect_silo
    with _chroma_lock:
        result = op_inspect_silo(_DB_PATH, silo, top=top)
    _release_chroma()
    return result


@mcp.tool()
def trigger_reindex(silo: str, confirm: bool = False) -> dict:
    """
    Re-index a registered silo from its source path in a background thread.
    Only works on already-registered silos — cannot add new paths.
    Requires confirm=True to proceed (safety guard against accidental calls).
    Returns immediately; the reindex runs in-process (uses the shared ChromaDB client,
    no concurrent-write crash risk). Concurrent calls are serialized via a lock.
    Check list_silos() for updated timestamp after completion.
    This updates indexed chunks from files; it is not `repair_silo` (no wipe) and not `watch_coverage` (not about pal bookmarks/daemons).
    """
    if not confirm:
        return {
            "status": "not_started",
            "message": "Pass confirm=True to start the reindex. This will re-crawl the silo's source folder.",
        }

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

    def _run_reindex() -> None:
        err: str | None = None
        try:
            with _reindex_lock:
                with _chroma_lock:
                    from ingest import run_add

                    run_add(path=source_path, db_path=_DB_PATH, incremental=True)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _logger.exception("trigger_reindex failed silo=%s path=%s", slug, source_path)
            traceback.print_exc(file=sys.stderr)
        finally:
            finished = datetime.now(timezone.utc).isoformat()
            rec = {
                "silo": slug,
                "path": source_path,
                "finished_at": finished,
                "ok": err is None,
                **({"error": err} if err else {}),
            }
            with _reindex_outcome_lock:
                _last_reindex_outcome[slug] = rec

    t = threading.Thread(target=_run_reindex, daemon=True)
    t.start()
    return {
        "status": "started",
        "silo": slug,
        "display_name": info.get("display_name", slug),
        "path": source_path,
        "message": (
            "Reindex running in background thread (in-process, serialized). "
            "Call list_silos() after a few minutes to see the updated timestamp. "
            "Call health() for last_background_reindex status after completion."
        ),
    }


@mcp.tool()
def repair_silo(silo: str, confirm: bool = False) -> dict:
    """
    Hard-wipe and fully re-index a silo to fix ChromaDB index corruption or 0-chunk inconsistencies.
    Runs inside this process (safe even when the MCP server has the DB open).
    Equivalent to `llmli repair <silo>` but avoids the concurrent-write crash that happens
    when a second process opens the same ChromaDB path.
    Requires confirm=True to proceed (safety guard).
    This is synchronous — it blocks until complete (may take a few minutes for large silos).
    Unrelated to pal auto-watch: for bookmarks/daemon job coverage use `watch_coverage` (read-only).
    """
    if not confirm:
        return {
            "status": "not_started",
            "message": "Pass confirm=True to start the repair. This wipes and fully re-indexes the silo.",
        }

    from operations import op_repair_silo
    try:
        with _chroma_lock:
            result = op_repair_silo(_DB_PATH, silo, verbose=False)
        if result.get("status") == "completed":
            result["message"] = (
                f"Repair complete. {result['files_indexed']} file(s) re-indexed, "
                f"{result['failures']} failure(s)."
            )
        return result
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def add_silo(
    path: str,
    silo: str | None = None,
    display_name: str | None = None,
    allow_cloud: bool = False,
    full: bool = False,
) -> dict:
    """
    Index a file or folder as a new silo (or update an existing one). Equivalent to `llmli add <path>`.
    silo: optional slug override (default: basename, slugified).
    display_name: optional human-readable name override.
    allow_cloud: set True to allow OneDrive/iCloud/Dropbox paths (blocked by default).
    full: set True to force a full non-incremental reindex (default: incremental).
    Runs synchronously — large trees may take a while. Returns files_indexed and failure count.
    """
    from pathlib import Path as _Path

    p = _Path(path)
    if not p.exists():
        return {"status": "error", "error": f"path does not exist: {path}"}
    if not p.is_dir() and not p.is_file():
        return {"status": "error", "error": f"path must be a file or directory: {path}"}

    try:
        from orchestration.ingest import IngestRequest, run_ingest

        with _chroma_lock:
            result = run_ingest(
                IngestRequest(
                    path=path,
                    db_path=_DB_PATH,
                    forced_silo_slug=silo,
                    display_name=display_name,
                    allow_cloud=allow_cloud,
                    incremental=not full,
                )
            )
        files_ok, n_failures = result.files_indexed, result.failures
        from state import resolve_silo_by_path, resolve_silo_to_slug
        slug = (
            resolve_silo_to_slug(_DB_PATH, silo) if silo
            else resolve_silo_by_path(_DB_PATH, _Path(path).resolve())
        )
        return {
            "status": "ok",
            "silo": slug,
            "path": str(_Path(path).resolve()),
            "files_indexed": files_ok,
            "failures": n_failures,
            "message": f"Indexed {files_ok} file(s) into silo '{slug}'" + (f" with {n_failures} failure(s). Run `llmli log` for details." if n_failures else "."),
        }
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def health() -> dict:
    """
    Diagnostic check. Returns db_path, db_exists, embedding model, Python version,
    and on-disk Chroma layout stats (including HNSW link_lists.bin bloat detection).
    Call this first if tools are failing, the disk is filling, or Python keeps spawning.
    """
    from operations import op_db_storage_summary

    embedding_model = os.environ.get("LLMLIBRARIAN_EMBEDDING_MODEL", "all-mpnet-base-v2")
    embedding_kind = os.environ.get("LLMLIBRARIAN_EMBEDDING", "") or "sentence_transformer"
    out: dict = {
        "db_path": _DB_PATH,
        "db_exists": Path(_DB_PATH).exists(),
        "embedding_model": embedding_model,
        "embedding_kind": embedding_kind,
        "python_version": sys.version,
    }
    if Path(_DB_PATH).is_dir():
        out["storage"] = op_db_storage_summary(_DB_PATH)
    with _reindex_outcome_lock:
        out["last_background_reindex"] = dict(_last_reindex_outcome)
    return out


@mcp.tool()
def capabilities() -> str:
    """
    Return a plain-text report of all supported file types and extractors.
    No Ollama call required. Useful as a connectivity smoke test.
    """
    from ingest import get_capabilities_text
    return get_capabilities_text()


@mcp.resource("silos://list")
def resource_silos() -> str:
    """Read-only JSON snapshot of all registered silos."""
    import json
    from state import list_silos as _list_silos
    return json.dumps(_list_silos(_DB_PATH), indent=2)


if __name__ == "__main__":
    mcp.run()  # STDIO transport by default
