import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

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
        "`retrieve` responses include `answer_confidence` (high/medium/low), `answer_confidence_score`, and "
        "`coverage_note` — use these to calibrate how much to hedge your answer. When no silo filter is passed, "
        "`retrieve` also returns `chunks_by_silo` grouping results by silo for provenance reasoning. "
        "Call `health` first if tools are failing — it reports db and model status. "
        "Use `capabilities` to see supported file types."
    ),
)


# ---------------------------------------------------------------------------
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
# Helper: doc-type breakdown from language_stats.by_ext
# ---------------------------------------------------------------------------

_CODE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp",
    ".cs", ".rb", ".sh", ".swift", ".kt", ".scala", ".r", ".lua", ".pl", ".php",
}
_DOC_TYPE_MAP = {
    ".pdf": "pdf", ".docx": "docx", ".doc": "docx",
    ".xlsx": "xlsx", ".xls": "xlsx", ".csv": "xlsx",
    ".pptx": "pptx", ".ppt": "pptx",
}

def _doc_type_breakdown(by_ext: dict) -> dict:
    breakdown: dict[str, int] = {}
    for ext, count in (by_ext or {}).items():
        ext_lower = ext.lower()
        if ext_lower in _DOC_TYPE_MAP:
            cat = _DOC_TYPE_MAP[ext_lower]
        elif ext_lower in _CODE_EXTS:
            cat = "code"
        else:
            cat = "other"
        breakdown[cat] = breakdown.get(cat, 0) + count
    return breakdown


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

        # Feature 6: answer-level confidence signal
        conf_level, conf_score, coverage_note = _compute_answer_confidence(chunks)
        result["answer_confidence"] = conf_level
        result["answer_confidence_score"] = conf_score
        result["coverage_note"] = coverage_note

        # Feature 7: cross-silo grouping (only when unscoped)
        if not silo and chunks:
            by_silo: dict[str, list] = {}
            for c in chunks:
                by_silo.setdefault(c.get("silo", ""), []).append(c)
            result["chunks_by_silo"] = by_silo

        return {"db_path": _DB_PATH, **result}
    except Exception as e:
        return {"db_path": _DB_PATH, "error": f"{type(e).__name__}: {e}", "chunks": []}


@mcp.tool()
def retrieve_bulk(
    queries: list[str],
    silo: str | None = None,
    n_results: int = 20,
    section: str | None = None,
    doc_type: str | None = None,
) -> dict:
    """
    Fire multiple semantic queries and return merged, deduplicated chunks ranked
    by best score. Use when a topic requires several retrieval angles — for example,
    to cover all risk factor categories in a 10-K with one call instead of many.
    Each chunk is tagged with the query that retrieved it. Pass section= to restrict
    all queries to a specific document section. Pass doc_type= to restrict all queries
    to a specific document type (e.g. 'transcript', 'resume', 'pdf', 'code', 'other').
    Response includes answer_confidence, answer_confidence_score, and coverage_note.
    """
    from query.core import run_retrieve
    seen: set[str] = set()
    all_chunks: list[dict] = []
    errors: list[str] = []
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

    # Feature 6: answer-level confidence on merged results
    conf_level, conf_score, coverage_note = _compute_answer_confidence(all_chunks)

    return {
        "db_path": _DB_PATH,
        "queries": queries,
        "total_chunks": len(all_chunks),
        "answer_confidence": conf_level,
        "answer_confidence_score": conf_score,
        "coverage_note": coverage_note,
        "chunks": all_chunks,
        **({"errors": errors} if errors else {}),
    }


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
    from state import list_silos as _list_silos
    silos = _list_silos(_DB_PATH)

    for s in silos:
        # Feature 4: doc type breakdown
        by_ext = (s.get("language_stats") or {}).get("by_ext") or {}
        s["doc_type_breakdown"] = _doc_type_breakdown(by_ext)

        # Feature 1: staleness detection (opt-in)
        if check_staleness:
            _inject_staleness(s)

    return {
        "db_path": _DB_PATH,
        "db_exists": Path(_DB_PATH).exists(),
        "silo_count": len(silos),
        "silos": silos,
    }


def _inject_staleness(silo_entry: dict) -> None:
    """Mutate silo_entry in-place with staleness fields."""
    from datetime import datetime, timezone

    source_path = silo_entry.get("path", "")
    updated_iso = silo_entry.get("updated", "")

    if not source_path or not Path(source_path).exists():
        silo_entry["is_stale"] = None
        silo_entry["staleness_note"] = "source path not accessible"
        return

    # Parse last-indexed timestamp
    try:
        last_indexed = datetime.fromisoformat(updated_iso).timestamp()
    except Exception:
        silo_entry["is_stale"] = None
        silo_entry["staleness_note"] = "cannot parse updated timestamp"
        return

    stale_count = 0
    newest_stale_mtime: float | None = None

    try:
        for dirpath, _dirs, filenames in os.walk(source_path):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                except OSError:
                    continue
                if mtime > last_indexed:
                    stale_count += 1
                    if newest_stale_mtime is None or mtime > newest_stale_mtime:
                        newest_stale_mtime = mtime
    except Exception as e:
        silo_entry["is_stale"] = None
        silo_entry["staleness_note"] = f"walk error: {e}"
        return

    silo_entry["is_stale"] = stale_count > 0
    silo_entry["stale_file_count"] = stale_count
    if newest_stale_mtime is not None:
        silo_entry["newest_source_mtime_iso"] = datetime.fromtimestamp(
            newest_stale_mtime, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        silo_entry["newest_source_mtime_iso"] = None


@mcp.tool()
def inspect_silo(silo: str, top: int = 50) -> dict:
    """
    Show per-file chunk counts for a silo. Returns total chunks (registry vs ChromaDB),
    a registry_match flag, and a files list sorted by chunk count descending.
    Useful to diagnose zero-chunk files (likely failed to parse), detect duplicate content,
    or verify coverage after indexing. top= limits how many files are returned (default 50).
    """
    from state import list_silos as _list_silos, resolve_silo_to_slug
    from constants import LLMLI_COLLECTION
    import chromadb
    from chromadb.config import Settings

    slug = resolve_silo_to_slug(_DB_PATH, silo)
    if slug is None:
        return {"error": f"silo not found: {silo!r}"}

    all_silos = _list_silos(_DB_PATH)
    info = next((s for s in all_silos if s.get("slug") == slug), None)
    display = (info or {}).get("display_name", slug)
    path = (info or {}).get("path", "")
    total_registry = (info or {}).get("chunks_count", 0)

    try:
        client = chromadb.PersistentClient(path=_DB_PATH, settings=Settings(anonymized_telemetry=False))
        coll = client.get_or_create_collection(name=LLMLI_COLLECTION)
        result = coll.get(where={"silo": slug}, include=["metadatas"])
        metas = result.get("metadatas") or []
    except Exception as e:
        return {"error": f"ChromaDB error: {e}"}

    total_chroma = len(metas)

    # Aggregate chunks by source file
    by_source: dict[str, int] = {}
    source_to_hash: dict[str, str] = {}
    for m in metas:
        meta = m or {}
        src = meta.get("source") or "?"
        by_source[src] = by_source.get(src, 0) + 1
        if src not in source_to_hash and meta.get("file_hash"):
            source_to_hash[src] = meta["file_hash"]

    # Detect duplicate content (same file_hash, different paths)
    hash_to_sources: dict[str, list[str]] = {}
    for src, h in source_to_hash.items():
        if h:
            hash_to_sources.setdefault(h, []).append(src)

    sorted_files = sorted(by_source.items(), key=lambda x: -x[1])[:max(1, top)]
    files = [
        {
            "source": src,
            "chunk_count": count,
            "has_duplicate_content": bool(
                source_to_hash.get(src)
                and len(hash_to_sources.get(source_to_hash[src], [])) > 1
            ),
        }
        for src, count in sorted_files
    ]

    return {
        "slug": slug,
        "display_name": display,
        "path": path,
        "total_chunks_registry": total_registry,
        "total_chunks_chroma": total_chroma,
        "registry_match": total_registry == total_chroma,
        "total_files": len(by_source),
        "files_shown": len(files),
        "files": files,
    }


@mcp.tool()
def trigger_reindex(silo: str, confirm: bool = False) -> dict:
    """
    Re-index a registered silo from its source path in the background.
    Only works on already-registered silos — cannot add new paths.
    Requires confirm=True to proceed (safety guard against accidental calls).
    Returns immediately; the reindex runs as a background process.
    Check list_silos() for updated timestamp after completion.
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

    llmli_bin = str(_ROOT / ".venv" / "bin" / "llmli")
    if not Path(llmli_bin).exists():
        return {"status": "error", "error": f"llmli binary not found at {llmli_bin}"}

    import subprocess
    env = {**os.environ, "LLMLIBRARIAN_DB": _DB_PATH}
    try:
        proc = subprocess.Popen(
            [llmli_bin, "add", source_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        return {
            "status": "started",
            "pid": proc.pid,
            "silo": slug,
            "display_name": info.get("display_name", slug),
            "path": source_path,
            "message": "Reindex running in background. Call list_silos() after a few minutes to see the updated timestamp.",
        }
    except Exception as e:
        return {"status": "error", "error": f"failed to launch process: {e}"}


@mcp.tool()
def health() -> dict:
    """
    Diagnostic check. Returns db_path, db_exists, embedding model, and Python version.
    Call this first if tools are failing or returning unexpected results.
    """
    embedding_model = os.environ.get("LLMLIBRARIAN_EMBEDDING_MODEL", "all-mpnet-base-v2")
    embedding_kind = os.environ.get("LLMLIBRARIAN_EMBEDDING", "") or "sentence_transformer"
    return {
        "db_path": _DB_PATH,
        "db_exists": Path(_DB_PATH).exists(),
        "embedding_model": embedding_model,
        "embedding_kind": embedding_kind,
        "python_version": sys.version,
    }


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
