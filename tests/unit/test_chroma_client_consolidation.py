"""Regressions for the Chroma-layer consolidation pass.

Each test here pins a bug that was live before the consolidation, not a
refactor detail. See the plan's "Verified bugs" table.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import chroma_client
import chroma_lock


# ---------------------------------------------------------------------------
# B1 — one cache key per persist directory
# ---------------------------------------------------------------------------


def test_get_client_same_dir_different_spellings_shares_one_client(monkeypatch, tmp_path):
    """Two spellings of one persist dir must not yield two PersistentClients.

    Keying the client cache on the raw string handed a second live Rust HNSW
    handle to the second caller — the concurrent-handle SIGSEGV this module
    exists to prevent.
    """
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.chdir(tmp_path)

    opened: list[str] = []

    def fake_open(path):
        opened.append(path)
        return object()

    monkeypatch.setattr(chroma_client, "_open_raw_client", fake_open)
    monkeypatch.setattr(chroma_client, "_storage_preflight", lambda p: None)
    chroma_client.release()

    a = chroma_client.get_client(str(db))
    b = chroma_client.get_client("./db")
    c = chroma_client.get_client(str(db) + "/")

    assert a is b is c
    assert len(opened) == 1, f"opened a client per spelling: {opened}"
    chroma_client.release()


def test_release_clears_cache_for_unresolved_spelling(monkeypatch, tmp_path):
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.chdir(tmp_path)

    opened: list[str] = []
    monkeypatch.setattr(chroma_client, "_open_raw_client", lambda p: opened.append(p) or object())
    monkeypatch.setattr(chroma_client, "_storage_preflight", lambda p: None)

    chroma_client.release()
    chroma_client.get_client("./db")
    chroma_client.release()
    chroma_client.get_client(str(db))
    assert len(opened) == 2
    chroma_client.release()


# ---------------------------------------------------------------------------
# B2 — the embedded-write guard must not fail open when auth is enabled
# ---------------------------------------------------------------------------


def test_healthz_probe_sends_auth_token(monkeypatch):
    monkeypatch.delenv("LLMLIBRARIAN_MCP_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_MCP_AUTH_TOKEN", "s3cret")
    seen: dict = {}

    def fake(host, port, ssl, path, *, headers=None, timeout=1.0):
        seen["headers"] = headers or {}
        return 200, b'{"ok": true, "db_path": "/tmp/db"}', None

    monkeypatch.setattr(chroma_client, "_probe_http", fake)
    chroma_client._mcp_healthz_info()
    assert seen["headers"].get("Authorization") == "Bearer s3cret"


def test_healthz_probe_accepts_legacy_bearer_token_name(monkeypatch):
    """pal still writes the older LLMLIBRARIAN_MCP_BEARER_TOKEN spelling."""
    monkeypatch.delenv("LLMLIBRARIAN_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_MCP_BEARER_TOKEN", "legacy-tok")
    seen: dict = {}

    def fake(host, port, ssl, path, *, headers=None, timeout=1.0):
        seen["headers"] = headers or {}
        return 200, b'{"ok": true, "db_path": "/tmp/db"}', None

    monkeypatch.setattr(chroma_client, "_probe_http", fake)
    chroma_client._mcp_healthz_info()
    assert seen["headers"].get("Authorization") == "Bearer legacy-tok"


def test_preflight_blocks_when_healthz_rejects_credentials(monkeypatch, tmp_path):
    """A 401 means a live MCP process we cannot identify — fail closed.

    Previously this returned (False, None), which read as "MCP is down" and let
    the embedded write proceed against a DB the MCP server might hold open.
    """
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT", "")

    monkeypatch.setattr(
        chroma_client, "_probe_http",
        lambda *a, **k: (401, b"unauthorized", None),
    )
    with patch.object(chroma_client, "active_watchers_for_db", return_value=[]):
        err = chroma_client.preflight_embedded_write(str(tmp_path))

    assert err is not None
    assert "LLMLIBRARIAN_MCP_AUTH_TOKEN" in err


def test_preflight_allows_when_nothing_is_listening(monkeypatch, tmp_path):
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT", "")
    monkeypatch.setattr(
        chroma_client, "_probe_http",
        lambda *a, **k: (0, b"", "connection refused"),
    )
    with patch.object(chroma_client, "active_watchers_for_db", return_value=[]):
        assert chroma_client.preflight_embedded_write(str(tmp_path)) is None


def test_preflight_stays_permissive_for_old_healthz_without_db_path(monkeypatch, tmp_path):
    """200 with no db_path is genuine version skew, not an auth failure."""
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT", "")
    monkeypatch.setattr(
        chroma_client, "_probe_http",
        lambda *a, **k: (200, b'{"ok": true}', None),
    )
    with patch.object(chroma_client, "active_watchers_for_db", return_value=[]):
        assert chroma_client.preflight_embedded_write(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# B3 — one meaning for LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS=0
# ---------------------------------------------------------------------------


def test_lock_timeout_zero_means_block_forever(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "0")
    assert chroma_lock.lock_timeout_seconds() is None


def test_lock_timeout_specific_env_wins(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS", "5")
    assert chroma_lock.lock_timeout_seconds("LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS") == 5.0


def test_lock_timeout_falls_back_to_shared_env(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "30")
    monkeypatch.delenv("LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS", raising=False)
    assert chroma_lock.lock_timeout_seconds("LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS") == 30.0


def test_lock_timeout_zero_propagates_to_mcp_layer(monkeypatch):
    """The MCP mutex used to read this as a literal 0.0-second timeout."""
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "0")
    monkeypatch.delenv("LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS", raising=False)
    assert chroma_lock.lock_timeout_seconds("LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS") is None


# ---------------------------------------------------------------------------
# B4 — a detected-stale client is never handed back
# ---------------------------------------------------------------------------


def _prime_client(monkeypatch, db, opened):
    monkeypatch.setattr(chroma_client, "_open_raw_client", lambda p: opened.append(p) or object())
    monkeypatch.setattr(chroma_client, "_storage_preflight", lambda p: None)
    chroma_client.release()
    return chroma_client.get_client(str(db))


def test_stale_generation_rebuilds_when_exit_disabled(monkeypatch, tmp_path):
    """With exit-on-stale off (every CLI/pal/watcher process), the old code
    detected the stale generation and then returned the stale client anyway."""
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.delenv("LLMLIBRARIAN_EXIT_ON_STALE_GENERATION", raising=False)
    db = tmp_path / "db"
    db.mkdir()

    opened: list[str] = []
    first = _prime_client(monkeypatch, db, opened)
    assert chroma_client.get_client(str(db)) is first
    assert len(opened) == 1

    chroma_client.bump_generation(str(db))
    # Generation files compare by mtime; force a strictly newer value.
    gen = chroma_client._generation_path(str(db))
    import os as _os
    st = gen.stat()
    _os.utime(gen, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    second = chroma_client.get_client(str(db))
    assert second is not first, "stale client was handed back to the caller"
    assert len(opened) == 2
    chroma_client.release()


def test_stale_generation_exits_when_enabled(monkeypatch, tmp_path):
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_EXIT_ON_STALE_GENERATION", "1")
    db = tmp_path / "db"
    db.mkdir()

    opened: list[str] = []
    _prime_client(monkeypatch, db, opened)

    chroma_client.bump_generation(str(db))
    gen = chroma_client._generation_path(str(db))
    import os as _os
    st = gen.stat()
    _os.utime(gen, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    with pytest.raises(SystemExit) as exc:
        chroma_client.get_client(str(db))
    assert exc.value.code == 99
    chroma_client.release()


# ---------------------------------------------------------------------------
# Dead surface stays dead
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["get_collection", "exit_if_stale", "check_for_writer_changes"],
)
def test_removed_helpers_stay_removed(name):
    """All three had zero production callers; get_client inlines the only
    generation comparison that mattered. Re-adding one means re-adding a second
    way to do something this module already does."""
    assert not hasattr(chroma_client, name)
