from types import SimpleNamespace
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

    rc = pal.ensure_self_silo()
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
    rc = pal.ensure_self_silo()
    assert rc == 0
    assert calls == []


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

    rc = pal.ensure_self_silo()
    assert rc == 0
    assert calls[0] == ["rm", "old-slug"]
    assert calls[1][0] == "add"


def test_capabilities_calls_ensure_self_once_and_no_llm(monkeypatch):
    calls = {"ensure": 0, "llmli": []}

    def _fake_ensure():
        calls["ensure"] += 1
        return 0

    def _fake_run_llmli(args):
        calls["llmli"].append(args)
        return 0

    monkeypatch.setattr("pal.ensure_self_silo", _fake_ensure)
    monkeypatch.setattr("pal._run_llmli", _fake_run_llmli)
    rc = pal.cmd_capabilities(SimpleNamespace())
    assert rc == 0
    assert calls["ensure"] == 1
    assert calls["llmli"] == [["capabilities"]]
