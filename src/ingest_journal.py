"""
Write-ahead pending marker for crash recovery.

Before _batch_add: write_pending(db_path, silo_slug)
After all state writes: clear_pending(db_path, silo_slug)
At top of run_add: check_pending(db_path) → force non-incremental if interrupted
"""

import json
from datetime import datetime, timezone
from pathlib import Path


def _pending_path(db_path: str, silo_slug: str) -> Path:
    safe = silo_slug.replace("/", "_").replace("\\", "_")
    return Path(db_path) / f"llmli_pending_{safe}.json"


def write_pending(db_path: str, silo_slug: str) -> None:
    """Record that an ingest is in progress. Call before _batch_add."""
    p = _pending_path(db_path, silo_slug)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"silo": silo_slug, "started_at": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
    except Exception:
        pass


def clear_pending(db_path: str, silo_slug: str) -> None:
    """Remove the pending marker after all state writes complete."""
    try:
        _pending_path(db_path, silo_slug).unlink(missing_ok=True)
    except Exception:
        pass


def check_pending(db_path: str) -> list[str]:
    """Return silo slugs with an interrupted (pending) ingest marker."""
    interrupted: list[str] = []
    try:
        for p in Path(db_path).glob("llmli_pending_*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                slug = data.get("silo", "")
                if slug:
                    interrupted.append(slug)
            except Exception:
                pass
    except Exception:
        pass
    return interrupted
