import json
from types import SimpleNamespace

import pal


def _args(full: bool = False, allow_cloud: bool = False, follow_symlinks: bool = False):
    return SimpleNamespace(full=full, allow_cloud=allow_cloud, follow_symlinks=follow_symlinks)


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


def test_cmd_pull_errors_when_no_sources(monkeypatch, capsys):
    monkeypatch.setattr("pal._read_registry", lambda: {"sources": []})
    rc = pal.cmd_pull(_args())
    assert rc == 1
    assert "No registered silos" in capsys.readouterr().err


def test_cmd_pull_reports_all_up_to_date(monkeypatch, capsys):
    monkeypatch.setattr(
        "pal._read_registry",
        lambda: {"sources": [{"name": "Stuff", "path": "/tmp/stuff"}, {"name": "Tax", "path": "/tmp/tax"}]},
    )
    _mock_subprocess(monkeypatch)
    rc = pal.cmd_pull(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "All silos up to date." in out


def test_cmd_pull_reports_updated_silos(monkeypatch, capsys):
    monkeypatch.setattr("pal._read_registry", lambda: {"sources": [{"name": "Stuff", "path": "/tmp/stuff"}]})
    _mock_subprocess(monkeypatch, files_indexed_by_path={"/tmp/stuff": 4})
    rc = pal.cmd_pull(_args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "Updated: Stuff (4 files)" in out


def test_cmd_pull_reports_failures_and_nonzero_exit(monkeypatch, capsys):
    monkeypatch.setattr("pal._read_registry", lambda: {"sources": [{"name": "Stuff", "path": "/tmp/stuff"}]})
    _mock_subprocess(monkeypatch, failures_by_path={"/tmp/stuff"})
    rc = pal.cmd_pull(_args())
    captured = capsys.readouterr()
    assert rc == 1
    assert "Failed: Stuff" in captured.err


def test_cmd_pull_non_tty_emits_progress_lines(monkeypatch, capsys):
    monkeypatch.setattr("pal._read_registry", lambda: {"sources": [{"name": "Stuff", "path": "/tmp/stuff"}]})
    monkeypatch.setattr("sys.stderr.isatty", lambda: False)
    _mock_subprocess(monkeypatch, files_indexed_by_path={"/tmp/stuff": 0})
    pal.cmd_pull(_args())
    out = capsys.readouterr().out
    assert "1/1" in out


def test_cmd_pull_passes_full_and_follow_flags(monkeypatch):
    monkeypatch.setattr("pal._read_registry", lambda: {"sources": [{"name": "Stuff", "path": "/tmp/stuff"}]})
    seen_cmds = _mock_subprocess(monkeypatch, files_indexed_by_path={"/tmp/stuff": 1}, seen_cmds=[])
    pal.cmd_pull(_args(full=True, allow_cloud=True, follow_symlinks=True))
    assert seen_cmds
    cmd = seen_cmds[0]
    assert "add" in cmd
    assert "--full" in cmd
    assert "--allow-cloud" in cmd
    assert "--follow-symlinks" in cmd
