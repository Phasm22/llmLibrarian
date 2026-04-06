#!/usr/bin/env python3
"""
Re-run indexing for each path in a backed-up llmli_registry.json.

Runs all adds in a single Python process so Chroma uses one PersistentClient /
HNSW handle for the whole batch. Spawning separate `llmli add` subprocesses
can corrupt or balloon on-disk HNSW (link_lists.bin).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BACKUP_REG = Path.home() / "llmLibrarian_chroma_recovery_backup_20260405" / "llmli_registry.json"
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
    if not BACKUP_REG.is_file():
        print(f"Missing registry backup: {BACKUP_REG}", file=sys.stderr)
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

    from constants import DB_PATH
    from ingest import run_add

    with BACKUP_REG.open(encoding="utf-8") as f:
        reg = json.load(f)
    items = sorted(reg.values(), key=lambda v: v.get("path") or "")

    failures: list[str] = []
    vision_ok = bool(os.environ.get("LLMLIBRARIAN_VISION_MODEL", "").strip())

    for v in items:
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
                db_path=DB_PATH,
                incremental=False,
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
