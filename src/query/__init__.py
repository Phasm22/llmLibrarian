"""
query package — split from query_engine.py.
Re-exports public API so existing imports (from query import run_ask) continue to work.
"""
from constants import DB_PATH, DEFAULT_N_RESULTS, DEFAULT_MODEL
from query.core import run_ask, main

__all__ = ["run_ask", "main", "DB_PATH", "DEFAULT_N_RESULTS", "DEFAULT_MODEL"]
