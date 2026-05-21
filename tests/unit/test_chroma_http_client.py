from __future__ import annotations

from unittest.mock import MagicMock, patch

import chroma_client


def test_chroma_transport_mode_embedded_by_default(monkeypatch):
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    assert chroma_client.chroma_transport_mode() == "embedded"
    assert not chroma_client.is_http_mode()


def test_chroma_transport_mode_http_when_host_set(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1")
    assert chroma_client.chroma_transport_mode() == "http"
    assert chroma_client.is_http_mode()


def test_open_raw_client_uses_http_when_configured(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1")
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_PORT", "8000")
    fake = MagicMock()
    with patch.object(chroma_client, "check_chroma_server_reachable", return_value=(True, None)):
        with patch("chroma_client.chromadb.HttpClient", return_value=fake) as http_ctor:
            client = chroma_client._open_raw_client("/tmp/db")
    http_ctor.assert_called_once()
    assert client is fake


def test_writer_client_no_bump_generation_in_http_mode(monkeypatch, tmp_path):
    from contextlib import contextmanager

    db = str(tmp_path / "db")
    (tmp_path / "db").mkdir()
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1")
    monkeypatch.setenv("LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT", "1")
    fake_client = MagicMock()

    @contextmanager
    def _noop_lock(_db):
        yield

    with patch("chroma_lock.chroma_exclusive_lock", _noop_lock):
        with patch.object(chroma_client, "preflight_embedded_write", return_value=None):
            with patch.object(chroma_client, "_storage_preflight"):
                with patch.object(
                    chroma_client,
                    "get_client",
                    return_value=chroma_client._SafeClient(fake_client),
                ):
                    with patch.object(chroma_client, "bump_generation") as bump:
                        with chroma_client.writer_client(db) as client:
                            assert client is not None
                        bump.assert_not_called()
