from types import SimpleNamespace
from pathlib import Path

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

    calls = []
    watcher._update_single_file = lambda path, **_kwargs: (calls.append(path) or ("updated", path))

    now = pal.time.time()
    watcher.enqueue_update(str(target))
    watcher._drain_due(now=now + 0.5)
    assert calls == []
    watcher._drain_due(now=now + 2.0)
    assert calls == [str(target.resolve())]
    assert logged == ["this folder: +1 updated, -0 removed, 0 skipped"]


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
    watcher._update_single_file = lambda *_args, **_kwargs: ("error", str(target.resolve()))

    now = pal.time.time()
    watcher.enqueue_update(str(target))
    watcher._drain_due(now=now + 2.0)

    queued = watcher._queue[str(target.resolve())]
    assert queued["action"] == "update"
    assert int(queued["attempts"]) == 1
    assert any("retrying file.py in 30s" in line for line in logged)
