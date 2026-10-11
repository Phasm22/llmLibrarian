"""Contract tests for the agent-facing llmli surface (JSON schema, safety, discoverability)."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import cli
from cli_agent import (
    EXIT_BUSY,
    EXIT_CONFIRM,
    SCHEMA_VERSION,
    busy_payload,
    command_schema,
    require_yes,
    suggest_close_matches,
    version_info,
)


def _run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["llmli"] + argv)
    return cli.main()


def test_schema_version_and_required_commands():
    schema = command_schema()
    assert schema["ok"] is True
    assert schema["schema_version"] == SCHEMA_VERSION
    names = {c["name"] for c in schema["commands"]}
    for required in (
        "schema",
        "version",
        "ls",
        "status",
        "query",
        "multi-query",
        "read",
        "find",
        "inspect",
        "verify",
        "add",
        "rm",
        "repair",
        "private",
    ):
        assert required in names
    assert "global_flags" in schema
    assert any(f.get("name") == "--json" for f in schema["global_flags"])
    query = next(c for c in schema["commands"] if c["name"] == "query")
    arg_names = {a["name"] for a in query["args"]}
    assert "--main-protein" in arg_names
    assert "--max-total-time" in arg_names
    assert "--tags" in arg_names


def test_llmli_schema_command_emits_json(monkeypatch, capsys):
    assert _run_cli(monkeypatch, ["schema"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["program"] == "llmli"


def test_llmli_version_flag(monkeypatch, capsys):
    with pytest.raises(SystemExit) as ei:
        _run_cli(monkeypatch, ["--version"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "llmli" in out
    assert "cli:" in out


def test_llmli_version_json(monkeypatch, capsys):
    with pytest.raises(SystemExit) as ei:
        _run_cli(monkeypatch, ["--json", "--version"])
    assert ei.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert "cli_path" in payload
    assert "version" in payload


def test_version_info_shape():
    info = version_info()
    assert info["ok"] is True
    assert info["name"] == "llmli"
    assert info["cli_path"]
    assert info["python"]


def test_did_you_mean_on_typo(monkeypatch, capsys):
    with pytest.raises(SystemExit) as ei:
        _run_cli(monkeypatch, ["verfy"])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "Did you mean" in err
    assert "verify" in err


def test_suggest_close_matches():
    assert "verify" in suggest_close_matches("verfy", ["verify", "version", "query"])


def test_rm_requires_yes(monkeypatch, capsys):
    rc = _run_cli(monkeypatch, ["rm", "Stuff"])
    assert rc == EXIT_CONFIRM
    err = capsys.readouterr().err
    assert "--yes" in err


def test_rm_dry_run_json(monkeypatch, capsys):
    monkeypatch.setattr(
        "state.list_silos",
        lambda _db: [{"slug": "stuff-aaaa", "display_name": "Stuff", "path": "/tmp/stuff", "chunks_count": 3}],
    )
    monkeypatch.setattr("state.resolve_silo_to_slug", lambda _db, _n: "stuff-aaaa")
    monkeypatch.setattr("state.resolve_silo_prefix", lambda _db, _n: None)
    monkeypatch.setattr("state.resolve_silo_by_path", lambda _db, _n: None)
    rc = _run_cli(monkeypatch, ["--json", "rm", "Stuff", "--dry-run"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["resolved_slug"] == "stuff-aaaa"


def test_rm_yes_dispatches(monkeypatch, capsys):
    seen = {}

    def _fake_rm(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli, "cmd_rm", _fake_rm)
    assert _run_cli(monkeypatch, ["rm", "Stuff", "--yes"]) == 0
    assert seen["args"].yes is True


def test_private_requires_yes(monkeypatch, capsys):
    monkeypatch.setattr("state.resolve_silo_to_slug", lambda _db, _n: "tax-aaaa")
    monkeypatch.setattr("state.resolve_silo_prefix", lambda _db, _n: None)
    monkeypatch.setattr("state.is_silo_private", lambda _db, _s: False)
    rc = _run_cli(monkeypatch, ["--json", "private", "tax-aaaa"])
    assert rc == EXIT_CONFIRM
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "confirmation_required"


def test_require_yes_helper():
    assert require_yes(SimpleNamespace(yes=False, dry_run=False), action="rm", detail="x")["exit_code"] == EXIT_CONFIRM
    assert require_yes(SimpleNamespace(yes=True, dry_run=False), action="rm", detail="x") is None
    assert require_yes(SimpleNamespace(yes=False, dry_run=True), action="rm", detail="x") is None


def test_busy_payload_exit_code():
    payload = busy_payload(TimeoutError("held"), "query")
    assert payload["ok"] is False
    assert payload["error"]["busy"] is True
    assert payload["error"]["retryable"] is True
    assert payload["exit_code"] == EXIT_BUSY


def test_ls_json(monkeypatch, capsys):
    monkeypatch.setattr(
        "state.list_silos",
        lambda _db: [
            {
                "slug": "recipes-aaaa",
                "display_name": "Recipes",
                "path": "/tmp/recipes",
                "files_indexed": 2,
                "chunks_count": 10,
                "updated": "2026-01-01T00:00:00",
                "private": False,
                "host": "test",
            }
        ],
    )
    assert _run_cli(monkeypatch, ["--json", "ls"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["silo_count"] == 1
    assert payload["silos"][0]["slug"] == "recipes-aaaa"


def test_query_parses_meta_filters(monkeypatch):
    seen = {}

    def _fake(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli, "cmd_query", _fake)
    rc = _run_cli(
        monkeypatch,
        [
            "query",
            "--in",
            "recipes-aaaa",
            "--main-protein",
            "chicken",
            "--max-total-time",
            "45",
            "--tags",
            "quick,weeknight",
            "--min-servings",
            "2",
            "dinner",
            "ideas",
        ],
    )
    assert rc == 0
    args = seen["args"]
    assert args.in_silo == "recipes-aaaa"
    assert args.main_protein == "chicken"
    assert args.max_total_time == 45
    assert args.tags == "quick,weeknight"
    assert args.min_servings == 2
    assert args.query == ["dinner", "ideas"]


def test_query_emits_json_chunks(monkeypatch, capsys):
    monkeypatch.setattr(
        "query.core.run_retrieve",
        lambda **kwargs: {
            "query": kwargs["query"],
            "chunks": [{"text": "hello", "source": "/tmp/a.md", "score": 0.9}],
            "intent": "LOOKUP",
        },
    )
    assert _run_cli(monkeypatch, ["query", "hello"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["chunks"][0]["text"] == "hello"


def test_status_json(monkeypatch, capsys):
    monkeypatch.setattr(
        "cli_agent.status_payload",
        lambda _db: {
            "ok": True,
            "db_path": str(_db),
            "write_in_progress": {},
            "lock": {"path": "/tmp/lock", "holder_pids": []},
            "busy": False,
        },
    )
    assert _run_cli(monkeypatch, ["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert "write_in_progress" in payload
    assert "lock" in payload


def test_help_lists_new_agent_commands(monkeypatch, capsys):
    with pytest.raises(SystemExit) as ei:
        _run_cli(monkeypatch, ["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("query", "multi-query", "read", "status", "schema", "version", "find", "private"):
        assert cmd in out
    assert "--json" in out
    assert "examples" in out.lower() or "llmli schema" in out
