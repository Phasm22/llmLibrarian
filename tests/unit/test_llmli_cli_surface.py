import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli


def _run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["llmli"] + argv)
    return cli.main()


def test_llmli_root_help_lists_all_subcommands(monkeypatch, capsys):
    with pytest.raises(SystemExit) as ei:
        _run_cli(monkeypatch, ["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    for cmd in ("add", "ask", "ls", "inspect", "index", "rm", "capabilities", "log", "eval-adversarial"):
        assert cmd in out


@pytest.mark.parametrize(
    ("subcmd", "tokens"),
    [
        ("add", ["--allow-cloud", "--follow-symlinks", "--full"]),
        ("ask", ["--in", "--unified", "--strict", "--quiet", "--explain", "--force", "--model", "--n-results"]),
        ("inspect", ["--top", "--filter"]),
        ("index", ["--archetype", "--mode", "--follow-symlinks"]),
        ("log", ["--last"]),
        ("eval-adversarial", ["--strict-mode", "--no-strict-mode", "--direct-decisive-mode", "--no-direct-decisive-mode"]),
    ],
)
def test_llmli_subcommand_help_includes_modifiers(monkeypatch, capsys, subcmd: str, tokens: list[str]):
    with pytest.raises(SystemExit) as ei:
        _run_cli(monkeypatch, [subcmd, "--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    for token in tokens:
        assert token in out


def test_llmli_add_parses_all_modifiers(monkeypatch):
    seen = {}

    def _fake_add(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli, "cmd_add", _fake_add)
    rc = _run_cli(
        monkeypatch,
        [
            "--db",
            "/tmp/db",
            "--config",
            "/tmp/archetypes.yaml",
            "--no-color",
            "add",
            "src",
            "--allow-cloud",
            "--follow-symlinks",
            "--full",
            "--silo",
            "src-silo",
            "--display-name",
            "Source Silo",
        ],
    )
    assert rc == 0
    args = seen["args"]
    # add command intentionally resets parser default db to None (uses constants/env fallback)
    assert args.db is None
    assert args.config == "/tmp/archetypes.yaml"
    assert args.no_color is True
    assert args.path == "src"
    assert args.allow_cloud is True
    assert args.follow_symlinks is True
    assert args.full is True
    assert args.silo == "src-silo"
    assert args.display_name == "Source Silo"


def test_llmli_ask_parses_all_modifiers(monkeypatch):
    seen = {}

    def _fake_ask(args):
        seen["args"] = args
        return 0

    monkeypatch.setattr(cli, "cmd_ask", _fake_ask)
    rc = _run_cli(
        monkeypatch,
        [
            "--db",
            "/tmp/db",
            "--config",
            "/tmp/archetypes.yaml",
            "--no-color",
            "ask",
            "--in",
            "stuff",
            "--strict",
            "--quiet",
            "--explain",
            "--force",
            "--model",
            "llama3.1:8b",
            "--n-results",
            "7",
            "what",
            "files",
            "are",
            "from",
            "2022",
        ],
    )
    assert rc == 0
    args = seen["args"]
    assert args.db == "/tmp/db"
    assert args.config == "/tmp/archetypes.yaml"
    assert args.no_color is True
    assert args.in_silo == "stuff"
    assert args.strict is True
    assert args.quiet is True
    assert args.explain is True
    assert args.force is True
    assert args.model == "llama3.1:8b"
    assert args.n_results == 7
    assert args.query == ["what", "files", "are", "from", "2022"]


def test_llmli_ask_parses_archetype_and_unified(monkeypatch):
    seen = {}

    def _fake_ask(args):
        seen.setdefault("calls", []).append(args)
        return 0

    monkeypatch.setattr(cli, "cmd_ask", _fake_ask)
    rc1 = _run_cli(monkeypatch, ["ask", "--archetype", "tax", "what", "changed"])
    rc2 = _run_cli(monkeypatch, ["ask", "--unified", "timeline"])
    assert rc1 == 0
    assert rc2 == 0
    assert seen["calls"][0].archetype == "tax"
    assert seen["calls"][0].unified is False
    assert seen["calls"][1].unified is True


def test_llmli_other_subcommands_parse_and_dispatch(monkeypatch):
    seen = {}

    def _capture(name: str):
        def _run(args):
            seen[name] = args
            return 0
        return _run

    monkeypatch.setattr(cli, "cmd_ls", _capture("ls"))
    monkeypatch.setattr(cli, "cmd_inspect", _capture("inspect"))
    monkeypatch.setattr(cli, "cmd_index", _capture("index"))
    monkeypatch.setattr(cli, "cmd_rm", _capture("rm"))
    monkeypatch.setattr(cli, "cmd_capabilities", _capture("capabilities"))
    monkeypatch.setattr(cli, "cmd_log", _capture("log"))
    monkeypatch.setattr(cli, "cmd_eval_adversarial", _capture("eval"))

    assert _run_cli(monkeypatch, ["ls"]) == 0
    assert _run_cli(monkeypatch, ["inspect", "stuff", "--top", "9", "--filter", "pdf"]) == 0
    assert _run_cli(monkeypatch, ["index", "--archetype", "tax", "--log", "/tmp/log.txt", "--mode", "deep", "--follow-symlinks"]) == 0
    assert _run_cli(monkeypatch, ["rm", "Stuff"]) == 0
    assert _run_cli(monkeypatch, ["capabilities"]) == 0
    assert _run_cli(monkeypatch, ["log", "--last"]) == 0
    assert _run_cli(
        monkeypatch,
        [
            "eval-adversarial",
            "--model",
            "llama3.1:8b",
            "--out",
            "/tmp/report.json",
            "--limit",
            "5",
            "--no-strict-mode",
            "--direct-decisive-mode",
        ],
    ) == 0

    assert seen["inspect"].silo == "stuff"
    assert seen["inspect"].top == 9
    assert seen["inspect"].filter == "pdf"
    assert seen["index"].archetype == "tax"
    assert seen["index"].mode == "deep"
    assert seen["index"].follow_symlinks is True
    assert seen["rm"].silo == ["Stuff"]
    assert seen["log"].last is True
    assert seen["eval"].strict_mode is False
    assert seen["eval"].direct_decisive_mode is True


def test_cmd_add_honors_env_db_path(monkeypatch, tmp_path: Path):
    target_dir = tmp_path / "src"
    target_dir.mkdir(parents=True, exist_ok=True)
    seen = {}

    def _fake_run_add(path, **kwargs):
        seen["path"] = path
        seen["kwargs"] = kwargs

    fake_ingest = SimpleNamespace(
        run_add=_fake_run_add,
        CloudSyncPathError=RuntimeError,
    )
    monkeypatch.setitem(sys.modules, "ingest", fake_ingest)
    env_db = tmp_path / "dbdir"
    monkeypatch.setenv("LLMLIBRARIAN_DB", str(env_db))
    args = argparse.Namespace(
        path=str(target_dir),
        db=None,
        no_color=True,
        allow_cloud=False,
        follow_symlinks=False,
        full=False,
        silo=None,
        display_name=None,
    )

    rc = cli.cmd_add(args)
    assert rc == 0
    assert str(seen["path"]) == str(target_dir.resolve())
    assert seen["kwargs"]["db_path"] == env_db
