from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

import pal


runner = CliRunner()


def test_pal_help_lists_all_commands():
    res = runner.invoke(pal.app, ["--help"])
    assert res.exit_code == 0
    for cmd in ("pull", "ask", "ls", "inspect", "capabilities", "log", "remove", "sync", "diff", "status", "silos", "tool"):
        assert cmd in res.stdout


def test_pal_ls_inspect_log_remove_and_tool_delegate_to_llmli(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr("pal._run_llmli", lambda args: calls.append(list(args)) or 0)
    monkeypatch.setenv("LLMLIBRARIAN_REQUIRE_SELF_SILO", "0")

    assert runner.invoke(pal.app, ["ls"]).exit_code == 0
    assert runner.invoke(pal.app, ["inspect", "stuff", "--top", "7", "--filter", "pdf"]).exit_code == 0
    assert runner.invoke(pal.app, ["log"]).exit_code == 0
    assert runner.invoke(pal.app, ["remove", "Stuff"]).exit_code == 0
    assert runner.invoke(pal.app, ["tool", "llmli", "ask", "hello"]).exit_code == 0

    assert calls[0] == ["ls"]
    assert calls[1] == ["inspect", "stuff", "--top", "7", "--filter", "pdf"]
    assert calls[2] == ["log", "--last"]
    assert calls[3] == ["rm", "Stuff"]
    assert calls[4] == ["ask", "hello"]


def test_pal_sync_forces_self_index(monkeypatch):
    seen = {}

    def _fake_ensure(force=False, emit_warning=True):
        seen["force"] = force
        return 0

    monkeypatch.setattr("pal.ensure_self_silo", _fake_ensure)
    res = runner.invoke(pal.app, ["sync"])
    assert res.exit_code == 0
    assert seen["force"] is True


def test_pal_diff_command_runs_without_identifiable_error(monkeypatch, tmp_path: Path):
    root = tmp_path / "srcsilo"
    root.mkdir(parents=True, exist_ok=True)
    f = root / "a.py"
    f.write_text("print('ok')\n", encoding="utf-8")
    st = f.stat()

    monkeypatch.setattr("pal._ensure_src_on_path", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "state",
        SimpleNamespace(
            resolve_silo_to_slug=lambda _db, _silo: "demo",
            resolve_silo_prefix=lambda _db, _silo: None,
            list_silos=lambda _db: [{"slug": "demo", "path": str(root)}],
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "file_registry",
        SimpleNamespace(
            _read_file_manifest=lambda _db: {"silos": {"demo": {"files": {str(f.resolve()): {"mtime": st.st_mtime, "size": st.st_size}}}}},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "ingest",
        SimpleNamespace(
            _load_limits_config=lambda: (50_000_000, 8, 100_000_000, 500, 50_000_000),
            collect_files=lambda *_a, **_k: [(f.resolve(), "code")],
            ADD_DEFAULT_INCLUDE=[],
            ADD_DEFAULT_EXCLUDE=[],
        ),
    )

    res = runner.invoke(pal.app, ["diff", "demo"])
    assert res.exit_code == 0
    assert ("No changes." in res.stdout) or ("demo:" in res.stdout)


def test_pal_status_and_silos_command_run_without_identifiable_error(monkeypatch):
    monkeypatch.setattr("pal._ensure_src_on_path", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "silo_audit",
        SimpleNamespace(
            load_registry=lambda _db: [{"chunks_count": 5}, {"chunks_count": 7}],
            load_manifest=lambda _db: {"silos": {}},
            load_file_registry=lambda _db: {"files": {}},
            find_count_mismatches=lambda *_a, **_k: [],
            find_duplicate_hashes=lambda *_a, **_k: [],
            find_path_overlaps=lambda *_a, **_k: [],
            format_report=lambda *_a, **_k: "silo audit report",
        ),
    )

    res_status = runner.invoke(pal.app, ["status"])
    assert res_status.exit_code == 0
    assert "all current" in res_status.stdout

    res_silos = runner.invoke(pal.app, ["silos"])
    assert res_silos.exit_code == 0
    assert "silo audit report" in res_silos.stdout
