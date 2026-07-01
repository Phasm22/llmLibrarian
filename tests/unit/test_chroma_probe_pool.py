"""Coverage for the pooled HTTP probe used by check_chroma_server_reachable /
_mcp_healthz_info, and the rate-limited heartbeat probe in get_client.

Motivation: prior to pooling, every reachability check opened a fresh
http.client.HTTPConnection, immediately closed it, and dropped the socket
into TIME_WAIT — under load this exhausted the ephemeral port range. And
every get_client() call fired a network heartbeat, so hot code paths
paid one round-trip per client access.
"""
from __future__ import annotations

import http.client
from unittest.mock import MagicMock, patch

import pytest

import chroma_client


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(chroma_client, "_clients", {})
    monkeypatch.setattr(chroma_client, "_heartbeat_ok_at", {})
    monkeypatch.setattr(chroma_client, "_probe_conns", {})
    yield
    chroma_client._close_probe_pool()


def _fake_response(status: int, body: bytes = b"", will_close: bool = False) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.will_close = will_close
    return resp


def test_probe_http_reuses_connection_across_calls(monkeypatch):
    """Successive probes on the same target reuse one HTTPConnection."""
    fake_conn = MagicMock(spec=http.client.HTTPConnection)
    fake_conn.getresponse.return_value = _fake_response(200, b"ok")
    ctor = MagicMock(return_value=fake_conn)
    monkeypatch.setattr(chroma_client, "_make_probe_conn", ctor)

    for _ in range(10):
        status, body, err = chroma_client._probe_http("127.0.0.1", 8000, False, "/ping")
        assert (status, body, err) == (200, b"ok", None)

    assert ctor.call_count == 1
    assert fake_conn.request.call_count == 10


def test_probe_http_drops_connection_when_server_closes(monkeypatch):
    """Server-side Connection: close forces a fresh socket on the next probe."""
    resp1 = _fake_response(200, b"ok", will_close=True)
    resp2 = _fake_response(200, b"ok", will_close=False)
    conn1 = MagicMock(spec=http.client.HTTPConnection)
    conn2 = MagicMock(spec=http.client.HTTPConnection)
    conn1.getresponse.return_value = resp1
    conn2.getresponse.return_value = resp2
    ctor = MagicMock(side_effect=[conn1, conn2])
    monkeypatch.setattr(chroma_client, "_make_probe_conn", ctor)

    chroma_client._probe_http("h", 1, False, "/a")
    chroma_client._probe_http("h", 1, False, "/b")

    assert ctor.call_count == 2
    conn1.close.assert_called_once()


def test_probe_http_retries_once_on_stale_socket(monkeypatch):
    """A pooled connection that raises on request() is dropped and rebuilt."""
    dead = MagicMock(spec=http.client.HTTPConnection)
    dead.request.side_effect = ConnectionResetError("stale")
    fresh = MagicMock(spec=http.client.HTTPConnection)
    fresh.getresponse.return_value = _fake_response(200, b"ok")
    # Only the retry attempt calls the ctor (initial dead is pre-warmed).
    ctor = MagicMock(return_value=fresh)
    monkeypatch.setattr(chroma_client, "_make_probe_conn", ctor)

    chroma_client._probe_conns[("h", 1, False)] = dead

    status, body, err = chroma_client._probe_http("h", 1, False, "/x")

    assert (status, body, err) == (200, b"ok", None)
    dead.close.assert_called_once()
    assert ctor.call_count == 1


def test_probe_http_returns_error_after_two_failures(monkeypatch):
    conn = MagicMock(spec=http.client.HTTPConnection)
    conn.request.side_effect = OSError("boom")
    monkeypatch.setattr(chroma_client, "_make_probe_conn", MagicMock(return_value=conn))

    status, body, err = chroma_client._probe_http("h", 1, False, "/x")

    assert status == 0
    assert body == b""
    assert err == "boom"


def test_check_chroma_server_reachable_uses_pooled_probe(monkeypatch):
    calls: list[str] = []

    def fake(host, port, ssl, path, *, headers=None, timeout=2.0):
        calls.append(path)
        return 200, b"", None

    monkeypatch.setattr(chroma_client, "_probe_http", fake)
    ok, err = chroma_client.check_chroma_server_reachable("127.0.0.1", 8000, ssl=False)
    assert ok and err is None
    assert calls == ["/api/v2/heartbeat"]


def test_check_chroma_server_reachable_surfaces_transport_error(monkeypatch):
    def fake(host, port, ssl, path, *, headers=None, timeout=2.0):
        return 0, b"", "connection refused"

    monkeypatch.setattr(chroma_client, "_probe_http", fake)
    ok, err = chroma_client.check_chroma_server_reachable("h", 1)
    assert ok is False
    assert err == "connection refused"


def test_mcp_healthz_info_uses_pooled_probe(monkeypatch):
    payload = b'{"ok": true, "db_path": "/tmp/db"}'

    def fake(host, port, ssl, path, *, headers=None, timeout=1.0):
        assert path == "/healthz"
        return 200, payload, None

    monkeypatch.setattr(chroma_client, "_probe_http", fake)
    up, db = chroma_client._mcp_healthz_info()
    assert up is True
    assert db and db.endswith("/tmp/db")


def test_get_client_rate_limits_heartbeat(monkeypatch):
    """Repeated get_client() calls within the heartbeat interval must not
    round-trip to the chroma server."""
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1")
    monkeypatch.setattr(chroma_client, "_storage_preflight", lambda _p: None)
    fake_http = MagicMock()
    fake_http.heartbeat = MagicMock(return_value=1)
    monkeypatch.setattr(chroma_client, "_open_raw_client", lambda _p: fake_http)

    for _ in range(20):
        chroma_client.get_client("/tmp/db")

    # First call constructs (no heartbeat); subsequent calls stay under
    # the min interval → heartbeat still never called.
    fake_http.heartbeat.assert_not_called()


def test_get_client_reprobes_after_interval_elapses(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1")
    monkeypatch.setattr(chroma_client, "_storage_preflight", lambda _p: None)
    fake_http = MagicMock()
    fake_http.heartbeat = MagicMock(return_value=1)
    monkeypatch.setattr(chroma_client, "_open_raw_client", lambda _p: fake_http)
    # Force interval to zero so every get_client re-probes.
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HEARTBEAT_INTERVAL_SEC", "0")

    chroma_client.get_client("/tmp/db")  # constructs
    chroma_client.get_client("/tmp/db")  # heartbeat probe
    chroma_client.get_client("/tmp/db")  # heartbeat probe

    assert fake_http.heartbeat.call_count == 2


def test_get_client_drops_cache_on_failed_heartbeat(monkeypatch):
    """A failed heartbeat evicts the cached client and clears its probe
    timestamp so the next call rebuilds."""
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1")
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HEARTBEAT_INTERVAL_SEC", "0")
    monkeypatch.setattr(chroma_client, "_storage_preflight", lambda _p: None)

    stale = MagicMock()
    stale.heartbeat = MagicMock(side_effect=RuntimeError("server restarted"))
    fresh = MagicMock()
    fresh.heartbeat = MagicMock(return_value=1)
    calls = iter([stale, fresh])
    monkeypatch.setattr(chroma_client, "_open_raw_client", lambda _p: next(calls))

    first = chroma_client.get_client("/tmp/db")
    second = chroma_client.get_client("/tmp/db")

    assert first._client is stale
    assert second._client is fresh
    assert "/tmp/db" in chroma_client._heartbeat_ok_at


def test_release_closes_probe_pool(monkeypatch):
    conn = MagicMock(spec=http.client.HTTPConnection)
    chroma_client._probe_conns[("h", 1, False)] = conn

    chroma_client.release()

    assert chroma_client._probe_conns == {}
    conn.close.assert_called_once()


def test_heartbeat_min_interval_env_override(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HEARTBEAT_INTERVAL_SEC", "12.5")
    assert chroma_client._heartbeat_min_interval() == 12.5

    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HEARTBEAT_INTERVAL_SEC", "not-a-number")
    assert chroma_client._heartbeat_min_interval() == 5.0

    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HEARTBEAT_INTERVAL_SEC", raising=False)
    assert chroma_client._heartbeat_min_interval() == 5.0
