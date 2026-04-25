"""Regression coverage for chroma_client.release() and the singleton contract.

Background: ChromaDB 1.4+ segfaults if `clear_system_cache()` is called
while background tokio threads are still live. The current `release()`
intentionally drops only the Python-side references and never calls
`clear_system_cache`. These tests lock that contract in.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import chroma_client


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Each test starts with a clean client cache."""
    monkeypatch.setattr(chroma_client, "_clients", {})
    monkeypatch.setattr(chroma_client, "_fallback_warned", set())
    yield


def test_release_clears_client_cache():
    chroma_client._clients["/tmp/x"] = MagicMock(name="client_x")
    chroma_client._clients["/tmp/y"] = MagicMock(name="client_y")

    chroma_client.release()

    assert chroma_client._clients == {}


def test_release_is_idempotent_on_empty_cache():
    chroma_client.release()
    chroma_client.release()
    assert chroma_client._clients == {}


def test_release_does_not_call_clear_system_cache(monkeypatch):
    """Regression: clear_system_cache caused SIGSEGV on Chroma 1.4+.

    Spy on every plausible import path; if release ever flips back to
    invoking it, this test fires.
    """
    called = []

    fake_module = MagicMock()
    fake_module.SharedSystemClient.clear_system_cache.side_effect = (
        lambda: called.append("shared_system_clear")
    )
    monkeypatch.setitem(__import__("sys").modules, "chromadb.api.client", fake_module)

    chroma_client._clients["/tmp/x"] = MagicMock()
    chroma_client.release()

    assert called == [], "release() must not call clear_system_cache (SIGSEGV regression)"


def test_release_does_not_invoke_client_close_or_destructor(monkeypatch):
    """release() should drop references only — not actively call .close() or
    .reset() on the Rust client (which would also tear down tokio threads)."""
    client = MagicMock()
    chroma_client._clients["/tmp/x"] = client

    chroma_client.release()

    client.close.assert_not_called()
    client.reset.assert_not_called()


def test_get_client_singleton_per_db_path(monkeypatch):
    """Repeated calls with the same db_path yield the same _SafeClient."""
    fake_persistent = MagicMock(return_value=MagicMock(name="raw_client"))
    monkeypatch.setattr(chroma_client.chromadb, "PersistentClient", fake_persistent)
    monkeypatch.setattr(chroma_client, "_storage_preflight", lambda _p: None)

    a = chroma_client.get_client("/tmp/db1")
    b = chroma_client.get_client("/tmp/db1")

    assert a is b
    assert fake_persistent.call_count == 1


def test_get_client_distinct_per_db_path(monkeypatch):
    fake_persistent = MagicMock(side_effect=lambda **_kw: MagicMock())
    monkeypatch.setattr(chroma_client.chromadb, "PersistentClient", fake_persistent)
    monkeypatch.setattr(chroma_client, "_storage_preflight", lambda _p: None)

    a = chroma_client.get_client("/tmp/db1")
    b = chroma_client.get_client("/tmp/db2")

    assert a is not b
    assert fake_persistent.call_count == 2


def test_release_then_get_client_returns_fresh_instance(monkeypatch):
    """Critical: after release, the next get_client must rebuild — otherwise
    callers retain a stale reference into a half-torn-down client."""
    raw_clients = [MagicMock(name="raw1"), MagicMock(name="raw2")]
    fake_persistent = MagicMock(side_effect=raw_clients)
    monkeypatch.setattr(chroma_client.chromadb, "PersistentClient", fake_persistent)
    monkeypatch.setattr(chroma_client, "_storage_preflight", lambda _p: None)

    first = chroma_client.get_client("/tmp/db")
    chroma_client.release()
    second = chroma_client.get_client("/tmp/db")

    assert first is not second
    assert fake_persistent.call_count == 2


def test_get_client_runs_preflight_only_on_creation(monkeypatch):
    """Singleton path skips preflight; only the first get_client call hits it."""
    monkeypatch.setattr(chroma_client.chromadb, "PersistentClient", MagicMock(return_value=MagicMock()))
    preflight_calls: list[str] = []
    monkeypatch.setattr(
        chroma_client,
        "_storage_preflight",
        lambda p: preflight_calls.append(p),
    )

    chroma_client.get_client("/tmp/db")
    chroma_client.get_client("/tmp/db")
    chroma_client.get_client("/tmp/db")

    assert preflight_calls == ["/tmp/db"]
