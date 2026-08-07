"""Health summaries and the operator guidance derived from them.

``collect_health_summary`` is the one place that reads every health signal;
``health()``, ``session_context()`` and ``mcp_runtime_status()`` are views over
its output rather than three separate roll-ups.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from mcp_runtime import jobs


def compute_answer_confidence(chunks: list[dict]) -> tuple[str, float, str]:
    """Coarse confidence signal for a retrieval result: (level, mean, note)."""
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


def collect_health_summary(db_path: str, *, include_audit: bool = False) -> dict:
    """Collect health signals used by health() and session_context().

    include_audit=False keeps the payload small for routine session bootstrap.
    include_audit=True adds the deeper audit/storage details health() reports.
    """
    from chroma_client import chroma_mode_info
    from operations import op_db_storage_summary, op_silo_hnsw_consistency
    from silo_audit import (
        find_count_mismatches,
        find_duplicate_hashes,
        find_orphaned_sources,
        find_path_overlaps,
        load_file_registry,
        load_manifest,
        load_registry,
    )
    from state import get_last_failures, get_query_health

    query_errors = get_query_health(db_path)
    last_failures = get_last_failures(db_path)

    out: dict = {
        "db_path": db_path,
        "db_exists": Path(db_path).exists(),
        **chroma_mode_info(),
        "embedding_model": os.environ.get("LLMLIBRARIAN_EMBEDDING_MODEL", "all-mpnet-base-v2"),
        "embedding_kind": os.environ.get("LLMLIBRARIAN_EMBEDDING", "") or "sentence_transformer",
        "python_version": sys.version,
        "query_health": {
            "recent_error_count": len(query_errors),
            **({"recent_errors": query_errors[-10:]} if include_audit else {}),
        },
        "ingest_failures": {
            "last_failure_count": len(last_failures),
            **({"last_failures": last_failures[:20]} if include_audit else {}),
        },
    }

    if Path(db_path).is_dir():
        try:
            hnsw = op_silo_hnsw_consistency(db_path)
            if hnsw.get("status") == "ok":
                bad = [r for r in hnsw.get("silos") or [] if not r.get("consistent")]
                out["hnsw_consistency"] = {
                    "silo_count": hnsw.get("silo_count", 0),
                    "desynced_count": hnsw.get("desynced_count", 0),
                }
                if include_audit:
                    out["hnsw_consistency"]["desynced"] = [
                        {
                            "slug": r["slug"],
                            "sqlite_ids": r["sqlite_ids"],
                            "missing_count": r["missing_count"],
                            "queued": r["queued"],
                            "missing_ids_sample": r["missing_ids"][:5],
                        }
                        for r in bad
                    ]
            else:
                out["hnsw_consistency"] = {"error": hnsw.get("error")}
        except Exception as e:
            out["hnsw_consistency"] = {"error": f"{type(e).__name__}: {e}"}

    if include_audit:
        registry = load_registry(db_path)
        manifest = load_manifest(db_path)
        file_registry = load_file_registry(db_path)
        mismatches = find_count_mismatches(registry, manifest)
        orphaned = find_orphaned_sources(registry)
        out["silo_audit"] = {
            "silo_count": len(registry),
            "count_mismatch_count": len(mismatches),
            "count_mismatches": mismatches[:20],
            "duplicate_hash_group_count": len(find_duplicate_hashes(file_registry)),
            "path_overlap_count": len(find_path_overlaps(registry)),
            "orphaned_source_count": len(orphaned),
            "orphaned_sources": orphaned[:20],
        }
        if Path(db_path).is_dir():
            out["storage"] = op_db_storage_summary(db_path)

    out.update(jobs.snapshot())
    return out


def derive_recommended_actions(silos: list[dict], summary: dict) -> list[str]:
    """Concise operational guidance for session bootstrap."""
    actions: list[str] = []

    if not summary.get("db_exists"):
        actions.append("Fix LLMLIBRARIAN_DB for this MCP process before retrieval.")
        return actions

    if summary.get("chroma_transport") == "http" and not summary.get("chroma_server_ok", True):
        host = summary.get("chroma_server_host", "127.0.0.1")
        port = summary.get("chroma_server_port", 8000)
        actions.append(
            f"Chroma HTTP appears down at {host}:{port}; start/restart it "
            "(for example: pal chroma start)."
        )

    hnsw = summary.get("hnsw_consistency") or {}
    if isinstance(hnsw, dict):
        if int(hnsw.get("desynced_count", 0) or 0) > 0:
            actions.append("One or more silos are HNSW-desynced; run repair_silo on affected silos.")
        elif hnsw.get("error"):
            actions.append("HNSW consistency check returned an error; call health() for full diagnostics.")

    if int(((summary.get("query_health") or {}).get("recent_error_count", 0) or 0)) > 0:
        actions.append(
            "Recent query index errors were recorded; check health() details and "
            "repair affected silos if needed."
        )
    if int(((summary.get("ingest_failures") or {}).get("last_failure_count", 0) or 0)) > 0:
        actions.append("Recent ingest failures exist; inspect failures and reindex/repair affected silos.")

    for s in silos:
        slug = str(s.get("slug") or "")
        if not slug:
            continue
        if bool(s.get("has_index_errors")):
            actions.append(f"Silo {slug} reports index errors; run repair_silo for a clean rebuild.")
        if bool(s.get("has_ingest_failures")):
            actions.append(f"Silo {slug} has ingest failures; inspect and reindex or repair.")
        if bool(s.get("is_stale")):
            stale_count = int(s.get("stale_file_count") or 0)
            newest = str(s.get("newest_source_mtime_iso") or "")
            updated = str(s.get("updated") or "")
            if stale_count <= 3 and newest and updated and newest == updated:
                actions.append(
                    f"Silo {slug} shows minor stale noise (<=3 files) with matching "
                    "timestamps; index is likely usable."
                )
            else:
                actions.append(
                    f"Silo {slug} appears stale ({stale_count} files); run trigger_reindex "
                    "once source changes settle."
                )

    active = summary.get("active_background_jobs") or {}
    if isinstance(active, dict) and active:
        actions.append(
            "Background ingest/reindex jobs are active; wait for completion or check "
            "health() before heavy querying."
        )

    return actions


def dedupe_lines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def compact_runtime_jobs(summary: dict, *, verbose: bool = False, limit: int = 5) -> dict:
    """Keep runtime job records compact by default; full records in verbose mode."""
    active = summary.get("active_background_jobs") or {}
    last = summary.get("last_background_reindex") or {}
    if not isinstance(active, dict):
        active = {}
    if not isinstance(last, dict):
        last = {}

    if verbose:
        return {"active_background_jobs": active, "last_background_reindex": last}

    active_compact: dict[str, dict] = {}
    for key in list(active.keys())[:limit]:
        row = active.get(key) or {}
        if not isinstance(row, dict):
            continue
        active_compact[key] = {
            "kind": row.get("kind"),
            "silo": row.get("silo"),
            "started_at": row.get("started_at"),
        }

    last_compact: dict[str, dict] = {}
    for key in list(last.keys())[:limit]:
        row = last.get(key) or {}
        if not isinstance(row, dict):
            continue
        last_compact[key] = {
            "ok": row.get("ok"),
            "finished_at": row.get("finished_at"),
            **({"error": row.get("error")} if row.get("error") else {}),
        }

    return {
        "active_background_jobs": active_compact,
        "last_background_reindex": last_compact,
        "active_count": len(active),
        "last_outcome_count": len(last),
    }
