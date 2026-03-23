import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_DB_PATH = str(Path(os.environ.get("LLMLIBRARIAN_DB", str(_ROOT / "my_brain_db"))).resolve())
_CONFIG_PATH = str(Path(os.environ.get("LLMLIBRARIAN_CONFIG", str(_ROOT / "archetypes.yaml"))).resolve())

from fastmcp import FastMCP

mcp = FastMCP(
    name="llmLibrarian",
    instructions=(
        "Use `retrieve` to get raw document chunks and reason over them yourself — "
        "preferred for most queries. "
        "Use `ask` for quick lookups where Ollama pre-synthesis is acceptable. "
        "Use `list_silos` to discover available data collections. "
        "Use `capabilities` to see supported file types."
    ),
)


@mcp.tool()
def retrieve(query: str, silo: str | None = None, n_results: int = 12) -> dict:
    """
    Retrieve raw document chunks without LLM synthesis. Returns ranked chunks
    with text, relevance score, source path, date, doc type, and position metadata.
    Prefer this over `ask` when you want to reason over the evidence yourself —
    cross-referencing sources, weighing contradictions, or combining context.
    Intent routing is still applied automatically to govern retrieval scope.
    For deterministic intents (file structure, code stats, etc.) use `ask` instead.
    """
    from query.core import run_retrieve
    try:
        return run_retrieve(
            query=query,
            silo=silo,
            n_results=n_results,
            db_path=_DB_PATH,
            config_path=_CONFIG_PATH,
        )
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "chunks": []}


@mcp.tool()
def ask(query: str, silo: str | None = None, n_results: int = 12) -> str:
    """
    Query indexed personal documents using natural language.
    Intent routing (factual, aggregate, reflect, code stats, etc.) is automatic.
    Optionally scope to a specific silo by name or slug.
    """
    from query.core import run_ask
    try:
        return run_ask(
            archetype_id=None,
            query=query,
            silo=silo,
            n_results=n_results,
            db_path=_DB_PATH,
            config_path=_CONFIG_PATH,
            no_color=True,
            quiet=True,
        )
    except Exception as e:
        return f"[Error] {type(e).__name__}: {e}"


@mcp.tool()
def list_silos() -> list[dict]:
    """
    List all indexed silos with slug, display name, path, file count,
    chunk count, and last-indexed timestamp. Use slugs with `ask` to scope queries.
    """
    from state import list_silos as _list_silos
    return _list_silos(_DB_PATH)


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
