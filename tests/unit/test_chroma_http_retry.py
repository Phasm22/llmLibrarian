"""Transport retry for the Chroma HTTP client.

A `chroma run` restart makes calls fail for a second or two. Unretried, that
reaches the MCP caller as a tool error — and the caller is a model that may read
"tool failed" as "no such knowledge" rather than retrying. These tests pin what
is retried, what is not, and that the budget stays short enough that a genuinely
down server still fails fast.
"""

from __future__ import annotations

import errno

import httpx
import pytest

import chroma_client as cc


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(cc.time, "sleep", lambda s: slept.append(s))
    return slept


def _flaky(exc, fail_times):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise exc
        return "ok"

    fn.calls = calls
    return fn


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("truncated"),
        ConnectionResetError(),
        ConnectionRefusedError(),
        OSError(errno.EMFILE, "too many open files"),
        OSError(errno.ECONNRESET, "reset"),
    ],
)
def test_connection_level_failures_are_transient(exc):
    assert cc._is_transient_transport_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("bad where filter"),
        KeyError("collection missing"),
        RuntimeError("Error finding id"),
        OSError(errno.ENOENT, "no such file"),
    ],
)
def test_application_errors_are_not_retried(exc):
    """These will not fix themselves by sleeping — retrying just delays the error."""
    assert cc._is_transient_transport_error(exc) is False


def test_connect_errors_are_known_to_have_missed_the_server():
    assert cc._never_reached_server(httpx.ConnectError("refused")) is True
    assert cc._never_reached_server(ConnectionRefusedError()) is True
    # A read timeout may mean the request landed and is still being applied.
    assert cc._never_reached_server(httpx.ReadTimeout("slow")) is False


# ---------------------------------------------------------------------------
# Retry loop
# ---------------------------------------------------------------------------


def test_read_recovers_from_a_restart(_no_sleep):
    fn = _flaky(httpx.ConnectError("refused"), fail_times=2)
    assert cc._retry_transport(fn, label="t", replayable=True) == "ok"
    assert fn.calls["n"] == 3


def test_read_gives_up_after_the_attempt_budget(_no_sleep):
    fn = _flaky(httpx.ConnectError("refused"), fail_times=99)
    with pytest.raises(httpx.ConnectError):
        cc._retry_transport(fn, label="t", replayable=True)
    assert fn.calls["n"] == cc._RETRY_ATTEMPTS


def test_application_error_fails_on_the_first_call(_no_sleep):
    fn = _flaky(ValueError("bad filter"), fail_times=99)
    with pytest.raises(ValueError):
        cc._retry_transport(fn, label="t", replayable=True)
    assert fn.calls["n"] == 1
    assert _no_sleep == []


def test_write_replays_only_when_the_request_missed_the_server(_no_sleep):
    fn = _flaky(httpx.ConnectError("refused"), fail_times=1)
    assert cc._retry_transport(fn, label="add", replayable=False) == "ok"
    assert fn.calls["n"] == 2


def test_write_does_not_replay_a_timeout(_no_sleep):
    """The write may have landed and still be applying; replaying races it."""
    fn = _flaky(httpx.ReadTimeout("slow"), fail_times=1)
    with pytest.raises(httpx.ReadTimeout):
        cc._retry_transport(fn, label="add", replayable=False)
    assert fn.calls["n"] == 1


def test_total_wait_stays_under_a_second(_no_sleep):
    """A down server must fail fast — a slow error is worse than a quick one."""
    fn = _flaky(httpx.ConnectError("refused"), fail_times=99)
    with pytest.raises(httpx.ConnectError):
        cc._retry_transport(fn, label="t", replayable=True)
    assert sum(_no_sleep) < 1.0


def test_backoff_grows_and_is_capped(_no_sleep, monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HTTP_RETRIES", "6")
    fn = _flaky(httpx.ConnectError("refused"), fail_times=99)
    with pytest.raises(httpx.ConnectError):
        cc._retry_transport(fn, label="t", replayable=True)
    assert _no_sleep[0] == cc._RETRY_BASE_DELAY
    assert _no_sleep[-1] >= _no_sleep[0]
    assert max(_no_sleep) <= cc._RETRY_MAX_DELAY


def test_retries_are_configurable(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HTTP_RETRIES", "0")
    assert cc._retry_attempts() == 1
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HTTP_RETRIES", "not-a-number")
    assert cc._retry_attempts() == cc._RETRY_ATTEMPTS


# ---------------------------------------------------------------------------
# Collection proxy
# ---------------------------------------------------------------------------


class _FlakyCollection:
    name = "llmli"

    def __init__(self, fail_times=0, exc=None):
        self.fail_times = fail_times
        self.exc = exc or httpx.ConnectError("refused")
        self.calls = 0

    def query(self, **_kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return {"ids": [["a"]]}

    def add(self, **_kw):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return None


def test_collection_query_retries(_no_sleep):
    coll = _FlakyCollection(fail_times=2)
    assert cc._RetryingCollection(coll).query(query_texts=["x"]) == {"ids": [["a"]]}
    assert coll.calls == 3


def test_collection_add_replays_a_refused_connection(_no_sleep):
    """Chunk ids are deterministic, so a replayed add is idempotent."""
    coll = _FlakyCollection(fail_times=1)
    cc._RetryingCollection(coll).add(ids=["1"], documents=["d"])
    assert coll.calls == 2


def test_collection_add_does_not_replay_a_timeout(_no_sleep):
    coll = _FlakyCollection(fail_times=1, exc=httpx.ReadTimeout("slow"))
    with pytest.raises(httpx.ReadTimeout):
        cc._RetryingCollection(coll).add(ids=["1"], documents=["d"])
    assert coll.calls == 1


def test_proxy_passes_through_non_data_attributes():
    coll = _FlakyCollection()
    assert cc._RetryingCollection(coll).name == "llmli"


def test_embedded_mode_is_not_wrapped(monkeypatch):
    """No transport to fail — the proxy would be pure indirection."""
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    coll = _FlakyCollection()
    assert cc._SafeClient._wrap(coll) is coll


def test_http_mode_is_wrapped(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1")
    coll = _FlakyCollection()
    assert isinstance(cc._SafeClient._wrap(coll), cc._RetryingCollection)
