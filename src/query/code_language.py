"""
CODE_LANGUAGE intent: deterministic language detection by file extension count.
No retrieval, no LLM — pure counting from registry or Chroma metadata.
"""
from pathlib import Path
from typing import Any

from style import bold, dim, label_style
from query.formatting import shorten_path

# CODE_LANGUAGE: only count actual code files (not PDF/DOCX/syllabi that mention languages)
CODE_EXTENSIONS = frozenset(
    {".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".sh", ".bash", ".zsh", ".php", ".kt", ".sql"}
)
EXT_TO_LANG: dict[str, str] = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TSX", ".jsx": "JSX",
    ".mjs": "JavaScript", ".cjs": "JavaScript", ".go": "Go", ".rs": "Rust", ".java": "Java",
    ".c": "C", ".cpp": "C++", ".h": "C", ".hpp": "C++", ".cs": "C#", ".rb": "Ruby",
    ".sh": "Shell", ".bash": "Bash", ".zsh": "Zsh", ".php": "PHP", ".kt": "Kotlin", ".sql": "SQL",
}


def get_code_language_stats_from_registry(db_path: str | Path, silo: str | None) -> tuple[dict[str, int], dict[str, list[str]]] | None:
    """Return (by_ext, sample_paths) from registry language_stats, or None if not available. silo=None = aggregate all silos."""
    try:
        from state import list_silos
        silos = list_silos(db_path)
    except Exception:
        return None
    if silo:
        silos = [s for s in silos if s.get("slug") == silo]
    by_ext: dict[str, int] = {}
    sample_paths: dict[str, list[str]] = {}
    for s in silos:
        ls = (s or {}).get("language_stats")
        if not ls or not isinstance(ls, dict):
            continue
        ext_counts = ls.get("by_ext") or ls
        if isinstance(ext_counts.get("by_ext"), dict):
            ext_counts = ext_counts["by_ext"]
        samples = (ls.get("sample_paths") or {}) if isinstance(ls.get("sample_paths"), dict) else {}
        for ext, count in (ext_counts or {}).items():
            if ext in CODE_EXTENSIONS and isinstance(count, (int, float)):
                by_ext[ext] = by_ext.get(ext, 0) + int(count)
        for ext, paths in (samples or {}).items():
            if ext not in sample_paths:
                sample_paths[ext] = []
            for p in (paths if isinstance(paths, list) else [paths])[:3]:
                if p and p not in sample_paths[ext]:
                    sample_paths[ext].append(p)
    if not by_ext:
        return None
    return (by_ext, sample_paths)


def compute_code_language_from_chroma(
    collection: Any,
    silo: str | None,
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """Get unique source paths from Chroma for scope (silo or all), filter to code exts, count and sample."""
    try:
        kw: dict = {"include": ["metadatas"]}
        if silo:
            kw["where"] = {"silo": silo}
        res = collection.get(**kw)
    except Exception:
        return ({}, {})
    metas = res.get("metadatas") or []
    unique_by_ext: dict[str, set[str]] = {}
    for m in metas:
        src = (m or {}).get("source") or ""
        if not src:
            continue
        ext = Path(src).suffix.lower()
        if ext not in CODE_EXTENSIONS:
            continue
        unique_by_ext.setdefault(ext, set()).add(src)
    by_ext = {ext: len(paths) for ext, paths in unique_by_ext.items()}
    sample_paths = {ext: list(paths)[:3] for ext, paths in unique_by_ext.items()}
    return (by_ext, sample_paths)


def format_code_language_answer(
    by_ext: dict[str, int],
    sample_paths: dict[str, list[str]],
    source_label: str,
    no_color: bool,
) -> str:
    """Format deterministic 'most common language' answer: top language, top 3, 3 sample paths."""
    if not by_ext:
        return f"No code files found for {source_label}. Add code (e.g. .py, .js) with: llmli add <path>"
    sorted_exts = sorted(by_ext.keys(), key=lambda e: -by_ext[e])
    top_ext = sorted_exts[0]
    lang_name = EXT_TO_LANG.get(top_ext, top_ext)
    count = by_ext[top_ext]
    lines = [
        bold(no_color, f"Most common coding language: {lang_name} ({count} file{'s' if count != 1 else ''})"),
        "",
        dim(no_color, "Top languages by file count:"),
    ]
    for ext in sorted_exts[:5]:
        name = EXT_TO_LANG.get(ext, ext)
        lines.append(f"  \u2022 {name}: {by_ext[ext]} files")
    lines.append("")
    lines.append(dim(no_color, "Sample files (evidence):"))
    for ext in sorted_exts[:3]:
        paths = sample_paths.get(ext, [])[:3]
        for p in paths:
            short = shorten_path(p)
            lines.append(f"  \u2022 {short}")
    lines.extend(["", dim(no_color, "---"), label_style(no_color, f"Answered by: {source_label}")])
    return "\n".join(lines)
