"""
Write-ahead pending marker for crash recovery *and* live write visibility.

Before _batch_add: write_pending(db_path, silo_slug)
After all state writes: clear_pending(db_path, silo_slug)
At top of run_add: check_pending(db_path) → force non-incremental if interrupted

The marker does double duty. Historically it only answered "was a previous ingest
interrupted." It also answers "is a write happening *right now*", which read paths
need: a full rebuild deletes the silo's chunks before writing the new ones, so a
query landing in that window gets zero chunks and no error — indistinguishable
from "the document doesn't say that." Recording the writer's pid lets readers tell
the two cases apart:

    pid alive  → write in progress, results may be partial; retry
    pid dead   → interrupted ingest, next run self-heals with a full re-index
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

_PREFIX = "llmli_pending_"


def _pending_path(db_path: str, silo_slug: str) -> Path:
    safe = silo_slug.replace("/", "_").replace("\\", "_")
    return Path(db_path) / f"{_PREFIX}{safe}.json"


def write_pending(db_path: str, silo_slug: str, kind: str = "incremental") -> None:
    """Record that an ingest is in progress. Call before _batch_add.

    kind: 'full' (destructive rebuild — readers can see an empty silo) or
    'incremental' (additive — readers see stale-but-valid results).
    """
    p = _pending_path(db_path, silo_slug)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "silo": silo_slug,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "kind": kind,
                    "pid": os.getpid(),
                }
            ),
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


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except Exception:
        return False
    return True


def _read_markers(db_path: str) -> list[dict]:
    out: list[dict] = []
    try:
        for p in Path(db_path).glob(f"{_PREFIX}*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("silo"):
                out.append(data)
    except Exception:
        pass
    return out


def check_pending(db_path: str) -> list[str]:
    """Return silo slugs with an interrupted (pending) ingest marker.

    Includes live writers: a concurrent write is still a reason for the next run
    to treat the silo as not-known-clean.
    """
    return [str(m["silo"]) for m in _read_markers(db_path)]


def active_writes(db_path: str) -> dict[str, dict]:
    """Silos with a *live* writer, keyed by slug.

    Excludes markers whose process is gone — those are interrupted ingests, not
    writes in flight, and reporting them as active would pin a silo in a
    permanent "writing" state after any crash.
    """
    active: dict[str, dict] = {}
    for marker in _read_markers(db_path):
        pid = marker.get("pid")
        if not _pid_alive(pid if isinstance(pid, int) else None):
            continue
        slug = str(marker["silo"])
        active[slug] = {
            "silo": slug,
            "started_at": marker.get("started_at"),
            "kind": marker.get("kind") or "incremental",
            "pid": pid,
        }
    return active


def merge_write_states(states: list[dict | None]) -> dict | None:
    """Union several write-in-progress descriptors into one.

    Used when one response is assembled from multiple retrievals: a rebuild seen
    by any of them taints the merged result set.
    """
    present = [s for s in states if s]
    if not present:
        return None
    silos: set[str] = set()
    rebuilding: set[str] = set()
    starts: list[str] = []
    for state in present:
        silos.update(state.get("silos") or [])
        rebuilding.update(state.get("rebuilding") or [])
        if state.get("started_at"):
            starts.append(state["started_at"])
    return {
        "silos": sorted(silos),
        "rebuilding": sorted(rebuilding),
        "results_may_be_incomplete": bool(rebuilding),
        "started_at": min(starts) if starts else None,
    }


def write_in_progress(db_path: str, silo_slug: str | None = None) -> dict | None:
    """Write-in-progress descriptor for a read path, or None when clear.

    Pass silo_slug to scope to one silo; omit it (unscoped query) to report any
    live write, since an unscoped read spans every silo.
    """
    try:
        active = active_writes(db_path)
    except Exception:
        return None
    if not active:
        return None
    if silo_slug:
        entry = active.get(silo_slug)
        if not entry:
            return None
        entries = [entry]
    else:
        entries = list(active.values())
    # A destructive rebuild is the case that can silently empty a result set.
    rebuilding = [e for e in entries if e.get("kind") == "full"]
    return {
        "silos": sorted(e["silo"] for e in entries),
        "rebuilding": sorted(e["silo"] for e in rebuilding),
        "results_may_be_incomplete": bool(rebuilding),
        "started_at": min((e.get("started_at") or "" for e in entries), default=None) or None,
    }
