"""
Single entry for indexing: maps to ingest.run_add with optional env (quiet, status file, extra_env).
Used by llmli CLI, pal pull, and MCP add_silo.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from ingest import run_add


@dataclass
class IngestRequest:
    """Normalized ingest options (mirrors run_add + ephemeral env)."""

    path: str | Path
    db_path: str | Path | None = None
    no_color: bool = False
    allow_cloud: bool = False
    follow_symlinks: bool = False
    incremental: bool = True
    forced_silo_slug: str | None = None
    display_name: str | None = None
    image_vision_enabled: bool | None = None
    workers: int | None = None
    embedding_workers: int | None = None
    get_chroma_client: Callable[[str], Any] | None = None
    quiet: bool = False
    """If True, set LLMLIBRARIAN_QUIET=1 for the duration of run_add (subprocess callers)."""
    status_file: str | Path | None = None
    """If set, LLMLIBRARIAN_STATUS_FILE for run_add completion JSON."""
    extra_env: dict[str, str] | None = None
    """Merged into os.environ for the duration of run_add (e.g. pal log levels)."""


@dataclass
class IngestResult:
    files_indexed: int
    failures: int
    silo_slug: str | None


@contextmanager
def _env_overlay(updates: dict[str, str | None]) -> Iterator[None]:
    prev: dict[str, str | None] = {}
    try:
        for k, v in updates.items():
            prev[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, old in prev.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


def run_ingest(request: IngestRequest) -> IngestResult:
    """
    Run ingest without printing. Callers handle CLI UX.
    Resolves silo slug after a successful path registration via state.
    """
    path = Path(request.path).resolve()
    db = request.db_path
    if db is None:
        from constants import DB_PATH

        db = DB_PATH

    overlay: dict[str, str | None] = {}
    if request.quiet:
        overlay["LLMLIBRARIAN_QUIET"] = "1"
    if request.status_file is not None:
        overlay["LLMLIBRARIAN_STATUS_FILE"] = str(Path(request.status_file).resolve())
    if request.extra_env:
        for k, v in request.extra_env.items():
            overlay[k] = v

    with _env_overlay(overlay):
        files_ok, n_failures = run_add(
            path,
            db_path=db,
            no_color=request.no_color,
            allow_cloud=request.allow_cloud,
            follow_symlinks=request.follow_symlinks,
            incremental=request.incremental,
            forced_silo_slug=request.forced_silo_slug,
            display_name_override=request.display_name,
            image_vision_enabled=request.image_vision_enabled,
            workers=request.workers,
            embedding_workers=request.embedding_workers,
            get_chroma_client=request.get_chroma_client,
        )

    slug: str | None = None
    try:
        from state import resolve_silo_by_path

        slug = resolve_silo_by_path(db, path)
    except Exception:
        slug = None

    return IngestResult(files_indexed=files_ok, failures=n_failures, silo_slug=slug)


def llmli_add_argv(request: IngestRequest) -> list[str]:
    """Build `llmli add` argument list (program name not included)."""
    out: list[str] = ["add"]
    if not request.incremental:
        out.append("--full")
    if request.allow_cloud:
        out.append("--allow-cloud")
    if request.follow_symlinks:
        out.append("--follow-symlinks")
    if request.image_vision_enabled:
        out.append("--image-vision")
    if request.workers is not None:
        out.extend(["--workers", str(request.workers)])
    if request.embedding_workers is not None:
        out.extend(["--embedding-workers", str(request.embedding_workers)])
    if request.forced_silo_slug:
        out.extend(["--silo", request.forced_silo_slug])
    if request.display_name:
        out.extend(["--display-name", request.display_name])
    out.append(str(Path(request.path).resolve()))
    return out
