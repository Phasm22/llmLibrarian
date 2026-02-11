from pal import _build_pull_env
import pal


def test_build_pull_env_filters_unrelated_vars(monkeypatch):
    monkeypatch.setenv("RANDOM_UNSAFE_VAR", "x")
    monkeypatch.setenv("LLMLIBRARIAN_DB", "/tmp/db")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = _build_pull_env("/tmp/status.json")

    assert env.get("LLMLIBRARIAN_DB") == "/tmp/db"
    assert env.get("PATH") == "/usr/bin"
    assert env.get("LLMLIBRARIAN_STATUS_FILE") == "/tmp/status.json"
    assert env.get("LLMLIBRARIAN_QUIET") == "1"
    assert "RANDOM_UNSAFE_VAR" not in env


def test_run_llmli_prefers_workspace_cli_and_sets_pythonpath(monkeypatch, tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "cli.py").write_text("print('x')", encoding="utf-8")
    monkeypatch.chdir(workspace)

    seen = {}

    def _fake_run(cmd, env=None, **_kwargs):
        seen["cmd"] = cmd
        seen["env"] = env
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr("pal.subprocess.run", _fake_run)
    rc = pal._run_llmli(["ls"])
    assert rc == 0
    assert seen["cmd"][1] == str((workspace / "cli.py").resolve())
    assert str((workspace / "src").resolve()) in (seen["env"].get("PYTHONPATH") or "")


def test_run_llmli_falls_back_to_module_cli_when_no_workspace_cli(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    seen = {}

    def _fake_run(cmd, env=None, **_kwargs):
        seen["cmd"] = cmd
        seen["env"] = env
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr("pal.subprocess.run", _fake_run)
    rc = pal._run_llmli(["ls"])
    assert rc == 0
    expected_cli = str((pal.Path(pal.__file__).resolve().parent / "cli.py"))
    assert seen["cmd"][1] == expected_cli
