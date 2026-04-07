"""Contract tests: ingest entrypoints share orchestration behavior."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_llmli_add_argv_maps_flags():
    from orchestration.ingest import IngestRequest, llmli_add_argv

    argv = llmli_add_argv(
        IngestRequest(
            path="/tmp/x",
            incremental=False,
            allow_cloud=True,
            follow_symlinks=True,
            image_vision_enabled=True,
            workers=3,
            embedding_workers=2,
            forced_silo_slug="my-silo",
            display_name="My Silo",
        )
    )
    assert argv[0] == "add"
    assert "--full" in argv
    assert "--allow-cloud" in argv
    assert "--follow-symlinks" in argv
    assert "--image-vision" in argv
    assert "--workers" in argv and "3" in argv
    assert "--embedding-workers" in argv and "2" in argv
    assert "--silo" in argv and "my-silo" in argv
    assert "--display-name" in argv and "My Silo" in argv
    assert argv[-1] == str(Path("/tmp/x").resolve())


def test_run_ingest_delegates_to_run_add(monkeypatch, tmp_path):
    from orchestration.ingest import IngestRequest, run_ingest

    captured: dict = {}

    def fake_run_add(path, db_path=None, **kwargs):
        captured["path"] = path
        captured["db_path"] = db_path
        captured["kwargs"] = kwargs
        return (2, 0)

    monkeypatch.setattr("orchestration.ingest.run_add", fake_run_add)
    db = tmp_path / "db"
    db.mkdir()
    f = tmp_path / "note.txt"
    f.write_text("hi", encoding="utf-8")
    r = run_ingest(IngestRequest(path=f, db_path=db, display_name="D", forced_silo_slug="s"))
    assert r.files_indexed == 2
    assert r.failures == 0
    assert captured["kwargs"].get("display_name_override") == "D"
    assert captured["kwargs"].get("forced_silo_slug") == "s"


def test_pull_all_subprocess_does_not_capture_output(monkeypatch):
    import pal

    monkeypatch.setattr(
        "pal._read_registry",
        lambda: {"bookmarks": [{"name": "A", "path": "/tmp/a"}]},
    )
    seen: dict = {}

    def fake_run(cmd, env=None, **kwargs):
        seen["kwargs"] = kwargs
        seen["cmd"] = cmd
        sf = (env or {}).get("LLMLIBRARIAN_STATUS_FILE")
        if sf:
            Path(sf).write_text(json.dumps({"files_indexed": 1}), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("pal.subprocess.run", fake_run)
    pal.pull_all_sources()
    assert not seen["kwargs"].get("capture_output")
    assert seen["cmd"][1].endswith("cli.py") or "cli" in seen["cmd"][1]
    assert "add" in seen["cmd"]


def test_mcp_add_silo_file_reaches_run_ingest(monkeypatch, tmp_path):
    """MCP add_silo allows a single file and delegates to run_ingest."""
    from orchestration.ingest import IngestResult

    import mcp_server
    import state

    called: dict = {}

    def fake_run_ingest(request):
        called["path"] = request.path
        return IngestResult(files_indexed=1, failures=0, silo_slug="solo-txt")

    monkeypatch.setattr("orchestration.ingest.run_ingest", fake_run_ingest)
    monkeypatch.setattr(state, "resolve_silo_by_path", lambda db, p: "solo-txt")
    monkeypatch.setattr(state, "resolve_silo_to_slug", lambda db, s: s or "solo-txt")
    db_dir = tmp_path / "db"
    db_dir.mkdir()
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db_dir))

    f = tmp_path / "solo.txt"
    f.write_text("ok", encoding="utf-8")

    out = mcp_server.add_silo(str(f))
    assert Path(called["path"]).resolve() == f.resolve()
    assert out.get("status") == "ok"
    assert out.get("files_indexed") == 1
