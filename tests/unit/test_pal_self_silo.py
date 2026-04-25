from pathlib import Path

import pal


def test_ensure_self_silo_adds_when_missing(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_REQUIRE_SELF_SILO", "1")
    monkeypatch.setattr("pal._get_git_root", lambda: Path("/tmp/repo"))
    monkeypatch.setattr("pal._is_dev_repo_at_root", lambda _root: True)
    monkeypatch.setattr("pal._read_llmli_registry", lambda _db: {})
    monkeypatch.setattr("pal._read_registry", lambda: {"bookmarks": []})

    add_calls: list[dict] = []

    def _fake_run_add(path, db_path=None, forced_silo_slug=None, display_name_override=None, allow_cloud=False, **_k):
        add_calls.append({"path": str(path), "forced_silo_slug": forced_silo_slug, "display_name_override": display_name_override})
        return (5, 0)

    saved = []

    def _fake_write_registry(reg):
        saved.append(reg)

    monkeypatch.setattr("orchestration.ingest.run_add", _fake_run_add)
    monkeypatch.setattr("pal._write_registry", _fake_write_registry)

    rc = pal.ensure_self_silo(force=True)
    assert rc == 0
    assert add_calls
    assert add_calls[0]["forced_silo_slug"] == "__self__"
    assert add_calls[0]["display_name_override"] == "self"
    assert add_calls[0]["path"] == str(Path("/tmp/repo").resolve())
    assert saved
    bookmarks = saved[0].get("bookmarks", [])
    assert any(s.get("silo") == "__self__" and s.get("name") == "self" for s in bookmarks)


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
    monkeypatch.setattr("pal._read_registry", lambda: {"bookmarks": []})
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
    monkeypatch.setattr("pal._read_registry", lambda: {"bookmarks": []})

    remove_calls: list[str] = []
    add_calls: list[dict] = []

    monkeypatch.setattr(
        "operations.op_remove_silo",
        lambda _db, slug: remove_calls.append(slug) or {"removed_slug": slug, "cleaned_slug": slug, "not_found": False},
    )
    monkeypatch.setattr("orchestration.ingest.run_add", lambda *_a, **_k: (5, 0))
    monkeypatch.setattr("pal._write_registry", lambda _reg: None)

    rc = pal.ensure_self_silo(force=True)
    assert rc == 0
    assert "old-slug" in remove_calls


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
    reg_content = {"bookmarks": [{"silo": "__self__", "path": repo_str}]}
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


def test_ask_explicit_self_scope_prints_stale_banner_before_answer(monkeypatch):
    from typer.testing import CliRunner

    monkeypatch.setattr("pal._should_require_self_silo", lambda: True)
    monkeypatch.setattr("pal._get_git_root", lambda: Path("/tmp/repo"))
    monkeypatch.setattr("pal._is_dev_repo_at_root", lambda _root: True)
    monkeypatch.setattr("pal._read_llmli_registry", lambda _db: {"__self__": {"path": str(Path('/tmp/repo').resolve())}})
    monkeypatch.setattr("pal._read_registry", lambda: {"bookmarks": [{"silo": "__self__", "path": str(Path('/tmp/repo').resolve())}]})
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
    monkeypatch.setattr("pal._read_registry", lambda: {"bookmarks": [{"silo": "__self__", "path": str(Path('/tmp/repo').resolve())}]})
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
    monkeypatch.setattr("pal._read_registry", lambda: {"bookmarks": [{"silo": "__self__", "path": str(Path('/tmp/repo').resolve())}]})
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
