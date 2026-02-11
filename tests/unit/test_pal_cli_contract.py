from pathlib import Path

from typer.testing import CliRunner

import pal


runner = CliRunner()


def test_pal_watch_self_legacy_hidden_command(monkeypatch):
    res_help = runner.invoke(pal.app, ["--help"])
    assert res_help.exit_code == 0
    assert "watch-self" not in res_help.stdout
    assert " add " not in f" {res_help.stdout} "

    monkeypatch.setattr("pal.Observer", object())
    monkeypatch.setattr("pal.is_dev_repo", lambda: True)
    monkeypatch.setattr("pal.ensure_self_silo", lambda force=True: 0)
    monkeypatch.setattr("pal._get_git_root", lambda: Path("/tmp/repo"))

    class _DummyWatcher:
        def __init__(self, *args, **kwargs):
            self.startup_message = kwargs.get("startup_message")

        def run(self):
            print(self.startup_message)

    def _fake_run_watcher(watcher, _db_path, _slug):
        watcher.run()
        return 0

    monkeypatch.setattr("pal.SiloWatcher", _DummyWatcher)
    monkeypatch.setattr("pal._run_watcher", _fake_run_watcher)
    res = runner.invoke(pal.app, ["watch-self"])
    assert res.exit_code == 0
    assert "Watching self folder: . (Tip: pal pull <path> --watch watches a folder)" in res.stdout


def test_pal_no_argv_preprocessing_shortcuts(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_REQUIRE_SELF_SILO", "0")
    calls = []
    monkeypatch.setattr("pal._run_llmli", lambda args: calls.append(list(args)) or 0)

    bad = runner.invoke(pal.app, ["what", "is", "pal"])
    assert bad.exit_code != 0
    assert "No such command" in (bad.stderr or bad.stdout)

    ok = runner.invoke(pal.app, ["ask", "in", "tax", "what", "is", "pal"])
    assert ok.exit_code == 0
    assert calls[-1] == ["ask", "in", "tax", "what", "is", "pal"]


def test_pull_help_mentions_watch():
    res = runner.invoke(pal.app, ["pull", "--help"])
    assert res.exit_code == 0
    assert "--watch" in res.stdout
