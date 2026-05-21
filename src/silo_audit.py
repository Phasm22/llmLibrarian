"""
Silo audit utilities: detect duplicate content, overlapping silos, and registry mismatches.
Read-only helpers for llmli registry, file registry, and file manifest.
"""
import json
import sys
from pathlib import Path
from typing import Any


def _registry_path(db_path: str | Path) -> Path:
    p = Path(db_path).resolve()
    if p.is_dir():
        return p / "llmli_registry.json"
    return p.parent / "llmli_registry.json"


def _file_registry_path(db_path: str | Path) -> Path:
    p = Path(db_path).resolve()
    if p.is_dir():
        return p / "llmli_file_registry.json"
    return p.parent / "llmli_file_registry.json"


def _file_manifest_path(db_path: str | Path) -> Path:
    p = Path(db_path).resolve()
    if p.is_dir():
        return p / "llmli_file_manifest.json"
    return p.parent / "llmli_file_manifest.json"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[llmli] failed to read {path}: {e}", file=sys.stderr)
        return {}


def load_registry(db_path: str | Path) -> list[dict[str, Any]]:
    reg = _read_json(_registry_path(db_path))
    out: list[dict[str, Any]] = []
    for v in reg.values():
        if isinstance(v, dict):
            out.append(v)
    return out


def load_file_registry(db_path: str | Path) -> dict[str, Any]:
    return _read_json(_file_registry_path(db_path))


def load_manifest(db_path: str | Path) -> dict[str, Any]:
    return _read_json(_file_manifest_path(db_path))


def find_duplicate_hashes(file_registry: dict[str, Any]) -> list[dict[str, Any]]:
    by_hash = file_registry.get("by_hash") or {}
    dupes = []
    for h, entries in by_hash.items():
        if not isinstance(entries, list):
            continue
        silos = sorted({(e or {}).get("silo") for e in entries if (e or {}).get("silo")})
        if len(silos) <= 1:
            continue
        paths = [e.get("path") for e in entries if isinstance(e, dict) and e.get("path")]
        dupes.append({"hash": h, "silos": silos, "paths": paths})
    return dupes


def find_path_overlaps(registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for s in registry:
        slug = s.get("slug")
        path = s.get("path")
        if slug and path:
            rows.append((slug, Path(path).resolve()))
    overlaps = []
    for i, (slug_a, path_a) in enumerate(rows):
        for slug_b, path_b in rows[i + 1 :]:
            if path_a == path_b:
                overlaps.append({"type": "same_path", "silos": [slug_a, slug_b], "path": str(path_a)})
            elif str(path_a).startswith(str(path_b) + "/"):
                overlaps.append({"type": "nested", "parent": slug_b, "child": slug_a, "path": str(path_a)})
            elif str(path_b).startswith(str(path_a) + "/"):
                overlaps.append({"type": "nested", "parent": slug_a, "child": slug_b, "path": str(path_b)})
    return overlaps


def find_count_mismatches(registry: list[dict[str, Any]], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    mismatches = []
    silos = manifest.get("silos") or {}
    for s in registry:
        slug = s.get("slug")
        if not slug:
            continue
        files_indexed = int(s.get("files_indexed", 0) or 0)
        silo_manifest = silos.get(slug) or {}
        files = silo_manifest.get("files") or {}
        if isinstance(files, dict):
            manifest_count = len(files)
            if manifest_count != files_indexed:
                mismatches.append(
                    {
                        "slug": slug,
                        "registry_files": files_indexed,
                        "manifest_files": manifest_count,
                    }
                )
    return mismatches


def find_orphaned_sources(registry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return silos whose registered source path no longer exists on disk."""
    orphans = []
    for s in registry:
        slug = s.get("slug")
        path = s.get("path")
        if slug and path and not Path(path).exists():
            orphans.append({"slug": slug, "path": path})
    return orphans


def format_report(
    registry: list[dict[str, Any]],
    dupes: list[dict[str, Any]],
    overlaps: list[dict[str, Any]],
    mismatches: list[dict[str, Any]],
    orphans: list[dict[str, Any]] | None = None,
    max_paths: int = 4,
) -> str:
    orphans = orphans or []
    lines: list[str] = []
    lines.append(f"Silos: {len(registry)}  Dupes: {len(dupes)}  Overlaps: {len(overlaps)}  Mismatches: {len(mismatches)}  Orphans: {len(orphans)}")

    if dupes:
        lines.append("")
        lines.append("Duplicate content:")
        for d in dupes:
            silos = ", ".join(d.get("silos") or [])
            paths = d.get("paths") or []
            lines.append(f"  {silos}  (hash {d.get('hash', '?')[:12]})")
            for p in paths[:max_paths]:
                lines.append(f"    {p}")
            if len(paths) > max_paths:
                lines.append(f"    +{len(paths) - max_paths} more")
            lines.append("    fix: pal remove <silo>")

    if overlaps:
        lines.append("")
        lines.append("Overlapping paths:")
        for o in overlaps:
            if o.get("type") == "same_path":
                lines.append(f"  same path: {', '.join(o.get('silos') or [])} -> {o.get('path')}")
            else:
                lines.append(f"  nested: {o.get('child')} inside {o.get('parent')}")
            lines.append("    fix: pal remove <silo>")

    if mismatches:
        lines.append("")
        lines.append("File count mismatches:")
        for m in mismatches:
            lines.append(
                f"  {m.get('slug')}: indexed {m.get('registry_files')}, on disk {m.get('manifest_files')}"
            )
            lines.append("    fix: pal pull --full")

    if orphans:
        lines.append("")
        lines.append("Orphaned sources (source folder deleted):")
        for o in orphans:
            lines.append(f"  {o.get('slug')}: {o.get('path')}")
            lines.append("    fix: llmli rm <silo>")

    if not dupes and not overlaps and not mismatches and not orphans:
        lines.append("All clean.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SQLite ↔ HNSW consistency probe.
#
# Reads `index_metadata.pickle` from every HNSW segment dir under the persist
# root and `embeddings_queue` from chroma.sqlite3, then compares against the
# canonical "this silo owns these IDs" view (collection.get(where={silo})).
# A truly missing ID is one in SQLite but neither in HNSW's id_to_label nor
# pending in the compaction queue.
# ---------------------------------------------------------------------------


def _hnsw_id_set(db_root: Path) -> set[str]:
    import pickle
    ids: set[str] = set()
    if not db_root.is_dir():
        return ids
    for p in db_root.iterdir():
        meta = p / "index_metadata.pickle"
        if not meta.exists():
            continue
        try:
            data = pickle.loads(meta.read_bytes())
        except Exception:
            continue
        if isinstance(data, dict):
            m = data.get("id_to_label")
            if isinstance(m, dict):
                ids.update(str(k) for k in m.keys())
    return ids


def _embeddings_queue_id_set(db_root: Path) -> set[str]:
    import sqlite3
    sqlite_path = db_root / "chroma.sqlite3"
    if not sqlite_path.exists():
        return set()
    try:
        con = sqlite3.connect(str(sqlite_path))
        try:
            rows = con.execute("SELECT id FROM embeddings_queue").fetchall()
        finally:
            con.close()
    except Exception:
        return set()
    return {r[0] for r in rows if r and r[0]}


def verify_silo_hnsw_consistency(collection: Any, silo_slug: str, db_path: str | Path) -> dict[str, Any]:
    """
    Compare a silo's SQLite-owned chunk IDs against the HNSW id_to_label set,
    after accounting for embeddings_queue compaction lag.

    Returns: {
        "slug": str,
        "sqlite_ids": int,
        "hnsw_reachable": int,        # |sqlite_ids ∩ hnsw_id_to_label|
        "queued": int,                # |missing_from_hnsw ∩ embeddings_queue|
        "missing_ids": list[str],     # truly missing (≤ 50 samples)
        "missing_count": int,         # full count of truly missing
        "consistent": bool,
    }
    """
    db_root = Path(db_path).resolve()
    try:
        res = collection.get(where={"silo": silo_slug}, include=["metadatas"])
        owned = set(res.get("ids") or [])
    except Exception as e:
        return {
            "slug": silo_slug,
            "sqlite_ids": 0,
            "hnsw_reachable": 0,
            "queued": 0,
            "missing_ids": [],
            "missing_count": 0,
            "consistent": False,
            "error": f"{type(e).__name__}: {e}",
        }
    hnsw_ids = _hnsw_id_set(db_root)
    queued_ids = _embeddings_queue_id_set(db_root)
    missing = owned - hnsw_ids
    truly = missing - queued_ids
    sample = sorted(truly)[:50]
    return {
        "slug": silo_slug,
        "sqlite_ids": len(owned),
        "hnsw_reachable": len(owned & hnsw_ids),
        "queued": len(missing & queued_ids),
        "missing_ids": sample,
        "missing_count": len(truly),
        "consistent": not truly,
    }
