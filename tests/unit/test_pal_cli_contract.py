from typer.testing import CliRunner

import pal


runner = CliRunner()


def test_pal_help_shows_expected_commands():
    res = runner.invoke(pal.app, ["--help"])
    assert res.exit_code == 0
    for cmd in ("pull", "ask", "ls", "inspect", "remove", "sync", "silos", "tool"):
        assert cmd in res.stdout


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
    assert "--prompt" in res.stdout
    assert "--clear-prompt" in res.stdout


def test_ask_help_mentions_catalog_flags():
    res = runner.invoke(pal.app, ["ask", "--help"])
    assert res.exit_code == 0
    assert "--explain" in res.stdout
    assert "--force" in res.stdout


def test_ask_passes_explain_and_force(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_REQUIRE_SELF_SILO", "0")
    calls = []
    monkeypatch.setattr("pal._run_llmli", lambda args: calls.append(list(args)) or 0)
    res = runner.invoke(
        pal.app,
        ["ask", "--in", "stuff", "--quiet", "--explain", "--force", "what", "files", "are", "from", "2022"],
    )
    assert res.exit_code == 0
    assert calls
    assert "--explain" in calls[-1]
    assert "--force" in calls[-1]
