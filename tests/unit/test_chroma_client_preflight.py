from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import pytest

import chroma_client


@contextmanager
def _embedded_mode():
    with patch.dict("os.environ", {}, clear=False):
        for key in ("LLMLIBRARIAN_CHROMA_HOST",):
            import os
            os.environ.pop(key, None)
        yield


def test_preflight_skipped_in_http_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("LLMLIBRARIAN_CHROMA_HOST", "127.0.0.1")
    monkeypatch.setenv("LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT", "")
    with patch.object(chroma_client, "_mcp_blocks_embedded_write", return_value=False):
        assert chroma_client.preflight_embedded_write(str(tmp_path)) is None


def test_preflight_blocks_when_mcp_healthz(monkeypatch, tmp_path):
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT", "")
    with patch.object(chroma_client, "_mcp_blocks_embedded_write", return_value=True):
        with patch.object(chroma_client, "_active_watch_processes_for_db", return_value=[]):
            err = chroma_client.preflight_embedded_write(str(tmp_path))
    assert err is not None
    assert "MCP HTTP" in err
    assert "SIGSEGV" in err


def test_writer_client_raises_before_persistent_open(monkeypatch, tmp_path):
    db = str(tmp_path / "db")
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT", "")
    with patch.object(chroma_client, "preflight_embedded_write", return_value="blocked"):
        with pytest.raises(RuntimeError, match="blocked"):
            with chroma_client.writer_client(db):
                pass


def test_preflight_skipped_when_env_set(monkeypatch, tmp_path):
    monkeypatch.delenv("LLMLIBRARIAN_CHROMA_HOST", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT", "1")
    with patch.object(chroma_client, "_mcp_blocks_embedded_write", return_value=False):
        assert chroma_client.preflight_embedded_write(str(tmp_path)) is None
