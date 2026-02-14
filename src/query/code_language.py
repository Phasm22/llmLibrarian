"""
CODE_LANGUAGE intent: deterministic language detection by file extension count.
No retrieval, no LLM — pure counting from registry or Chroma metadata.
"""
from datetime import datetime, timezone
import re
from pathlib import Path
from typing import Any

from file_registry import _read_file_manifest
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


def get_code_language_stats_from_manifest_year(
    db_path: str | Path,
    silo: str | None,
    year: int,
) -> tuple[dict[str, int], dict[str, list[str]]]:
    """
    Deterministic year-scoped code-language stats from file manifest mtime.
    - Uses mtime year only.
    - silo=None aggregates across all silos.
    """
    manifest = _read_file_manifest(db_path)
    manifest_silos = (manifest.get("silos") or {}) if isinstance(manifest, dict) else {}
    if not isinstance(manifest_silos, dict):
        return ({}, {})

    if silo:
        silo_items = [(silo, manifest_silos.get(silo))]
    else:
        silo_items = sorted(manifest_silos.items(), key=lambda kv: str(kv[0]))

    by_ext: dict[str, int] = {}
    sample_paths: dict[str, list[str]] = {}

    for _slug, entry in silo_items:
        if not isinstance(entry, dict):
            continue
        files_map = entry.get("files") or {}
        if not isinstance(files_map, dict):
            continue

        for src, meta in sorted(files_map.items(), key=lambda kv: str(kv[0])):
            mtime = (meta or {}).get("mtime")
            if mtime is None:
                continue
            try:
                dt = datetime.fromtimestamp(float(mtime), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                continue
            if dt.year != int(year):
                continue

            ext = Path(src).suffix.lower()
            if ext not in CODE_EXTENSIONS:
                continue

            by_ext[ext] = by_ext.get(ext, 0) + 1
            sample_paths.setdefault(ext, [])
            if src not in sample_paths[ext] and len(sample_paths[ext]) < 3:
                sample_paths[ext].append(src)

    return (by_ext, sample_paths)


def get_code_sources_from_manifest_year(
    db_path: str | Path,
    silo: str | None,
    year: int,
    cap: int | None = None,
) -> list[str]:
    """
    Return deterministic list of code file sources from manifest matching mtime year.
    - Uses mtime year only.
    - silo=None aggregates across all silos.
    """
    manifest = _read_file_manifest(db_path)
    manifest_silos = (manifest.get("silos") or {}) if isinstance(manifest, dict) else {}
    if not isinstance(manifest_silos, dict):
        return []

    if silo:
        silo_items = [(silo, manifest_silos.get(silo))]
    else:
        silo_items = sorted(manifest_silos.items(), key=lambda kv: str(kv[0]))

    out: list[str] = []
    seen: set[str] = set()
    for _slug, entry in silo_items:
        if not isinstance(entry, dict):
            continue
        files_map = entry.get("files") or {}
        if not isinstance(files_map, dict):
            continue
        for src, meta in sorted(files_map.items(), key=lambda kv: str(kv[0])):
            mtime = (meta or {}).get("mtime")
            if mtime is None:
                continue
            try:
                dt = datetime.fromtimestamp(float(mtime), tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                continue
            if dt.year != int(year):
                continue
            ext = Path(src).suffix.lower()
            if ext not in CODE_EXTENSIONS:
                continue
            src_norm = str(src)
            if src_norm in seen:
                continue
            seen.add(src_norm)
            out.append(src_norm)
            if cap is not None and cap > 0 and len(out) >= int(cap):
                return out
    return out


def summarize_code_activity_year(
    year: int,
    docs: list[str],
    metas: list[dict | None],
) -> str:
    """
    Deterministic summary for "what was I coding in YYYY" from code-context evidence.
    Uses both metadata (extensions/paths) and retrieved content keywords.
    """
    # Unique sources with stable insertion order.
    seen_sources: set[str] = set()
    sources: list[str] = []
    ext_counts: dict[str, int] = {}
    for m in metas:
        src = str((m or {}).get("source") or "")
        if not src or src in seen_sources:
            continue
        seen_sources.add(src)
        sources.append(src)
        ext = Path(src).suffix.lower()
        if ext in CODE_EXTENSIONS:
            ext_counts[ext] = ext_counts.get(ext, 0) + 1

    # Theme detection from retrieved content snippets.
    joined = " ".join((d or "").lower() for d in docs[:20])
    theme_rules: list[tuple[str, tuple[str, ...]]] = [
        ("GUI apps (tkinter/PIL)", ("tkinter", "canvas", "gui", "messagebox", "image", "pil")),
        ("Shell scripting and CLI tooling", ("getopts", "bash", "shell", "chmod", "argv", "opt")),
        ("Cryptography and number theory exercises", ("encrypt", "decrypt", "cipher", "rsa", "euclidean", "gcd", "mod")),
        ("Data/file handling and JSON utilities", ("json", "open(", "read", "write", "dictionary", "file")),
        ("Testing and programming exercises", ("expect", "assert", "test", "lab", "homework", "project")),
    ]
    themes: list[str] = []
    for label, kws in theme_rules:
        if any(kw in joined for kw in kws):
            themes.append(label)

    # Fallback when snippets are sparse: infer from path tokens.
    if not themes and sources:
        path_blob = " ".join(Path(s).as_posix().lower() for s in sources[:30])
        fallback_tokens = [
            ("Course/lab assignments", ("lab", "homework", "project", "semester")),
            ("Utility scripts", ("script", "tools", "helper", "utils")),
            ("GUI experiments", ("tkinter", "gui")),
        ]
        for label, kws in fallback_tokens:
            if any(kw in path_blob for kw in kws):
                themes.append(label)

    sorted_exts = sorted(ext_counts.keys(), key=lambda e: (-ext_counts[e], e))
    if sorted_exts:
        lang_parts = []
        for ext in sorted_exts[:3]:
            lang = EXT_TO_LANG.get(ext, ext)
            lang_parts.append(f"{lang} ({ext_counts[ext]})")
        lang_text = ", ".join(lang_parts)
    else:
        lang_text = "unknown"

    lines: list[str] = [
        f"In {year}, you were working on code across {len(sources)} file(s).",
        f"Main languages in this evidence: {lang_text}.",
    ]
    if themes:
        lines.append("")
        lines.append("What you were working on:")
        for t in themes[:5]:
            lines.append(f"  • {t}")
    if sources:
        lines.append("")
        lines.append("Representative files:")
        for src in sources[:6]:
            lines.append(f"  • {shorten_path(src)}")
    return "\n".join(lines)


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


def format_code_language_year_answer(
    year: int,
    by_ext: dict[str, int],
    sample_paths: dict[str, list[str]],
    source_label: str,
    no_color: bool,
) -> str:
    """Format deterministic year-scoped coding language answer."""
    if not by_ext:
        return "\n".join(
            [
                f"No code files found from {year} in {source_label}.",
                "",
                dim(no_color, "---"),
                label_style(no_color, f"Answered by: {source_label}"),
            ]
        )

    sorted_exts = sorted(by_ext.keys(), key=lambda e: (-by_ext[e], e))
    top_ext = sorted_exts[0]
    lang_name = EXT_TO_LANG.get(top_ext, top_ext)
    count = by_ext[top_ext]

    lines = [
        bold(
            no_color,
            f"In {year}, your most common coding language was {lang_name} ({count} file{'s' if count != 1 else ''}).",
        ),
        "",
        dim(no_color, f"Top languages by file count in {year}:"),
    ]
    for ext in sorted_exts[:5]:
        name = EXT_TO_LANG.get(ext, ext)
        lines.append(f"  \u2022 {name}: {by_ext[ext]} files")

    lines.append("")
    lines.append(dim(no_color, "Sample files (evidence):"))
    for ext in sorted_exts[:3]:
        paths = sample_paths.get(ext, [])[:3]
        for p in paths:
            lines.append(f"  \u2022 {shorten_path(p)}")

    lines.extend(["", dim(no_color, "---"), label_style(no_color, f"Answered by: {source_label}")])
    return "\n".join(lines)
