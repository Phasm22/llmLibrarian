"""Single choke point for silo exclude policy.

Every ingest entry point (CLI add, pal pull, watcher, MCP add_silo / update_file /
repair / reindex) must resolve excludes through ``effective_excludes`` before any
file is indexed. Persisted custom patterns are always merged with built-in defaults
and any per-run extras; an omitted or empty per-run list never clears what the
silo already stored.

Also provides a cheap post-ingest audit (manifest paths vs effective excludes)
and a purge helper so a watcher/MCP sync cannot leave a silently widened silo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scan_patterns import (
    ADD_DEFAULT_EXCLUDE,
    exclude_matches,
    merge_exclude_patterns,
    normalize_patterns,
)


def effective_excludes(
    db_path: str | Path,
    silo_slug: str,
    requested: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Built-in defaults ∪ persisted silo excludes ∪ per-run extras."""
    from state import get_silo_exclude_patterns

    return merge_exclude_patterns(
        ADD_DEFAULT_EXCLUDE,
        get_silo_exclude_patterns(db_path, silo_slug),
        requested,
    )


def persistable_excludes(
    db_path: str | Path,
    silo_slug: str,
    requested: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Patterns to write back to the registry after an ingest.

    Always the full effective set so older readers that forget to merge defaults
    still honor both built-ins and custom patterns. An empty ``requested`` list
    does not drop previously persisted custom patterns.
    """
    return effective_excludes(db_path, silo_slug, requested)


def find_exclude_violations(
    db_path: str | Path,
    silo_slug: str,
    *,
    exclude_patterns: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return indexed manifest sources that match the silo's effective excludes.

    Cheap: reads the file manifest only (no Chroma scan). Each hit is
    ``{source, matched_pattern}``.
    """
    from file_registry import _read_file_manifest

    patterns = (
        normalize_patterns(exclude_patterns)
        if exclude_patterns is not None
        else effective_excludes(db_path, silo_slug)
    )
    if not patterns:
        return []
    manifest = _read_file_manifest(db_path)
    silo_entry = (manifest.get("silos") or {}).get(silo_slug, {})
    files = (silo_entry.get("files") or {}) if isinstance(silo_entry, dict) else {}
    if not isinstance(files, dict):
        return []
    violations: list[dict[str, Any]] = []
    for source in files:
        path_str = str(source)
        for pattern in patterns:
            if exclude_matches(path_str, pattern):
                violations.append({"source": path_str, "matched_pattern": pattern})
                break
    return violations


def purge_exclude_violations(
    db_path: str | Path,
    silo_slug: str,
    *,
    exclude_patterns: list[str] | None = None,
) -> dict[str, Any]:
    """Remove indexed files that violate the silo's exclude policy.

    Returns ``{violations_found, purged, errors}``.
    """
    from ingest import remove_single_file

    violations = find_exclude_violations(
        db_path, silo_slug, exclude_patterns=exclude_patterns
    )
    purged: list[str] = []
    errors: list[dict[str, str]] = []
    for hit in violations:
        source = str(hit.get("source") or "")
        if not source:
            continue
        try:
            status, resolved = remove_single_file(
                source,
                db_path=db_path,
                silo_slug=silo_slug,
                update_counts=True,
            )
            if status in ("removed", "skipped"):
                purged.append(resolved or source)
            else:
                errors.append({"source": source, "error": f"status={status}"})
        except Exception as exc:
            errors.append({"source": source, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "violations_found": len(violations),
        "purged": purged,
        "errors": errors,
        "violations": violations,
    }
