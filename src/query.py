"""
Archetype-aware query: one collection per archetype, retrieval transparency.
Returns answer + "Answered by: <name>" + Sources (path, snippet, line/page).
"""
# Re-export from query engine so cli can import from query
from query_engine import run_ask, DB_PATH, DEFAULT_N_RESULTS, DEFAULT_MODEL

__all__ = ["run_ask", "DB_PATH", "DEFAULT_N_RESULTS", "DEFAULT_MODEL"]
