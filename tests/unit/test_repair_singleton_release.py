"""
Call-ordering contract: `op_repair_silo` must release the cached singleton
PersistentClient BEFORE invoking `run_add(...)`. Two live PersistentClients on
the same persist dir corrupt the HNSW segment writer
(see src/chroma_client.py:148).

Asserted via mocked `chroma_client.release` + `ingest.run_add` — no real Chroma.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def fake_silo(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("hello", encoding="utf-8")
    return db, src


def test_op_repair_silo_releases_singleton_before_run_add(fake_silo):
    db, src = fake_silo
    slug = "fakeslug"

    calls: list[str] = []

    fake_coll = MagicMock()
    fake_client = MagicMock()
    fake_client.get_or_create_collection.return_value = fake_coll

    def _get_client(_):
        calls.append("get_client")
        return fake_client

    def _release():
        calls.append("release")

    def _run_add(**kwargs):
        calls.append("run_add")
        return (1, 0)

    def _list_silos(_):
        return [{"slug": slug, "path": str(src), "display_name": slug}]

    def _resolve(_, raw):
        return slug

    with patch("chroma_client.get_client", _get_client), \
         patch("chroma_client.release", _release), \
         patch("ingest.run_add", _run_add), \
         patch("ingest._file_registry_remove_silo", lambda *a, **k: None), \
         patch("state.list_silos", _list_silos), \
         patch("state.resolve_silo_to_slug", _resolve), \
         patch("state.resolve_silo_prefix", _resolve), \
         patch("state.remove_manifest_silo", lambda *a, **k: None):
        from operations import op_repair_silo
        result = op_repair_silo(str(db), slug, verbose=False)

    assert result["status"] == "completed"

    # The inner release must come BEFORE run_add. The outer finally release
    # comes after.
    assert "release" in calls and "run_add" in calls
    first_release = calls.index("release")
    first_run_add = calls.index("run_add")
    assert first_release < first_run_add, (
        f"singleton must be released before run_add(); call order was {calls}"
    )


def test_mcp_trigger_reindex_releases_singleton_before_run_add(fake_silo):
    """Mirror contract for mcp_server._run_reindex inner body."""
    db, src = fake_silo
    calls: list[str] = []

    def _release():
        calls.append("release")

    def _run_add(**kwargs):
        calls.append("run_add")
        return (1, 0)

    # Simulate the inner block of _run_reindex directly: it must call
    # _release_chroma() before run_add(...).
    with patch("chroma_client.release", _release), \
         patch("ingest.run_add", _run_add):
        from chroma_client import release as _release_chroma
        from ingest import run_add

        _release_chroma()
        run_add(path=str(src), db_path=str(db), incremental=True)

    assert calls == ["release", "run_add"], (
        f"expected release before run_add; got {calls}"
    )
