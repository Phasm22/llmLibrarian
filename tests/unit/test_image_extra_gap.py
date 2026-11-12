"""Missing image extra: warn and degrade only when a configured silo holds images.

Covers the dependency-drift failure: a plain `uv sync` drops open_clip/torchvision,
image files then park silently. The startup check (MCP and watcher) should say so
once, and /healthz should carry `image_backend_required`.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import image_embeddings
import mcp_server
import pal
from mcp_server import HealthState


MISSING = {"status": "missing", "reason": "ModuleNotFoundError: open_clip", "checked_at": 0.0}
OK = {"status": "ok", "reason": None, "checked_at": 0.0}


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(mcp_server, "_IMAGE_EXTRA_STATE", {"required": False, "warned": False})
    monkeypatch.setattr(image_embeddings, "_IMAGE_BACKEND_CHECK", None)


# ---------------------------------------------------------------------------
# folder_has_image_files
# ---------------------------------------------------------------------------


def test_folder_with_an_image_is_detected(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "photo.HEIC").write_bytes(b"x")
    assert image_embeddings.folder_has_image_files(tmp_path) is True


def test_text_only_folder_is_not_an_image_folder(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    (tmp_path / "b.pdf").write_bytes(b"%PDF")
    assert image_embeddings.folder_has_image_files(tmp_path) is False


def test_hidden_directories_are_skipped(tmp_path):
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "thumb.png").write_bytes(b"x")
    assert image_embeddings.folder_has_image_files(tmp_path) is False


def test_missing_folder_is_not_an_image_folder(tmp_path):
    assert image_embeddings.folder_has_image_files(tmp_path / "nope") is False


def test_scan_is_capped(tmp_path):
    for i in range(3):
        (tmp_path / f"t{i}.txt").write_text("x")
    (tmp_path / "zzz.png").write_bytes(b"x")
    assert image_embeddings.folder_has_image_files(tmp_path, max_entries=2) is False
    assert image_embeddings.folder_has_image_files(tmp_path, max_entries=50) is True


# ---------------------------------------------------------------------------
# MCP startup / timer check
# ---------------------------------------------------------------------------


@pytest.fixture
def health(monkeypatch):
    buf = io.StringIO()
    state = HealthState(enabled=True, out=buf)
    monkeypatch.setattr(mcp_server, "_HEALTH", state)
    return buf


def _events(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def _silo_with_images(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "p.jpg").write_bytes(b"x")
    monkeypatch.setattr(mcp_server, "_configured_silo_paths", lambda: [str(tmp_path)])


def test_missing_extra_with_image_silo_warns_once_and_degrades(monkeypatch, tmp_path, caplog, health):
    monkeypatch.setattr(image_embeddings, "image_backend_status", lambda **_: MISSING)
    _silo_with_images(monkeypatch, tmp_path)

    with caplog.at_level("WARNING"):
        mcp_server._refresh_image_backend_health()
        mcp_server._refresh_image_backend_health()  # the 5-minute re-check

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "uv sync --extra image" in warnings[0].getMessage()
    assert mcp_server._IMAGE_EXTRA_STATE["required"] is True
    statuses = [e["status"] for e in _events(health)]
    assert statuses == ["degraded"]


def test_missing_extra_without_image_silo_is_silent(monkeypatch, tmp_path, caplog, health):
    (tmp_path / "notes.txt").write_text("text only")
    monkeypatch.setattr(image_embeddings, "image_backend_status", lambda **_: MISSING)
    monkeypatch.setattr(mcp_server, "_configured_silo_paths", lambda: [str(tmp_path)])

    with caplog.at_level("WARNING"):
        mcp_server._refresh_image_backend_health()

    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert mcp_server._IMAGE_EXTRA_STATE["required"] is False
    assert _events(health) == []  # never degraded


def test_restored_extra_recovers_a_degraded_server(monkeypatch, tmp_path, health):
    _silo_with_images(monkeypatch, tmp_path)
    monkeypatch.setattr(image_embeddings, "image_backend_status", lambda **_: MISSING)
    mcp_server._refresh_image_backend_health()
    monkeypatch.setattr(image_embeddings, "image_backend_status", lambda **_: OK)
    mcp_server._refresh_image_backend_health()
    assert [e["status"] for e in _events(health)] == ["degraded", "recovered"]
    assert mcp_server._IMAGE_EXTRA_STATE["required"] is False


def test_healthz_reports_required_flag(monkeypatch, tmp_path):
    import asyncio

    from starlette.requests import Request

    monkeypatch.setattr(image_embeddings, "image_backend_status", lambda **_: MISSING)
    monkeypatch.setattr(mcp_server, "_IMAGE_EXTRA_STATE", {"required": True, "warned": True})
    request = Request({"type": "http", "method": "GET", "path": "/healthz", "headers": [],
                       "client": ("127.0.0.1", 1)})
    body = json.loads(asyncio.run(mcp_server.healthz(request)).body)
    assert body["image_backend"] == "missing"
    assert body["image_backend_required"] is True
    assert body["ok"] is True  # additive: liveness contract unchanged


# ---------------------------------------------------------------------------
# Watcher startup
# ---------------------------------------------------------------------------


class _DummyObserver:
    def schedule(self, *args, **kwargs):
        return None

    def start(self):
        return None

    def stop(self):
        return None

    def join(self):
        return None


def _watcher(monkeypatch, root: Path):
    monkeypatch.setattr(pal, "Observer", _DummyObserver)
    monkeypatch.setattr(pal, "PAL_HOME", root / ".pal")
    watcher = pal.SiloWatcher(root, db_path=str(root / "db"), interval=10, debounce=1)
    return watcher


def test_watcher_warns_at_startup_when_its_folder_has_images(monkeypatch, tmp_path, capsys):
    root = tmp_path / "photos"
    root.mkdir()
    (root / "a.png").write_bytes(b"x")
    monkeypatch.setattr(image_embeddings, "image_backend_status", lambda **_: MISSING)
    watcher = _watcher(monkeypatch, root)

    watcher._warn_if_image_extra_missing()

    err = capsys.readouterr().err
    assert err.count("WARNING") == 1
    assert "uv sync --extra image" in err


def test_watcher_is_silent_for_a_text_folder_or_a_present_extra(monkeypatch, tmp_path, capsys):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("x")
    monkeypatch.setattr(image_embeddings, "image_backend_status", lambda **_: MISSING)
    _watcher(monkeypatch, root)._warn_if_image_extra_missing()
    assert capsys.readouterr().err == ""

    (root / "b.png").write_bytes(b"x")
    monkeypatch.setattr(image_embeddings, "image_backend_status", lambda **_: OK)
    _watcher(monkeypatch, root)._warn_if_image_extra_missing()
    assert capsys.readouterr().err == ""
