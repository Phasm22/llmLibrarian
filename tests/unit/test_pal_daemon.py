from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import pal


runner = CliRunner()


def test_pull_path_mode_syncs_daemon_when_installed(monkeypatch):
    seen = {"sync": 0}
    monkeypatch.setattr("pal.Path.is_dir", lambda _self: True)
    monkeypatch.setattr("orchestration.ingest.run_add", lambda *_a, **_k: (5, 0))
    monkeypatch.setattr("pal._record_source_path", lambda _path: None)
    monkeypatch.setattr("pal._daemon_is_installed", lambda: True)
    monkeypatch.setattr("pal._sync_daemon_services", lambda emit_output=False: seen.__setitem__("sync", seen["sync"] + 1) or 0)

    rc = pal._pull_path_mode("/tmp/one")
    assert rc == 0
    assert seen["sync"] == 1


def test_remove_command_prunes_source_registry_and_syncs_daemon(monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setattr("operations.op_remove_silo", lambda _db, _name: {"removed_slug": "docs", "cleaned_slug": "docs", "not_found": False})
    monkeypatch.setattr("pal._resolve_registry_source_for_remove", lambda _name, _db: "/tmp/source")
    monkeypatch.setattr("pal._remove_source_path", lambda path: seen.setdefault("removed_path", path) or True)
    monkeypatch.setattr("pal._daemon_is_installed", lambda: True)
    monkeypatch.setattr("pal._sync_daemon_services", lambda emit_output=False: seen.setdefault("synced", emit_output) or 0)

    res = runner.invoke(pal.app, ["remove", "Docs"])
    assert res.exit_code == 0
    assert seen["removed_path"] == "/tmp/source"
    assert seen["synced"] is False


def test_daemon_install_writes_metadata_and_syncs(monkeypatch, tmp_path: Path):
    pal_home = tmp_path / ".pal"
    written = {}
    monkeypatch.setattr("pal.PAL_HOME", pal_home)
    monkeypatch.setattr("pal._daemon_runtime_metadata", lambda manager=None: {"manager": "launchd", "db_path": "/tmp/db"})
    monkeypatch.setattr("pal.jobsrt.write_daemon_metadata", lambda _home, payload: written.setdefault("payload", payload) or (_home / "daemon.json"))
    monkeypatch.setattr("pal._sync_daemon_services", lambda emit_output=True: 0)

    res = runner.invoke(pal.app, ["daemon", "install"])
    assert res.exit_code == 0
    assert written["payload"]["manager"] == "launchd"
    assert (pal_home / "logs").exists()


def test_jobs_ls_renders_derived_jobs(monkeypatch):
    job = pal.jobsrt.JobSpec(
        id="watch_silo:docs",
        kind="watch_silo",
        slug="docs-aaaa1111",
        source_path="/tmp/docs",
        service_name="io.llmlibrarian.watch.docs-aaaa1111",
        log_path="/tmp/watch.log",
        interval=60,
        debounce=30,
    )
    monkeypatch.setattr("pal._daemon_metadata", lambda: {"manager": "launchd", "db_path": "/tmp/db"})
    monkeypatch.setattr("pal._derive_watch_jobs_for_daemon", lambda _manager, db_path=None: ([job], []))
    monkeypatch.setattr("pal._status_records", lambda _db, _path=None: ([{"silo": "docs-aaaa1111", "state": "running"}], None))

    res = runner.invoke(pal.app, ["jobs", "ls"])
    assert res.exit_code == 0
    assert "watch_silo" in res.stdout
    assert "docs-aaaa1111" in res.stdout
    assert "running" in res.stdout


def test_daemon_logs_reads_stdout_and_stderr(monkeypatch, tmp_path: Path):
    job = pal.jobsrt.JobSpec(
        id="watch_silo:docs",
        kind="watch_silo",
        slug="docs-aaaa1111",
        source_path="/tmp/docs",
        service_name="io.llmlibrarian.watch.docs-aaaa1111",
        log_path=str(tmp_path / "watch.log"),
        interval=60,
        debounce=30,
    )
    Path(job.log_path).write_text("hello\nworld\n", encoding="utf-8")
    monkeypatch.setattr("pal.PAL_HOME", tmp_path / ".pal")
    (tmp_path / ".pal" / "logs").mkdir(parents=True)
    pal.jobsrt.watch_stderr_log_path(tmp_path / ".pal", job.slug).write_text("stderr line\n", encoding="utf-8")
    monkeypatch.setattr("pal._daemon_metadata", lambda: {"manager": "launchd", "db_path": "/tmp/db"})
    monkeypatch.setattr("pal._derive_watch_jobs_for_daemon", lambda _manager, db_path=None: ([job], []))

    res = runner.invoke(pal.app, ["daemon", "logs", "docs-aaaa1111", "--lines", "10"])
    assert res.exit_code == 0
    assert "# docs-aaaa1111 stdout" in res.stdout
    assert "hello" in res.stdout
    assert "# docs-aaaa1111 stderr" in res.stdout
