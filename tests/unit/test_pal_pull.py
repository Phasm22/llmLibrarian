import json
import os
from pathlib import Path
from types import SimpleNamespace

import pal


def _mock_subprocess(monkeypatch, files_indexed_by_path=None, failures_by_path=None, seen_cmds=None):
    files_indexed_by_path = files_indexed_by_path or {}
    failures_by_path = failures_by_path or set()
    seen_cmds = seen_cmds if seen_cmds is not None else []

    def fake_run(cmd, env=None, capture_output=False, text=False):
        path = cmd[-1]
        seen_cmds.append(cmd)
        payload = {"files_indexed": files_indexed_by_path.get(path, 0)}
        with open(env["LLMLIBRARIAN_STATUS_FILE"], "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return SimpleNamespace(returncode=(1 if path in failures_by_path else 0), stdout="", stderr="")

    monkeypatch.setattr("pal.subprocess.run", fake_run)
    return seen_cmds


def test_pull_all_errors_when_no_sources(monkeypatch, capsys):
    monkeypatch.setattr("pal._read_registry", lambda: {"sources": []})
    rc = pal.pull_all_sources()
    assert rc == 1
    assert "No registered folders" in capsys.readouterr().err


def test_pull_all_reports_all_up_to_date(monkeypatch, capsys):
    monkeypatch.setattr(
        "pal._read_registry",
        lambda: {"sources": [{"name": "Stuff", "path": "/tmp/stuff"}, {"name": "Tax", "path": "/tmp/tax"}]},
    )
    _mock_subprocess(monkeypatch)
    rc = pal.pull_all_sources()
    out = capsys.readouterr().out
    assert rc == 0
    assert "All silos up to date." in out


def test_pull_all_reports_updated_silos(monkeypatch, capsys):
    monkeypatch.setattr("pal._read_registry", lambda: {"sources": [{"name": "Stuff", "path": "/tmp/stuff"}]})
    _mock_subprocess(monkeypatch, files_indexed_by_path={"/tmp/stuff": 4})
    rc = pal.pull_all_sources()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Updated: Stuff (4 files)" in out


def test_pull_all_reports_failures_and_nonzero_exit(monkeypatch, capsys):
    monkeypatch.setattr("pal._read_registry", lambda: {"sources": [{"name": "Stuff", "path": "/tmp/stuff"}]})
    _mock_subprocess(monkeypatch, failures_by_path={"/tmp/stuff"})
    rc = pal.pull_all_sources()
    captured = capsys.readouterr()
    assert rc == 1
    assert "Failed: Stuff" in captured.err


def test_pull_all_non_tty_emits_progress_lines(monkeypatch, capsys):
    monkeypatch.setattr("pal._read_registry", lambda: {"sources": [{"name": "Stuff", "path": "/tmp/stuff"}]})
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    _mock_subprocess(monkeypatch, files_indexed_by_path={"/tmp/stuff": 0})
    pal.pull_all_sources()
    out = capsys.readouterr().out
    assert "1/1" in out


def test_pull_all_passes_full_and_follow_flags(monkeypatch):
    monkeypatch.setattr("pal._read_registry", lambda: {"sources": [{"name": "Stuff", "path": "/tmp/stuff"}]})
    seen_cmds = _mock_subprocess(monkeypatch, files_indexed_by_path={"/tmp/stuff": 1}, seen_cmds=[])
    pal.pull_all_sources(full=True, allow_cloud=True, follow_symlinks=True)
    assert seen_cmds
    cmd = seen_cmds[0]
    assert "add" in cmd
    assert "--full" in cmd
    assert "--allow-cloud" in cmd
    assert "--follow-symlinks" in cmd


def test_pull_command_requires_path_for_watch(capsys):
    from typer.testing import CliRunner
    runner = CliRunner()
    res = runner.invoke(pal.app, ["pull", "--watch"])
    assert res.exit_code == 2


def test_pull_command_requires_path_for_prompt():
    from typer.testing import CliRunner

    runner = CliRunner()
    res = runner.invoke(pal.app, ["pull", "--prompt", "Use this"])
    assert res.exit_code == 2
    assert "--prompt/--clear-prompt requires PATH" in (res.stderr or res.stdout)


def test_pull_command_rejects_prompt_and_clear_together():
    from typer.testing import CliRunner

    runner = CliRunner()
    res = runner.invoke(pal.app, ["pull", "/tmp/folder", "--prompt", "A", "--clear-prompt"])
    assert res.exit_code == 2
    assert "Use either --prompt or --clear-prompt" in (res.stderr or res.stdout)


def test_pull_command_rejects_blank_prompt():
    from typer.testing import CliRunner

    runner = CliRunner()
    res = runner.invoke(pal.app, ["pull", "/tmp/folder", "--prompt", "   "])
    assert res.exit_code == 2
    assert "Blank --prompt is not allowed" in (res.stderr or res.stdout)


def test_pull_watch_with_path_uses_path_watcher(monkeypatch):
    watched = {}
    monkeypatch.setattr("pal._pull_path_mode", lambda *a, **k: 0)
    monkeypatch.setattr(
        "pal._read_llmli_registry",
        lambda _db: {"folder-slug": {"path": str(Path('/tmp/folder').resolve())}},
    )

    class _DummyWatcher:
        def __init__(self, root, db_path, interval, debounce, silo_slug, allow_cloud=False, label="this folder", startup_message=None):
            watched["root"] = str(root)
            watched["interval"] = interval
            watched["debounce"] = debounce
            watched["silo_slug"] = silo_slug

    def _fake_run_watcher(_watcher, db_path, silo_slug):
        watched["db_path"] = db_path
        watched["run_silo_slug"] = silo_slug
        return 0

    monkeypatch.setattr("pal.SiloWatcher", _DummyWatcher)
    monkeypatch.setattr("pal._run_watcher", _fake_run_watcher)
    from typer.testing import CliRunner
    runner = CliRunner()
    res = runner.invoke(pal.app, ["pull", "/tmp/folder", "--watch", "--interval", "12", "--debounce", "2"])
    assert res.exit_code == 0
    assert watched["silo_slug"] == "folder-slug"
    assert watched["run_silo_slug"] == "folder-slug"
    assert watched["interval"] == 12
    assert watched["debounce"] == 2
    assert watched["root"] == str(Path("/tmp/folder").resolve())


def test_pull_with_path_calls_path_mode(monkeypatch):
    called = {}

    def _fake_pull_path(path_input, allow_cloud=False, follow_symlinks=False, full=False, prompt=None, clear_prompt=False):
        called["path"] = str(path_input)
        called["allow_cloud"] = allow_cloud
        called["follow_symlinks"] = follow_symlinks
        called["full"] = full
        called["prompt"] = prompt
        called["clear_prompt"] = clear_prompt
        return 0

    monkeypatch.setattr("pal._pull_path_mode", _fake_pull_path)
    from typer.testing import CliRunner
    runner = CliRunner()
    res = runner.invoke(pal.app, ["pull", "/tmp/one", "--full", "--allow-cloud", "--follow-symlinks"])
    assert res.exit_code == 0
    assert called["path"] == "/tmp/one"
    assert called["allow_cloud"] is True
    assert called["follow_symlinks"] is True
    assert called["full"] is True
    assert called["prompt"] is None
    assert called["clear_prompt"] is False


def test_pull_status_mode_dispatches(monkeypatch):
    called = {}

    def _fake_status(path_input, json_output=False, prune_stale=False):
        called["path_input"] = path_input
        called["json_output"] = json_output
        called["prune_stale"] = prune_stale
        return 0

    monkeypatch.setattr("pal._pull_watch_status_mode", _fake_status)
    from typer.testing import CliRunner
    runner = CliRunner()
    res = runner.invoke(pal.app, ["pull", "/tmp/one", "--status", "--json", "--prune-stale"])
    assert res.exit_code == 0
    assert called == {"path_input": "/tmp/one", "json_output": True, "prune_stale": True}


def test_pull_stop_mode_dispatches(monkeypatch):
    called = {}

    def _fake_stop(target, json_output=False, timeout=3.0):
        called["target"] = target
        called["json_output"] = json_output
        called["timeout"] = timeout
        return 0

    monkeypatch.setattr("pal._pull_watch_stop_mode", _fake_stop)
    from typer.testing import CliRunner
    runner = CliRunner()
    res = runner.invoke(pal.app, ["pull", "--stop", "demo", "--json"])
    assert res.exit_code == 0
    assert called["target"] == "demo"
    assert called["json_output"] is True


def test_pull_with_path_passes_prompt_option(monkeypatch):
    called = {}

    def _fake_pull_path(path_input, allow_cloud=False, follow_symlinks=False, full=False, prompt=None, clear_prompt=False):
        called["path"] = str(path_input)
        called["prompt"] = prompt
        called["clear_prompt"] = clear_prompt
        return 0

    monkeypatch.setattr("pal._pull_path_mode", _fake_pull_path)
    from typer.testing import CliRunner

    runner = CliRunner()
    res = runner.invoke(pal.app, ["pull", "/tmp/one", "--prompt", "Use this prompt"])
    assert res.exit_code == 0
    assert called["path"] == "/tmp/one"
    assert called["prompt"] == "Use this prompt"
    assert called["clear_prompt"] is False


def test_pull_with_path_passes_clear_prompt_option(monkeypatch):
    called = {}

    def _fake_pull_path(path_input, allow_cloud=False, follow_symlinks=False, full=False, prompt=None, clear_prompt=False):
        called["path"] = str(path_input)
        called["prompt"] = prompt
        called["clear_prompt"] = clear_prompt
        return 0

    monkeypatch.setattr("pal._pull_path_mode", _fake_pull_path)
    from typer.testing import CliRunner

    runner = CliRunner()
    res = runner.invoke(pal.app, ["pull", "/tmp/one", "--clear-prompt"])
    assert res.exit_code == 0
    assert called["path"] == "/tmp/one"
    assert called["prompt"] is None
    assert called["clear_prompt"] is True


def test_pull_path_mode_sets_prompt_override_when_requested(monkeypatch):
    monkeypatch.setattr("pal.Path.is_dir", lambda _self: True)
    monkeypatch.setattr("pal._run_llmli", lambda _args: 0)
    monkeypatch.setattr("pal._record_source_path", lambda _path: None)
    calls = {}

    def _fake_set(path, prompt, clear_prompt=False):
        calls["args"] = (str(path), prompt, clear_prompt)
        return True

    monkeypatch.setattr("pal._set_silo_prompt_for_path", _fake_set)

    rc = pal._pull_path_mode("/tmp/one", prompt="Prompt")
    assert rc == 0
    assert calls["args"][1] == "Prompt"
    assert calls["args"][2] is False


def test_pull_path_mode_errors_when_prompt_override_target_missing(monkeypatch):
    monkeypatch.setattr("pal.Path.is_dir", lambda _self: True)
    monkeypatch.setattr("pal._run_llmli", lambda _args: 0)
    monkeypatch.setattr("pal._record_source_path", lambda _path: None)
    monkeypatch.setattr("pal._set_silo_prompt_for_path", lambda *_a, **_k: False)

    rc = pal._pull_path_mode("/tmp/one", prompt="Prompt")
    assert rc == 1


def test_acquire_silo_pid_lock_blocks_live_pid(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("pal.WATCH_LOCKS_DIR", tmp_path / "watch_locks")
    db_path = tmp_path / "db"
    lock_path = pal._watch_lock_path(db_path, "demo")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 4242}), encoding="utf-8")
    monkeypatch.setattr("pal._pid_is_running", lambda pid: pid == 4242)

    acquired, err = pal._acquire_silo_pid_lock(db_path, "demo")
    assert acquired is None
    assert err is not None
    assert "pid 4242" in err


def test_acquire_silo_pid_lock_clears_stale(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("pal.WATCH_LOCKS_DIR", tmp_path / "watch_locks")
    db_path = tmp_path / "db"
    lock_path = pal._watch_lock_path(db_path, "demo")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 12345}), encoding="utf-8")
    monkeypatch.setattr("pal._pid_is_running", lambda _pid: False)

    acquired, err = pal._acquire_silo_pid_lock(db_path, "demo")
    assert err is None
    assert acquired == lock_path
    assert pal._read_watch_lock_pid(lock_path) == os.getpid()
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert payload["silo"] == "demo"
    assert payload["db_path"] == str(Path(db_path).resolve())
    assert "argv_hash" in payload
    assert "pal_version" in payload
    pal._release_silo_pid_lock(acquired)
    assert not lock_path.exists()


def test_run_watcher_returns_error_when_lock_held(monkeypatch, capsys):
    class _DummyWatcher:
        def __init__(self):
            self.ran = False
            self.stopped = False

        def run(self):
            self.ran = True

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(
        "pal._acquire_silo_pid_lock",
        lambda _db_path, _slug, root_path=None: (None, "Error: watcher already running for silo 'demo' (pid 999)."),
    )
    released = []
    monkeypatch.setattr("pal._release_silo_pid_lock", lambda lock_path: released.append(lock_path))
    watcher = _DummyWatcher()

    rc = pal._run_watcher(watcher, "/tmp/db", "demo")
    assert rc == 1
    assert watcher.ran is False
    assert released == []
    assert "watcher already running" in capsys.readouterr().err


def test_run_watcher_handles_sigterm_and_releases_lock(monkeypatch):
    class _DummyWatcher:
        def __init__(self):
            self.stop_calls = 0

        def run(self):
            handlers[pal.signal.SIGTERM](pal.signal.SIGTERM, None)

        def stop(self):
            self.stop_calls += 1

    watcher = _DummyWatcher()
    handlers = {}
    saved = {}

    def _fake_signal(sig, handler):
        previous = saved.get(sig, "prev")
        saved[sig] = handler
        handlers[sig] = handler
        return previous

    released = []
    monkeypatch.setattr("pal.signal.signal", _fake_signal)
    monkeypatch.setattr("pal._acquire_silo_pid_lock", lambda _db_path, _slug, root_path=None: (Path("/tmp/lock"), None))
    monkeypatch.setattr("pal._release_silo_pid_lock", lambda lock_path: released.append(lock_path))

    rc = pal._run_watcher(watcher, "/tmp/db", "demo")
    assert rc == 0
    assert watcher.stop_calls >= 1
    assert released == [Path("/tmp/lock")]
