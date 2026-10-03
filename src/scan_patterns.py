"""Default include/exclude patterns for file scanning.

Single source for both scanners: ``ingest`` (full pipeline) and
``watch_scan`` (watch daemons, deliberately free of chromadb/torch
imports). They previously carried byte-identical copies, which is a silent
drift hazard — a pattern added to one scanner but not the other means
``llmli add`` and the watcher disagree about what belongs in a silo.

Kept dependency-free so watch_scan stays lightweight.
"""

from __future__ import annotations

import fnmatch
from pathlib import PurePath

ADD_DEFAULT_INCLUDE = [
    "*.py", "*.ts", "*.tsx", "*.js", "*.go", "*.rs", "*.sh", "*.md", "*.txt",
    "*.yml", "*.yaml", "*.json", "*.csv", "*.xml", "*.html", "*.htm", "*.rst", "*.toml", "*.ini", "*.cfg", "*.sql",
    "*.pdf", "*.docx", "*.xlsx", "*.pptx",
    "*.png", "*.jpg", "*.jpeg", "*.heic", "*.heif", "*.tif", "*.tiff",
]

ADD_DEFAULT_EXCLUDE = [
    # Obsidian / journalLinker intent cortex lives under .../cortex/; keep out of retrieval silos.
    "/cortex/",
    "node_modules/", ".venv/", "venv/", "env/", "__pycache__/", "vendor", "dist", "build", ".git",
    # Matching is by whole segment now, so build dirs and oddly named virtualenvs
    # that "build"/"venv" used to catch only as substrings are listed explicitly.
    ".build/", "*venv/", "*Venv/",
    "llmLibrarianVenv/", "site-packages/", "Old Firefox Data", "Firefox", "*.app/",
    ".env", ".env.*", ".aws/", ".ssh/", "*.pem", "*.key", "secrets.json", "credentials.json", "credentials*.json",
    "pnpm-lock.yaml", "package-lock.json", "yarn.lock", "Pipfile.lock", "poetry.lock",
    "composer.lock", "Gemfile.lock", "Cargo.lock", "uv.lock",
    "my_brain_db/", "*.db", "*.sqlite", "*.sqlite3", "*.sqlite3-journal",
    # The vector store's own bookkeeping. "my_brain_db/" above only catches the
    # default location, but LLMLIBRARIAN_DB is configurable — with a DB
    # anywhere else, these .json artifacts match the *.json include rule and
    # the store indexes itself. That also self-feeds: ingesting rewrites the
    # manifest, which the watcher then sees as a change to re-ingest. Matched
    # by name so the location does not matter.
    "llmli_*.json", "image_artifacts/",
    # Agent worktrees are full copies of a repo living inside it; a silo on the
    # repo indexed each one again (and every in-progress edit). Tool caches are
    # never content. Leading slash keeps these to whole path segments.
    "/.claude/worktrees/", "/.pytest_cache/", "/.mypy_cache/", "/.ruff_cache/",
]


def exclude_matches(path: str | PurePath, pattern: str) -> bool:
    """True when ``pattern`` excludes ``path``, matching whole path segments.

    Patterns used to be tested as raw substrings (``pattern.rstrip("/") in path``),
    so "env/" excluded /notes/environment-setup.md, "build" build_notes.md,
    "dist" distance.py, "vendor" vendors.md and "Firefox" Firefox-tips.md — those
    files were silently never indexed.

    Now a pattern is split on "/" (leading/trailing slashes only mark it as a
    directory name) and must match a contiguous run of the path's segments, each
    part by fnmatch, case-insensitively: "node_modules/" matches a segment named
    node_modules; "/.claude/worktrees/" matches those two segments in a row;
    "*.pem" matches any segment ending .pem (the basename, or a directory). Every
    segment is checked, not just the last, because watch events arrive as file
    paths inside excluded directories.
    """
    raw = str(pattern or "").replace("\\", "/").strip()
    parts = [part for part in raw.lower().split("/") if part]
    if not parts:
        return False
    segments = [seg for seg in str(path).replace("\\", "/").lower().split("/") if seg]
    width = len(parts)
    for start in range(len(segments) - width + 1):
        if all(fnmatch.fnmatchcase(segments[start + i], parts[i]) for i in range(width)):
            return True
    return False
