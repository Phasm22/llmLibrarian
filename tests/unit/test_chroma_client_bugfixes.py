"""Regressions for four latent bugs in the Chroma client and lock layers.

Each pins a failure that was live before the fix, not a refactor detail.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

import chroma_client
import chroma_lock


# ---------------------------------------------------------------------------
# One cache key per persist directory
# ---------------------------------------------------------------------------


def test_same_dir_spelled_differently_shares_one_client(monkeypatch, tmp_path):
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
    monkeypatch.setattr(chroma_client, "_open_raw_client", lambda p: opened.append(p) or object())
    monkeypatch.setattr(chroma_client, "_storage_preflight", lambda p: None)
    chroma_client.release()

    a = chroma_client.get_client(str(db))
    b = chroma_client.get_client("./db")
    c = chroma_client.get_client(str(db) + "/")

    assert a is b is c
    assert len(opened) == 1, f"opened a client per spelling: {opened}"
    chroma_client.release()


# ---------------------------------------------------------------------------
# The embedded-write guard must not fail open when auth is enabled
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

    This previously read as "MCP is down" and let the embedded write proceed
    against a DB the MCP server might hold open.
    """
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT", "")
    monkeypatch.setattr(chroma_client, "_probe_http", lambda *a, **k: (401, b"unauthorized", None))

    with patch.object(chroma_client, "_active_watch_processes_for_db", return_value=[]):
        err = chroma_client.preflight_embedded_write(str(tmp_path))

    assert err is not None
    assert "LLMLIBRARIAN_MCP_AUTH_TOKEN" in err


def test_preflight_allows_when_nothing_is_listening(monkeypatch, tmp_path):
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT", "")
    monkeypatch.setattr(
        chroma_client, "_probe_http", lambda *a, **k: (0, b"", "connection refused")
    )
    with patch.object(chroma_client, "_active_watch_processes_for_db", return_value=[]):
        assert chroma_client.preflight_embedded_write(str(tmp_path)) is None


def test_preflight_stays_permissive_for_old_healthz_without_db_path(monkeypatch, tmp_path):
    """200 with no db_path is genuine version skew, not an auth failure."""
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT", "")
    monkeypatch.setattr(chroma_client, "_probe_http", lambda *a, **k: (200, b'{"ok": true}', None))
    with patch.object(chroma_client, "_active_watch_processes_for_db", return_value=[]):
        assert chroma_client.preflight_embedded_write(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# One meaning for the lock-timeout sentinel
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_lock_env(monkeypatch):
    for name in (
        "LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS",
        "LLMLIBRARIAN_CHROMA_WRITE_LOCK_TIMEOUT_SECONDS",
        "LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_zero_means_block_forever_for_flock(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "0")
    assert chroma_lock._lock_timeout_seconds() is None
    assert chroma_lock._lock_timeout_seconds(write=True) is None


def test_zero_means_block_forever_for_the_mcp_mutex(monkeypatch):
    """The MCP mutex read this as a literal 0.0-second timeout — the opposite."""
    import mcp_server

    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "0")
    assert mcp_server._mcp_lock_timeout_seconds() is None


def test_writers_keep_their_longer_default(monkeypatch):
    read = chroma_lock._lock_timeout_seconds()
    write = chroma_lock._lock_timeout_seconds(write=True)
    assert write > read


def test_write_specific_override_applies_to_writers_only(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_WRITE_LOCK_TIMEOUT_SECONDS", "42")
    assert chroma_lock._lock_timeout_seconds(write=True) == 42.0
    assert chroma_lock._lock_timeout_seconds() != 42.0


def test_caller_specific_override_wins(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS", "7")
    assert chroma_lock._lock_timeout_seconds(env_name="LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS") == 7.0


def test_caller_specific_override_falls_through_when_unset(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "30")
    assert (
        chroma_lock._lock_timeout_seconds(env_name="LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS") == 30.0
    )


def test_retry_hint_survives_a_blocking_timeout(monkeypatch):
    """retry_after halved a None and raised TypeError before this."""
    import mcp_server

    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "0")
    assert mcp_server._retry_after_seconds() >= 1


def test_retry_hint_is_half_the_budget(monkeypatch):
    import mcp_server

    monkeypatch.setenv("LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS", "10")
    assert mcp_server._retry_after_seconds() == 5


def test_mcp_lock_acquire_honours_the_blocking_sentinel(monkeypatch):
    """A blocking acquire must not be passed timeout=None as a float."""
    import mcp_server

    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "0")
    mcp_server._acquire_chroma_lock("smoke")
    mcp_server._chroma_lock.release()


def test_mcp_lock_timeout_message_names_the_operation(monkeypatch):
    import mcp_server

    monkeypatch.setenv("LLMLIBRARIAN_MCP_LOCK_TIMEOUT_SECONDS", "0.01")
    mcp_server._chroma_lock.acquire()
    try:
        with pytest.raises(TimeoutError, match="repair_silo"):
            mcp_server._acquire_chroma_lock("repair_silo")
    finally:
        mcp_server._chroma_lock.release()


# ---------------------------------------------------------------------------
# A detected-stale client is never handed back
# ---------------------------------------------------------------------------


def _bump_generation_past(db) -> None:
    chroma_client.bump_generation(str(db))
    gen = chroma_client._generation_path(str(db))
    st = gen.stat()
    os.utime(gen, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_stale_generation_rebuilds_when_exit_disabled(monkeypatch, tmp_path):
    """With exit-on-stale off — the default for every CLI, pal and watcher —
    the old code detected the stale generation and returned the client anyway."""
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.delenv("LLMLIBRARIAN_EXIT_ON_STALE_GENERATION", raising=False)
    db = tmp_path / "db"
    db.mkdir()

    opened: list[str] = []
    monkeypatch.setattr(chroma_client, "_open_raw_client", lambda p: opened.append(p) or object())
    monkeypatch.setattr(chroma_client, "_storage_preflight", lambda p: None)
    chroma_client.release()

    first = chroma_client.get_client(str(db))
    assert chroma_client.get_client(str(db)) is first
    assert len(opened) == 1

    _bump_generation_past(db)

    second = chroma_client.get_client(str(db))
    assert second is not first, "stale client was handed back to the caller"
    assert len(opened) == 2
    chroma_client.release()


def test_stale_generation_exits_when_enabled(monkeypatch, tmp_path):
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_EXIT_ON_STALE_GENERATION", "1")
    db = tmp_path / "db"
    db.mkdir()

    monkeypatch.setattr(chroma_client, "_open_raw_client", lambda p: object())
    monkeypatch.setattr(chroma_client, "_storage_preflight", lambda p: None)
    chroma_client.release()
    chroma_client.get_client(str(db))

    _bump_generation_past(db)

    with pytest.raises(SystemExit) as exc:
        chroma_client.get_client(str(db))
    assert exc.value.code == 99
    chroma_client.release()
