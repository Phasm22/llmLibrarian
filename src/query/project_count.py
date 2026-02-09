"""
Deterministic project count: number of coding project buckets (top-level
folders containing code files) in a silo. No LLM.
"""
from pathlib import Path
from typing import Any

from style import dim, label_style

from query.code_language import CODE_EXTENSIONS

try:
    from ingest import get_paths_by_silo
except ImportError:
    get_paths_by_silo = None  # type: ignore[misc, assignment]


def _get_silo_root(db_path: str | Path, silo: str | None) -> str | None:
    """Return silo root path from registry if available."""
    if not silo:
        return None
    try:
        from state import list_silos  # local import to avoid cycles
    except Exception:
        return None
    for s in list_silos(db_path):
        if (s or {}).get("slug") == silo:
            return (s or {}).get("path")
    return None


def compute_project_count(
    *,
    db_path: str,
    silo: str | None,
    collection: Any,
) -> tuple[int, list[str]]:
    """Deterministic count of project buckets (top-level folders containing code files)."""
    paths_by_silo = get_paths_by_silo(db_path) if get_paths_by_silo else {}
    silo_paths = set(paths_by_silo.get(silo or "", []))
    root = _get_silo_root(db_path, silo)

    metas: list[dict] = []
    if not silo_paths:
        try:
            kw: dict = {"include": ["metadatas"]}
            if silo:
                kw["where"] = {"silo": silo}
            res = collection.get(**kw)
            metas = res.get("metadatas") or []
            for m in metas:
                p = (m or {}).get("source")
                if p:
                    silo_paths.add(p)
        except Exception:
            pass

    buckets: set[str] = set()
    samples: list[str] = []

    def _bucket_for(path_str: str) -> str:
        try:
            p = Path(path_str).resolve()
            if root and Path(path_str).resolve().as_posix().startswith(Path(root).resolve().as_posix()):
                rel_parts = p.relative_to(Path(root).resolve()).parts
                if len(rel_parts) >= 2:
                    return rel_parts[0]
                return "(root)"
            parent = p.parent.name
            return parent or "(root)"
        except Exception:
            return "(root)"

    for p in sorted(silo_paths):
        ext = Path(p).suffix.lower()
        if ext not in CODE_EXTENSIONS:
            continue
        bucket = _bucket_for(p)
        if bucket not in buckets and len(samples) < 5:
            if root and Path(p).resolve().as_posix().startswith(Path(root).resolve().as_posix()):
                try:
                    rel = Path(p).resolve().relative_to(Path(root).resolve())
                    samples.append(str(rel))
                except Exception:
                    samples.append(Path(p).name)
            else:
                samples.append(Path(p).name)
        buckets.add(bucket)

    return len(buckets), samples


def format_project_count(
    *,
    count: int,
    samples: list[str],
    source_label: str,
    no_color: bool,
) -> str:
    """Format project count output for CLI."""
    if count > 0:
        lines = [f"Found {count} coding project folder{'s' if count != 1 else ''} in {source_label}."]
        if samples:
            lines.append("")
            lines.append(dim(no_color, "Samples:"))
            for s in samples:
                lines.append(f"  • {s}")
        lines.extend(["", dim(no_color, "---"), label_style(no_color, f"Answered by: {source_label}")])
        return "\n".join(lines)
    if samples:
        lines = [
            f"Found {len(samples)} code file{'s' if len(samples) != 1 else ''} in {source_label}, all at root-level.",
            "",
            dim(no_color, "Samples:"),
        ]
        for s in samples:
            lines.append(f"  • {s}")
        lines.extend(["", dim(no_color, "---"), label_style(no_color, f"Answered by: {source_label}")])
        return "\n".join(lines)
    return (
        f"No code files indexed for {source_label}. Add code (e.g. .py, .js) with: llmli add <path>\n\n"
        + dim(no_color, "---")
        + "\n"
        + label_style(no_color, f"Answered by: {source_label}")
    )
