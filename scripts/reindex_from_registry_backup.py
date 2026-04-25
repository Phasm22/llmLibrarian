#!/usr/bin/env python3
"""
Re-run indexing for each path in a backed-up llmli_registry.json.

Runs all adds in a single Python process so Chroma uses one PersistentClient /
HNSW handle for the whole batch. Spawning separate `llmli add` subprocesses
can corrupt or balloon on-disk HNSW (link_lists.bin).

Example:
  uv run python scripts/reindex_from_registry_backup.py \\
    --registry-backup ~/backup/llmli_registry.json \\
    --db-path ./my_brain_db
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _apply_bulk_reindex_defaults() -> None:
    """Tune ingest for speed and lower CPU/battery on large rebuilds.

    Scanned PDFs + macOS Vision OCR per page are the main cost. Override any of
    these in the environment before running this script if you need full OCR or
    PDF table extraction (e.g. tax silos): unset LLMLIBRARIAN_OCR_BACKEND or set
    LLMLIBRARIAN_REINDEX_FULL=1.
    """
    if os.environ.get("LLMLIBRARIAN_REINDEX_FULL", "").strip().lower() in ("1", "true", "yes"):
        return
    os.environ.setdefault("LLMLIBRARIAN_OCR_BACKEND", "none")
    os.environ.setdefault("LLMLIBRARIAN_PDF_TABLES", "0")
    # Avoid MPS wakeups during long runs; override with LLMLIBRARIAN_EMBEDDING_DEVICE=mps if desired.
    os.environ.setdefault("LLMLIBRARIAN_EMBEDDING_DEVICE", "cpu")
    os.environ.setdefault("LLMLIBRARIAN_MAX_WORKERS", "4")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bulk re-index every silo path from a backed-up llmli_registry.json (single process).",
    )
    parser.add_argument(
        "--registry-backup",
        type=Path,
        required=True,
        help="Path to llmli_registry.json backup (JSON object keyed by slug).",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Chroma persist directory (default: LLMLIBRARIAN_DB env or repo constants.DB_PATH).",
    )
    args = parser.parse_args()
    backup = args.registry_backup.expanduser().resolve()
    if not backup.is_file():
        print(f"Missing registry backup: {backup}", file=sys.stderr)
        return 1

    _apply_bulk_reindex_defaults()
    print(
        "Bulk reindex defaults: OCR off, PDF tables off, embedding on CPU, max 4 file workers. "
        "Set LLMLIBRARIAN_REINDEX_FULL=1 to use normal ingest settings.",
        flush=True,
    )

    # Import llmli modules from src/ (same layout as CLI / hatch dev mode).
    src = REPO / "src"
    sys.path.insert(0, str(src))
    os.chdir(REPO)

    try:
        from env_bootstrap import bootstrap_llmlibrarian_env

        bootstrap_llmlibrarian_env(repo_root=REPO)
    except Exception:
        pass

    from constants import DB_PATH
    from ingest import run_add

    db_path = args.db_path
    if db_path is not None:
        db_resolved = str(db_path.expanduser().resolve())
    else:
        raw = os.environ.get("LLMLIBRARIAN_DB", "").strip()
        db_resolved = str(Path(raw).expanduser().resolve()) if raw else str(Path(DB_PATH).resolve())

    with backup.open(encoding="utf-8") as f:
        reg = json.load(f)
    items = sorted(reg.items(), key=lambda item: (item[1].get("path") or "", item[0]))

    failures: list[str] = []
    vision_ok = bool(os.environ.get("LLMLIBRARIAN_VISION_MODEL", "").strip())

    for slug, v in items:
        path = (v.get("path") or "").strip()
        if not path:
            continue
        p = Path(path)
        if not p.exists():
            print(f"SKIP (missing): {path}", flush=True)
            continue
        want_vision = bool(v.get("image_vision_enabled")) and vision_ok
        print(f"ADD: {path}" + (" [image-vision]" if want_vision else ""), flush=True)
        try:
            run_add(
                path=p,
                db_path=db_resolved,
                incremental=False,
                forced_silo_slug=slug,
                display_name_override=v.get("display_name") or v.get("name") or slug,
                exclude_patterns=v.get("exclude_patterns"),
                image_vision_enabled=True if want_vision else None,
            )
        except Exception as exc:
            print(f"FAILED {path}: {exc}", file=sys.stderr, flush=True)
            failures.append(path)

    if failures:
        print("FAILURES:", failures, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
