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
        "Use `list_silos` to discover silo slugs before scoping a query. "
        "Call `health` first if tools are failing — it reports db and model status. "
        "Use `capabilities` to see supported file types."
    ),
)


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
    return {
        "db_path": _DB_PATH,
        "queries": queries,
        "total_chunks": len(all_chunks),
        "chunks": all_chunks,
        **({"errors": errors} if errors else {}),
    }


@mcp.tool()
def list_silos() -> dict:
    """
    List all indexed silos. Returns db_path (the database this server is using),
    db_exists (false means LLMLIBRARIAN_DB is misconfigured), silo_count, and
    a silos array with slug, display name, path, file count, chunk count, and
    last-indexed timestamp. Use slugs with `retrieve`/`retrieve_bulk` to scope queries.
    """
    from state import list_silos as _list_silos
    silos = _list_silos(_DB_PATH)
    return {
        "db_path": _DB_PATH,
        "db_exists": Path(_DB_PATH).exists(),
        "silo_count": len(silos),
        "silos": silos,
    }


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
