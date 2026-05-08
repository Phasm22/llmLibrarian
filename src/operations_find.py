"""
Filename / date file lookup against the manifest.

``op_find_files`` is the metadata-only counterpart to vector retrieval: it
filters ``llmli_file_manifest.json`` by name glob and date range and returns a
structured list of file hits. No ChromaDB access unless the caller asks for
chunk counts.

Manifest entries written before this feature existed will not have
``name_date`` set. ``op_find_files`` derives it on the fly per query (one regex
per file) so upgrades work without re-ingest. The derived value is not
written back; ``llmli reindex --names`` is the one-shot migration that
persists it.
"""
from __future__ import annotations

import fnmatch
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal, Optional

from file_registry import _read_file_manifest
from query.filename_dates import (
    month_overlaps_range,
    parse_filename_date,
    utc_mtime_to_local_date,
)

DateField = Literal["name_date", "mtime", "either"]
DateSource = Literal["name_date", "mtime", "both"]


@dataclass
class FileHit:
    path: str
    silo: str
    name_date: Optional[str]
    name_date_precision: Optional[str]
    mtime: float
    mtime_local_date: str
    size: int
    hash: Optional[str]
    chunk_count: Optional[int]
    date_source: Optional[DateSource]


@dataclass
class FindResult:
    files: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_scanned: int = 0
    total_matched: int = 0
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def op_find_files(
    db_path: str | Path,
    *,
    silos: list[str] | None = None,
    name_glob: str | None = None,
    date_start: date | None = None,
    date_end: date | None = None,
    date_field: DateField = "either",
    include_chunk_count: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Return a manifest-filtered list of file hits.

    Filters are conjunctive: a hit must satisfy ``name_glob`` (if given) and
    the date range (if given). With ``date_field="either"`` a hit qualifies if
    the filename-embedded date *or* mtime falls in range; ``date_source``
    records which signal matched ("both" if they agree).
    """
    if date_start is not None and date_end is not None and date_start > date_end:
        return FindResult(warnings=[f"empty range: {date_start} > {date_end}"]).as_dict()

    manifest = _read_file_manifest(db_path)
    silo_map = manifest.get("silos") or {}
    if not isinstance(silo_map, dict):
        return FindResult(warnings=["manifest is malformed"]).as_dict()

    target_slugs: list[str]
    if silos:
        target_slugs = [s for s in silos if s in silo_map]
        missing = [s for s in silos if s not in silo_map]
    else:
        target_slugs = list(silo_map.keys())
        missing = []

    result = FindResult()
    for slug in missing:
        result.warnings.append(f"silo not in manifest: {slug}")

    raw_hits: list[FileHit] = []
    for slug in target_slugs:
        silo_entry = silo_map.get(slug) or {}
        if not isinstance(silo_entry, dict):
            continue
        silo_root = silo_entry.get("path") or ""
        files_map = silo_entry.get("files") or {}
        if not isinstance(files_map, dict):
            continue

        for abs_path, info in files_map.items():
            if not isinstance(info, dict):
                continue
            result.total_scanned += 1

            name_date_iso, name_date_precision = _resolve_name_date(abs_path, info)
            mtime = float(info.get("mtime", 0) or 0)
            mtime_local = utc_mtime_to_local_date(mtime)
            size = int(info.get("size", 0) or 0)
            file_hash = info.get("hash") or None

            if name_glob is not None:
                rel = _relative_silo_path(abs_path, silo_root)
                if not (
                    fnmatch.fnmatch(rel, name_glob)
                    or fnmatch.fnmatch(Path(abs_path).name, name_glob)
                ):
                    continue

            date_source = _date_match_source(
                name_date_iso=name_date_iso,
                name_date_precision=name_date_precision,
                mtime_local=mtime_local,
                date_start=date_start,
                date_end=date_end,
                date_field=date_field,
            )
            if date_start is not None or date_end is not None:
                if date_source is None:
                    continue

            raw_hits.append(
                FileHit(
                    path=abs_path,
                    silo=slug,
                    name_date=name_date_iso,
                    name_date_precision=name_date_precision,
                    mtime=mtime,
                    mtime_local_date=mtime_local,
                    size=size,
                    hash=file_hash,
                    chunk_count=None,
                    date_source=date_source,
                )
            )

    raw_hits.sort(
        key=lambda h: (
            h.name_date is None,
            _neg_iso(h.name_date),
            h.path,
        )
    )

    result.total_matched = len(raw_hits)
    if limit > 0 and len(raw_hits) > limit:
        result.truncated = True
        raw_hits = raw_hits[:limit]

    if include_chunk_count and raw_hits:
        _augment_chunk_counts(str(db_path), raw_hits, result.warnings)

    result.files = [asdict(h) for h in raw_hits]
    return result.as_dict()


def _resolve_name_date(
    abs_path: str,
    info: dict[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    """Use stored name_date if present, else derive on the fly (no write-back)."""
    stored = info.get("name_date")
    stored_precision = info.get("name_date_precision")
    if isinstance(stored, str) and stored:
        return stored, stored_precision if isinstance(stored_precision, str) else None
    return parse_filename_date(abs_path)


def _relative_silo_path(abs_path: str, silo_root: str) -> str:
    if not silo_root:
        return abs_path
    try:
        rel = str(Path(abs_path).resolve().relative_to(Path(silo_root).resolve()))
    except (ValueError, OSError):
        return abs_path
    return rel.replace("\\", "/")


def _date_match_source(
    *,
    name_date_iso: Optional[str],
    name_date_precision: Optional[str],
    mtime_local: str,
    date_start: date | None,
    date_end: date | None,
    date_field: DateField,
) -> DateSource | None:
    if date_start is None and date_end is None:
        # No date filter: still report which signals are present so renderers
        # can show conflicts.
        if name_date_iso and mtime_local:
            return "both" if name_date_iso == mtime_local else "name_date"
        if name_date_iso:
            return "name_date"
        if mtime_local:
            return "mtime"
        return None

    lo = date_start or date.min
    hi = date_end or date.max
    name_match = _name_date_in_range(name_date_iso, name_date_precision, lo, hi)
    mtime_match = _mtime_in_range(mtime_local, lo, hi)

    if date_field == "name_date":
        return "name_date" if name_match else None
    if date_field == "mtime":
        return "mtime" if mtime_match else None
    if name_match and mtime_match:
        return "both"
    if name_match:
        return "name_date"
    if mtime_match:
        return "mtime"
    return None


def _name_date_in_range(
    iso: Optional[str],
    precision: Optional[str],
    lo: date,
    hi: date,
) -> bool:
    if not iso:
        return False
    if precision == "month":
        return month_overlaps_range(iso, lo, hi)
    try:
        d = date.fromisoformat(iso)
    except ValueError:
        return False
    return lo <= d <= hi


def _mtime_in_range(mtime_local: str, lo: date, hi: date) -> bool:
    if not mtime_local:
        return False
    try:
        d = date.fromisoformat(mtime_local)
    except ValueError:
        return False
    return lo <= d <= hi


def _neg_iso(iso: Optional[str]) -> str:
    """Sort key helper: produce a string that orders newest dates first."""
    if not iso:
        return ""
    try:
        # Subtract from a fixed late date to invert ordering inside ascending sort.
        anchor = date(9999, 12, 31)
        d = date.fromisoformat(iso)
        delta = (anchor - d).days
        return f"{delta:08d}"
    except ValueError:
        return ""


def _augment_chunk_counts(
    db_path: str,
    hits: list[FileHit],
    warnings: list[str],
) -> None:
    try:
        from chroma_client import get_client, release  # lazy: avoid cold-start cost
        from chroma_lock import chroma_shared_lock
        from constants import LLMLI_COLLECTION
    except Exception as e:
        warnings.append(f"chunk_count unavailable (chroma import failed): {e}")
        return

    try:
        with chroma_shared_lock(db_path):
            client = get_client(db_path)
            try:
                coll = client.get_or_create_collection(name=LLMLI_COLLECTION)
            except Exception as e:
                warnings.append(f"chunk_count unavailable (collection): {e}")
                return
            for hit in hits:
                try:
                    res = coll.get(
                        where={"$and": [{"silo": hit.silo}, {"source": hit.path}]},
                        include=[],
                    )
                    ids = res.get("ids") or []
                    hit.chunk_count = len(ids)
                except Exception as e:
                    warnings.append(f"chunk_count failed for {hit.path}: {e}")
    finally:
        try:
            release()
        except Exception:
            pass
