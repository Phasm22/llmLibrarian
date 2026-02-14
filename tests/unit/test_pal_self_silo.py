from pathlib import Path

import pal


def test_ensure_self_silo_adds_when_missing(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_REQUIRE_SELF_SILO", "1")
    monkeypatch.setattr("pal._get_git_root", lambda: Path("/tmp/repo"))
    monkeypatch.setattr("pal._is_dev_repo_at_root", lambda _root: True)
    monkeypatch.setattr("pal._read_llmli_registry", lambda _db: {})
    monkeypatch.setattr("pal._read_registry", lambda: {"sources": []})

    calls = []

    def _fake_run_llmli(args):
        calls.append(args)
        return 0

    saved = []

    def _fake_write_registry(reg):
        saved.append(reg)

    monkeypatch.setattr("pal._run_llmli", _fake_run_llmli)
    monkeypatch.setattr("pal._write_registry", _fake_write_registry)

    rc = pal.ensure_self_silo(force=True)
    assert rc == 0
    assert calls
    assert calls[0][:4] == ["add", "--silo", "__self__", "--display-name"]
    assert calls[0][-1] == str(Path("/tmp/repo").resolve())
    assert saved
    sources = saved[0].get("sources", [])
    assert any(s.get("silo") == "__self__" and s.get("name") == "self" for s in sources)


def test_ensure_self_silo_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_REQUIRE_SELF_SILO", "0")
    calls = []
    monkeypatch.setattr("pal._run_llmli", lambda args: calls.append(args) or 0)
    rc = pal.ensure_self_silo(force=True)
    assert rc == 0
    assert calls == []


def test_ensure_self_silo_warns_when_missing_and_not_forced(monkeypatch, capsys):
    monkeypatch.setenv("LLMLIBRARIAN_REQUIRE_SELF_SILO", "1")
    monkeypatch.setattr("pal._get_git_root", lambda: Path("/tmp/repo"))
    monkeypatch.setattr("pal._is_dev_repo_at_root", lambda _root: True)
    monkeypatch.setattr("pal._read_llmli_registry", lambda _db: {})
    monkeypatch.setattr("pal._read_registry", lambda: {"sources": []})
    calls = []
    monkeypatch.setattr("pal._run_llmli", lambda args: calls.append(args) or 0)

    rc = pal.ensure_self_silo(force=False)
    assert rc == 0
    assert calls == []
    err = capsys.readouterr().err
    assert "Self-silo missing" in err


def test_ensure_self_silo_reindexes_when_slug_differs(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_REQUIRE_SELF_SILO", "1")
    monkeypatch.setattr("pal._get_git_root", lambda: Path("/tmp/repo"))
    monkeypatch.setattr("pal._is_dev_repo_at_root", lambda _root: True)
    monkeypatch.setattr(
        "pal._read_llmli_registry",
        lambda _db: {"old-slug": {"path": "/tmp/repo", "updated": "2024-01-01T00:00:00+00:00"}},
    )
    monkeypatch.setattr("pal._read_registry", lambda: {"sources": []})

    calls = []

    def _fake_run_llmli(args):
        calls.append(args)
        return 0

    monkeypatch.setattr("pal._run_llmli", _fake_run_llmli)
    monkeypatch.setattr("pal._write_registry", lambda _reg: None)

    rc = pal.ensure_self_silo(force=True)
    assert rc == 0
    assert calls[0] == ["rm", "old-slug"]
    assert calls[1][0] == "add"


def test_ensure_self_silo_warns_when_stale(monkeypatch, capsys):
    """When stale in auto mode, only warn; no reindex."""
    repo_root = Path("/tmp/repo").resolve()
    repo_str = str(repo_root)
    monkeypatch.setenv("LLMLIBRARIAN_REQUIRE_SELF_SILO", "1")
    monkeypatch.setattr("pal._get_git_root", lambda: repo_root)
    monkeypatch.setattr("pal._is_dev_repo_at_root", lambda _root: True)
    monkeypatch.setattr(
        "pal._read_llmli_registry",
        lambda _db: {"__self__": {"path": repo_str, "updated": "2024-01-01T00:00:00+00:00"}},
    )
    reg_content = {"sources": [{"silo": "__self__", "path": repo_str, "self_silo_last_index_mtime": 1}]}
    monkeypatch.setattr("pal._read_registry", lambda: reg_content)
    monkeypatch.setattr("pal._git_is_dirty", lambda _root: True)
    monkeypatch.setattr("pal._git_last_commit_ct", lambda _root: 99999)

    calls = []
    monkeypatch.setattr("pal._run_llmli", lambda args: calls.append(list(args)) or 0)
    monkeypatch.setattr("pal._write_registry", lambda _reg: None)

    rc = pal.ensure_self_silo(force=False)
    assert rc == 0
    assert calls == []
    err = capsys.readouterr().err
    assert "Self-silo stale" in err
    assert "pal sync" in err


def test_capabilities_calls_ensure_self_once_and_no_llm(monkeypatch):
    from typer.testing import CliRunner
    calls = {"ensure": 0, "llmli": []}

    def _fake_ensure(force=False):
        calls["ensure"] += 1
        return 0

    def _fake_run_llmli(args):
        calls["llmli"].append(args)
        return 0

    monkeypatch.setattr("pal.ensure_self_silo", _fake_ensure)
    monkeypatch.setattr("pal._run_llmli", _fake_run_llmli)
    runner = CliRunner()
    res = runner.invoke(pal.app, ["capabilities"])
    assert res.exit_code == 0
    assert calls["ensure"] == 1
    assert calls["llmli"] == [["capabilities"]]


def test_ask_explicit_self_scope_prints_stale_banner_before_answer(monkeypatch):
    from typer.testing import CliRunner

    monkeypatch.setattr("pal._should_require_self_silo", lambda: True)
    monkeypatch.setattr("pal._get_git_root", lambda: Path("/tmp/repo"))
    monkeypatch.setattr("pal._is_dev_repo_at_root", lambda _root: True)
    monkeypatch.setattr("pal._read_llmli_registry", lambda _db: {"__self__": {"path": str(Path('/tmp/repo').resolve())}})
    monkeypatch.setattr("pal._read_registry", lambda: {"sources": [{"silo": "__self__", "path": str(Path('/tmp/repo').resolve()), "self_silo_last_index_mtime": 1}]})
    monkeypatch.setattr("pal._git_is_dirty", lambda _root: True)
    monkeypatch.setattr("pal._git_last_commit_ct", lambda _root: 99999)
    monkeypatch.setattr("pal.ensure_self_silo", lambda force=False, emit_warning=True: 0)

    def _fake_run_llmli(_args):
        print("ANSWER_BODY")
        return 0

    monkeypatch.setattr("pal._run_llmli", _fake_run_llmli)
    runner = CliRunner()
    res = runner.invoke(pal.app, ["ask", "--in", "__self__", "hello"])
    assert res.exit_code == 0
    out = res.stdout
    assert "Index may be outdated" in out
    assert "ANSWER_BODY" in out
    assert out.find("Index may be outdated") < out.find("ANSWER_BODY")


def test_ask_quiet_does_not_print_stale_banner(monkeypatch):
    from typer.testing import CliRunner

    monkeypatch.setattr("pal._should_require_self_silo", lambda: True)
    monkeypatch.setattr("pal._get_git_root", lambda: Path("/tmp/repo"))
    monkeypatch.setattr("pal._is_dev_repo_at_root", lambda _root: True)
    monkeypatch.setattr("pal._read_llmli_registry", lambda _db: {"__self__": {"path": str(Path('/tmp/repo').resolve())}})
    monkeypatch.setattr("pal._read_registry", lambda: {"sources": [{"silo": "__self__", "path": str(Path('/tmp/repo').resolve()), "self_silo_last_index_mtime": 1}]})
    monkeypatch.setattr("pal._git_is_dirty", lambda _root: True)
    monkeypatch.setattr("pal._git_last_commit_ct", lambda _root: 99999)
    monkeypatch.setattr("pal.ensure_self_silo", lambda force=False, emit_warning=True: 0)

    def _fake_run_llmli(_args):
        print("ANSWER_BODY")
        return 0

    monkeypatch.setattr("pal._run_llmli", _fake_run_llmli)
    runner = CliRunner()
    res = runner.invoke(pal.app, ["ask", "--quiet", "hello"])
    assert res.exit_code == 0
    assert "Index may be outdated" not in res.stdout
    assert "ANSWER_BODY" in res.stdout


def test_ask_scoped_non_self_does_not_print_stale_banner(monkeypatch):
    from typer.testing import CliRunner

    monkeypatch.setattr("pal._should_require_self_silo", lambda: True)
    monkeypatch.setattr("pal._get_git_root", lambda: Path("/tmp/repo"))
    monkeypatch.setattr("pal._is_dev_repo_at_root", lambda _root: True)
    monkeypatch.setattr("pal._read_llmli_registry", lambda _db: {"__self__": {"path": str(Path('/tmp/repo').resolve())}})
    monkeypatch.setattr("pal._read_registry", lambda: {"sources": [{"silo": "__self__", "path": str(Path('/tmp/repo').resolve()), "self_silo_last_index_mtime": 1}]})
    monkeypatch.setattr("pal._git_is_dirty", lambda _root: True)
    monkeypatch.setattr("pal._git_last_commit_ct", lambda _root: 99999)
    monkeypatch.setattr("pal.ensure_self_silo", lambda force=False, emit_warning=True: 0)

    def _fake_run_llmli(_args):
        print("ANSWER_BODY")
        return 0

    monkeypatch.setattr("pal._run_llmli", _fake_run_llmli)
    runner = CliRunner()
    res = runner.invoke(pal.app, ["ask", "--in", "tax", "hello"])
    assert res.exit_code == 0
    assert "Index may be outdated" not in res.stdout
    assert "ANSWER_BODY" in res.stdout
