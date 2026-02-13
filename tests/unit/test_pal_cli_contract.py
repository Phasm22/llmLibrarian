import sys
from types import SimpleNamespace

from typer.testing import CliRunner

import pal


runner = CliRunner()


def test_pal_help_shows_expected_commands():
    res = runner.invoke(pal.app, ["--help"])
    assert res.exit_code == 0
    for cmd in ("pull", "ask", "ls", "inspect", "capabilities", "log", "remove", "sync", "diff", "status", "silos", "tool"):
        assert cmd in res.stdout


def test_pal_short_help_flag_supported():
    res = runner.invoke(pal.app, ["-h"])
    assert res.exit_code == 0
    assert "Index folders, ask questions, stay in sync." in res.stdout


def test_pal_no_argv_preprocessing_shortcuts(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_REQUIRE_SELF_SILO", "0")
    calls = []
    monkeypatch.setattr("pal._run_llmli", lambda args: calls.append(list(args)) or 0)
    monkeypatch.setitem(
        sys.modules,
        "state",
        SimpleNamespace(
            resolve_silo_to_slug=lambda _db, name: "tax" if name == "tax" else None,
            resolve_silo_prefix=lambda _db, _prefix: None,
        ),
    )

    bad = runner.invoke(pal.app, ["what", "is", "pal"])
    assert bad.exit_code != 0
    assert "No such command" in (bad.stderr or bad.stdout)

    ok = runner.invoke(pal.app, ["ask", "in", "tax", "what", "is", "pal"])
    assert ok.exit_code == 0
    assert calls[-1] == ["ask", "--in", "tax", "what", "is", "pal"]


def test_pal_ask_natural_in_malformed_scope_token_has_deterministic_hint(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_REQUIRE_SELF_SILO", "0")
    res = runner.invoke(pal.app, ["ask", "in", "marketman--quiet", "show", "structure", "snapshot"])
    assert res.exit_code == 2
    assert "Malformed scope token" in (res.stderr or res.stdout)


def test_pull_help_mentions_watch():
    res = runner.invoke(pal.app, ["pull", "--help"])
    assert res.exit_code == 0
    assert "--watch" in res.stdout
    assert "--status" in res.stdout
    assert "--stop" in res.stdout
    assert "--json" in res.stdout
    assert "--prune-stale" in res.stdout
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


def test_pull_rejects_conflicting_operation_modes():
    res = runner.invoke(pal.app, ["pull", "/tmp/folder", "--watch", "--status"])
    assert res.exit_code == 2
    assert "only one operation mode" in (res.stderr or res.stdout).lower()


def test_pull_status_rejects_indexing_flags():
    res = runner.invoke(pal.app, ["pull", "--status", "--full"])
    assert res.exit_code == 2
    assert "--status cannot be combined" in (res.stderr or res.stdout)
