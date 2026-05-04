from pathlib import Path
from types import SimpleNamespace

import pal


class _DummyObserver:
    def schedule(self, *args, **kwargs):
        return None

    def start(self):
        return None

    def stop(self):
        return None

    def join(self):
        return None


def _make_watcher(monkeypatch, root: Path):
    monkeypatch.setattr(pal, "Observer", _DummyObserver)
    monkeypatch.setattr(pal, "PAL_HOME", root / ".pal")
    watcher = pal.SiloWatcher(root, db_path=str(root / "db"), interval=10, debounce=1)
    return watcher


def test_watch_debounce_collapses_updates(monkeypatch, tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "file.py"
    target.write_text("x", encoding="utf-8")

    watcher = _make_watcher(monkeypatch, root)
    logged = []
    watcher._log = lambda message: logged.append(message)

    calls: list[tuple] = []

    def _fake_mcp(tool, **kwargs):
        calls.append((tool, kwargs))
        return {"status": "updated", "path": kwargs.get("path"), "silo": kwargs.get("silo")}

    monkeypatch.setattr(pal, "_mcp_call_sync", _fake_mcp)

    now = pal.time.time()
    watcher.enqueue_update(str(target))
    watcher._drain_due(now=now + 0.5)
    assert calls == []
    watcher._drain_due(now=now + 2.0)
    assert len(calls) == 1
    assert calls[0][0] == "update_file"
    assert calls[0][1]["path"] == str(target.resolve())
    assert calls[0][1]["confirm"] is True
    assert logged == ["this folder: pull complete after +1 queued, -0 queued, 0 skipped"]


def test_reconcile_queues_missing_for_removal(monkeypatch, tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()

    watcher = _make_watcher(monkeypatch, root)

    watcher._collect_files = lambda *a, **k: []
    watcher._read_manifest = lambda _db: {"silos": {"__self__": {"files": {str(root / "gone.py"): {"mtime": 1, "size": 1}}}}}

    updated, removed_count, skipped = watcher._reconcile_once()
    assert updated == 0
    assert removed_count == 1
    assert skipped == 0
    queued = watcher._queue[str((root / "gone.py").resolve())]
    assert queued["action"] == "delete"
    assert int(queued["attempts"]) == 0


def test_watch_retries_error_with_backoff(monkeypatch, tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "file.py"
    target.write_text("x", encoding="utf-8")

    watcher = _make_watcher(monkeypatch, root)
    logged = []
    watcher._log = lambda message: logged.append(message)

    monkeypatch.setattr(
        pal,
        "_mcp_call_sync",
        lambda tool, **kwargs: {"status": "error", "error": "boom"},
    )

    now = pal.time.time()
    watcher.enqueue_update(str(target))
    watcher._drain_due(now=now + 2.0)

    queued = watcher._queue[str(target.resolve())]
    assert queued["action"] == "update"
    assert int(queued["attempts"]) == 1
    assert any("failed via MCP" in line and "boom" in line for line in logged)
