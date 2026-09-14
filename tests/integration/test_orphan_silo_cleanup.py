"""Removing a silo that is no longer in the registry must still delete its chunks.

This is the recovery path for the 2026-09-14 incident: `llmlibrarian-46ad0cbe`
lost its registry entry while 2467 chunks stayed in Chroma. `llmli rm <slug>` is
what should clear those, and it could not — the fallback re-slugified an
already-hashed slug ("alpha-deb12c9c" -> "alpha-deb12c9c-9b411e47"), so the
delete matched nothing and reported success.
"""
from __future__ import annotations

from pathlib import Path

import chromadb
from chromadb.config import Settings

from constants import LLMLI_COLLECTION
from ingest import run_add
from operations import op_remove_silo
from state import list_silos, resolve_silo_by_path


def _chunks(db_path: Path, slug: str) -> int:
    client = chromadb.PersistentClient(
        path=str(db_path), settings=Settings(anonymized_telemetry=False)
    )
    coll = client.get_or_create_collection(name=LLMLI_COLLECTION)
    return len(coll.get(where={"silo": slug}, include=["metadatas"]).get("metadatas") or [])


def _ingest(tmp_path: Path) -> tuple[Path, str]:
    data = tmp_path / "alpha"
    data.mkdir()
    (data / "a.txt").write_text("hello world", encoding="utf-8")
    db = tmp_path / "db"
    run_add(data, db_path=db, incremental=True)
    slug = resolve_silo_by_path(str(db), data)
    assert slug, "ingest registered no silo"
    assert _chunks(db, slug) > 0
    return db, slug


def test_remove_registered_silo_deletes_chunks(tmp_path: Path) -> None:
    db, slug = _ingest(tmp_path)

    result = op_remove_silo(str(db), slug)

    assert result["removed_slug"] == slug
    assert result["not_found"] is False
    assert _chunks(db, slug) == 0


def test_remove_orphaned_slug_deletes_chunks(tmp_path: Path) -> None:
    """The registry entry is gone; the chunks are not. `llmli rm <slug>` must clear them."""
    db, slug = _ingest(tmp_path)

    # Reproduce the incident: drop only the registry entry.
    import json

    reg_path = db / "llmli_registry.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    del reg[slug]
    reg_path.write_text(json.dumps(reg, indent=2), encoding="utf-8")
    assert slug not in {s["slug"] for s in list_silos(str(db))}
    assert _chunks(db, slug) > 0, "chunks should still be orphaned in Chroma"

    result = op_remove_silo(str(db), slug)

    assert result["not_found"] is True
    assert _chunks(db, slug) == 0, "orphaned chunks survived the removal"
