"""
Regression test for ChromaDB SQLite ↔ HNSW desync.

User-reported symptom: after `repair_silo` (or MCP `trigger_reindex` running
while a singleton client is already alive in the same process), the silo's
SQLite metadata records N chunk IDs but only M < N are reachable through the
HNSW index. e.g. 441 SQLite IDs / 213 HNSW-reachable.

Hypothesis: `mcp_server.trigger_reindex` and `operations.op_repair_silo` both
keep the cached `chroma_client.get_client(...)` singleton alive while calling
`run_add(...)`, which opens a SECOND `PersistentClient` inside `writer_client(...)`.
Two live `PersistentClient` instances on the same persist dir corrupt the HNSW
segment writer (see comment in src/chroma_client.py:148-159).

These tests reproduce that pattern on a synthetic fixture and assert the
invariant: every chunk ID returned by SQLite (`collection.get(where=...)`)
must be reachable through the HNSW path (`collection.query(query_embeddings=...)`).

Tests are expected to FAIL on `main` (no fix yet) and PASS once the singleton
is released before `run_add(...)`.
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest

from chroma_client import get_client, release as release_chroma_client
from chroma_lock import chroma_exclusive_lock
from constants import LLMLI_COLLECTION
from embeddings import get_embedding_function
from ingest import run_add
from operations import op_repair_silo
from state import slugify


# ---------------------------------------------------------------------------
# Fixture: a small folder with enough variety to produce many chunks per file.
# ---------------------------------------------------------------------------


_SEED_TEXT = (
    "The quick brown fox jumps over the lazy dog. "
    "Pack my box with five dozen liquor jugs. "
    "How vexingly quick daft zebras jump! "
    "Sphinx of black quartz, judge my vow. "
)


# NOTE: ADD_DEFAULT_EXCLUDE contains the substring "tmp" / "/tmp", so any
# fixture under pytest's default tmp_path (which lives in /tmp/pytest-of-*) is
# silently excluded by collect_files and `run_add` indexes zero files. Use a
# non-/tmp scratch root under the user's cache dir instead.
_SCRATCH_ROOT = Path.home() / ".cache" / "llmli_hnsw_consistency_tests"


@pytest.fixture
def scratch_dir() -> Path:
    base = _SCRATCH_ROOT / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)
        # Best-effort cleanup of empty parent.
        try:
            _SCRATCH_ROOT.rmdir()
        except OSError:
            pass


def _make_fixture(root: Path, n_files: int = 12, paragraphs_per_file: int = 8) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n_files):
        p = root / f"doc_{i:02d}.md"
        body_parts: list[str] = [f"# Document {i}\n"]
        for j in range(paragraphs_per_file):
            # Vary the text so chunks are not trivially deduped or all-identical.
            body_parts.append(
                f"## Section {j}\n\n"
                + (_SEED_TEXT * 6)
                + f"\n\nDocument {i} section {j} unique marker: alpha-{i}-{j}-bravo.\n\n"
            )
        p.write_text("\n".join(body_parts), encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Consistency invariant.
# ---------------------------------------------------------------------------


import pickle
import sqlite3


def _sqlite_ids_for_silo(collection: Any, silo_slug: str) -> list[str]:
    """Authoritative SQLite truth: every chunk recorded for this silo."""
    result = collection.get(where={"silo": silo_slug}, include=["metadatas"])
    return list(result.get("ids") or [])


def _hnsw_id_set(db_root: Path) -> set[str]:
    """Union `id_to_label` across every HNSW segment under db_root.

    This is the same probe used to detect the original tjs-pc 228/441 split:
    direct inspection of `index_metadata.pickle` for the set of UUIDs the
    HNSW segment writer believes it owns. An ID present in SQLite's
    `embeddings` rows but missing from this set is a real desync (unless it
    is still queued in `embeddings_queue`, which is normal compaction lag).
    """
    ids: set[str] = set()
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


def _queued_id_set(db_root: Path) -> set[str]:
    """IDs sitting in `embeddings_queue` — Chroma compaction lag, not desync."""
    sqlite_path = db_root / "chroma.sqlite3"
    if not sqlite_path.exists():
        return set()
    con = sqlite3.connect(str(sqlite_path))
    try:
        rows = con.execute("SELECT id FROM embeddings_queue").fetchall()
    finally:
        con.close()
    return {r[0] for r in rows if r and r[0]}


def assert_silo_hnsw_consistent(collection: Any, silo_slug: str, db_path: Path) -> None:
    """Assert that every SQLite-owned ID for this silo is either in HNSW's
    id_to_label or still pending in embeddings_queue. Anything else is
    a true SQLite ↔ HNSW desync (the bug we are guarding against)."""
    sqlite_ids = set(_sqlite_ids_for_silo(collection, silo_slug))
    assert sqlite_ids, f"silo {silo_slug!r} has no chunks in SQLite — baseline ingest failed"
    db_root = Path(db_path).resolve()
    hnsw_ids = _hnsw_id_set(db_root)
    queued_ids = _queued_id_set(db_root)
    missing_from_hnsw = sqlite_ids - hnsw_ids
    truly_missing = missing_from_hnsw - queued_ids
    if truly_missing:
        sample = sorted(truly_missing)[:5]
        pytest.fail(
            f"SQLite ↔ HNSW desync in silo {silo_slug!r}: "
            f"{len(sqlite_ids)} SQLite IDs, "
            f"{len(missing_from_hnsw)} missing from HNSW id_to_label "
            f"({len(missing_from_hnsw & queued_ids)} accounted for by embeddings_queue), "
            f"{len(truly_missing)} truly missing. Sample: {sample}"
        )


# ---------------------------------------------------------------------------
# Helpers to set up the silo and obtain the live singleton collection handle.
# ---------------------------------------------------------------------------


def _baseline_ingest(fixture_dir: Path, db_path: Path) -> str:
    files_indexed, failures = run_add(
        path=fixture_dir,
        db_path=db_path,
        incremental=False,
    )
    assert failures == 0, f"baseline ingest produced {failures} failures"
    assert files_indexed > 0
    # `run_add` opens a fresh writer client and drops it. Release any cached
    # singleton state so each test starts from a clean slot before warming it.
    release_chroma_client()
    return slugify(fixture_dir.name, str(fixture_dir))


def _warm_singleton_handle(db_path: Path, silo_slug: str) -> Any:
    """
    Reproduce the real-world state where the cached singleton already has the
    HNSW handle materialised, exactly as MCP does after `list_silos` and
    `query_personal_knowledge`.
    """
    client = get_client(str(Path(db_path).resolve()))
    ef = get_embedding_function(batch_size=8)
    coll = client.get_or_create_collection(name=LLMLI_COLLECTION, embedding_function=ef)
    # Touch both code paths — get() (SQLite) and query() (HNSW).
    coll.peek(1)
    coll.query(query_texts=["seed"], n_results=1)
    # Sanity: baseline must be consistent before we attempt to repro the break.
    assert_silo_hnsw_consistent(coll, silo_slug, db_path)
    return coll


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_repair_silo_preserves_hnsw_consistency(scratch_dir: Path) -> None:
    """
    Reproduce the user-reported failure mode: `op_repair_silo` is invoked
    while a singleton client is alive (mirrors MCP `repair_silo` after any
    prior read tool call). After repair, the rebuilt silo must remain
    fully reachable through HNSW.
    """
    fixture_dir = _make_fixture(scratch_dir / "src_repair")
    db_path = scratch_dir / "db"

    silo_slug = _baseline_ingest(fixture_dir, db_path)

    # Materialise the singleton's HNSW handle BEFORE op_repair_silo.
    coll = _warm_singleton_handle(db_path, silo_slug)

    # Run the repair while the singleton is alive — this is the bug condition.
    result = op_repair_silo(str(db_path), silo_slug, verbose=False)
    assert result.get("status") == "completed", f"repair failed: {result}"
    assert result.get("failures", 0) == 0

    # The singleton may now be stale because the writer client mutated the DB
    # from another handle. Drop it and re-open to verify the on-disk state.
    release_chroma_client()
    fresh_coll = _warm_singleton_handle(db_path, silo_slug)
    assert_silo_hnsw_consistent(fresh_coll, silo_slug, db_path)


@pytest.mark.integration
def test_trigger_reindex_pattern_preserves_consistency(scratch_dir: Path) -> None:
    """
    Reproduce the MCP `trigger_reindex` pattern: singleton client materialised
    by prior MCP tool calls, then `run_add(...)` invoked while the singleton
    is still alive.
    """
    fixture_dir = _make_fixture(scratch_dir / "src_trigger")
    db_path = scratch_dir / "db"

    silo_slug = _baseline_ingest(fixture_dir, db_path)

    # Warm singleton — mirrors prior list_silos / query_personal_knowledge.
    coll = _warm_singleton_handle(db_path, silo_slug)

    # Now run the same sequence MCP does in _run_reindex.
    with chroma_exclusive_lock(str(db_path)):
        run_add(path=fixture_dir, db_path=db_path, incremental=True)

    # Verify on-disk consistency through a fresh handle.
    release_chroma_client()
    fresh_coll = _warm_singleton_handle(db_path, silo_slug)
    assert_silo_hnsw_consistent(fresh_coll, silo_slug, db_path)


@pytest.mark.integration
def test_baseline_ingest_alone_is_consistent(scratch_dir: Path) -> None:
    """
    Control test: a plain `run_add(..., incremental=False)` with no concurrent
    singleton should produce a fully consistent silo. If THIS fails, the
    repro is invalid (the bug isn't specific to singleton-coexistence).
    """
    fixture_dir = _make_fixture(scratch_dir / "src_baseline")
    db_path = scratch_dir / "db"

    silo_slug = _baseline_ingest(fixture_dir, db_path)
    coll = _warm_singleton_handle(db_path, silo_slug)
    assert_silo_hnsw_consistent(coll, silo_slug, db_path)
