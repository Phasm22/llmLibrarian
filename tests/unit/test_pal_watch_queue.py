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
    watcher = pal.SiloWatcher(root, db_path=str(root / "db"), interval=10, debounce=1)
    return watcher


def test_watch_debounce_collapses_updates(monkeypatch, tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "file.py"
    target.write_text("x", encoding="utf-8")

    watcher = _make_watcher(monkeypatch, root)

    calls = []
    watcher._update_single_file = lambda path, **_kwargs: (calls.append(path) or ("updated", path))

    now = pal.time.time()
    watcher.enqueue_update(str(target))
    watcher._drain_due(now=now + 0.5)
    assert calls == []
    watcher._drain_due(now=now + 2.0)
    assert calls == [str(target.resolve())]


def test_reconcile_removes_missing(monkeypatch, tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()

    watcher = _make_watcher(monkeypatch, root)

    removed = []
    watcher._remove_single_file = lambda path, **_kwargs: (removed.append(path) or ("removed", path))
    watcher._update_single_file = lambda path, **_kwargs: ("updated", path)
    watcher._update_silo_counts = lambda *a, **k: None

    watcher._collect_files = lambda *a, **k: []
    watcher._read_manifest = lambda _db: {"silos": {"__self__": {"files": {str(root / "gone.py"): {"mtime": 1, "size": 1}}}}}

    updated, removed_count, skipped = watcher._reconcile_once()
    assert updated == 0
    assert removed_count == 1
    assert skipped == 0
    assert removed == [str((root / "gone.py").resolve())]
