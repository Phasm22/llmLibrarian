"""Wiring test for `pal find` — should be a thin pass-through to llmli."""
from typer.testing import CliRunner

import pal


runner = CliRunner()


def test_pal_find_passthrough_minimal(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr("pal._run_llmli", lambda args: calls.append(list(args)) or 0)
    res = runner.invoke(pal.app, ["find"])
    assert res.exit_code == 0
    assert calls == [["find"]]


def test_pal_find_passes_all_flags(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr("pal._run_llmli", lambda args: calls.append(list(args)) or 0)
    res = runner.invoke(
        pal.app,
        [
            "find",
            "--in", "journal",
            "--in", "notes",
            "--name", "*.md",
            "--date", "2026-05-04:2026-05-06",
            "--field", "name_date",
            "--with-chunks",
            "--limit", "10",
            "--json",
        ],
    )
    assert res.exit_code == 0
    assert calls == [[
        "find",
        "--in", "journal",
        "--in", "notes",
        "--name", "*.md",
        "--date", "2026-05-04:2026-05-06",
        "--field", "name_date",
        "--with-chunks",
        "--limit", "10",
        "--json",
    ]]


def test_pal_find_default_field_omitted(monkeypatch):
    """`--field either` is the default; pal should not forward it."""
    calls: list[list[str]] = []
    monkeypatch.setattr("pal._run_llmli", lambda args: calls.append(list(args)) or 0)
    res = runner.invoke(pal.app, ["find", "--field", "either"])
    assert res.exit_code == 0
    assert calls == [["find"]]


def test_pal_find_default_limit_omitted(monkeypatch):
    """`--limit 50` is the default; pal should not forward it."""
    calls: list[list[str]] = []
    monkeypatch.setattr("pal._run_llmli", lambda args: calls.append(list(args)) or 0)
    res = runner.invoke(pal.app, ["find", "--limit", "50"])
    assert res.exit_code == 0
    assert calls == [["find"]]
