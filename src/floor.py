"""
Optional resource/summary print (DB size, reranker status). Used by index --mode.
"""
import os
from pathlib import Path
from typing import Any

def print_resources(
    db_path: str,
    mode: str = "normal",
    reranker_loaded: bool = False,
    no_color: bool = False,
) -> None:
    """Print DB path, size, and optional reranker status."""
    from style import dim
    p = Path(db_path)
    if p.exists():
        try:
            size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            size_mb = size / (1024 * 1024)
            print(dim(no_color, f"  DB: {p.resolve()} ({size_mb:.1f} MB)"))
        except Exception:
            print(dim(no_color, f"  DB: {p.resolve()}"))
    else:
        print(dim(no_color, f"  DB: {p.resolve()} (not yet created)"))
    if reranker_loaded:
        print(dim(no_color, "  Reranker: enabled"))
