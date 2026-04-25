from __future__ import annotations

from typer.testing import CliRunner

import pal


runner = CliRunner()


def test_pal_help_lists_all_commands():
    res = runner.invoke(pal.app, ["--help"])
    assert res.exit_code == 0
    for cmd in (
        "install",
        "pull",
        "ask",
        "ls",
        "remove",
        "tool",
        "daemon",
        "extension",
    ):
        assert cmd in res.stdout


def test_pal_ls_remove_and_tool_delegate_to_llmli(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr("pal._run_llmli", lambda args: calls.append(list(args)) or 0)
    monkeypatch.setattr(
        "operations.op_remove_silo",
        lambda _db, name: {"removed_slug": name, "cleaned_slug": name, "not_found": False},
    )
    monkeypatch.setenv("LLMLIBRARIAN_REQUIRE_SELF_SILO", "0")

    assert runner.invoke(pal.app, ["ls"]).exit_code == 0
    assert runner.invoke(pal.app, ["remove", "Stuff"]).exit_code == 0
    assert runner.invoke(pal.app, ["tool", "llmli", "ask", "hello"]).exit_code == 0

    assert calls[0] == ["ls"]
    # remove uses op_remove_silo directly — no llmli subprocess needed
    assert calls[1] == ["ask", "hello"]


def test_pal_pull_forwards_exclude_patterns(monkeypatch):
    seen = {}

    def _fake_pull_path_mode(*args, **kwargs):
        seen["path"] = {"args": args, "kwargs": kwargs}
        return 0

    monkeypatch.setattr("pal._pull_path_mode", _fake_pull_path_mode)
    res = runner.invoke(
        pal.app,
        ["pull", "/tmp/src", "--exclude", "node_modules/", "--exclude", "*.tmp"],
    )
    assert res.exit_code == 0
    assert seen["path"]["kwargs"]["exclude_patterns"] == ["node_modules/", "*.tmp"]


