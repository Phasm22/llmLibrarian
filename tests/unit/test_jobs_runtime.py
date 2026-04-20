from __future__ import annotations

import plistlib
from pathlib import Path

import jobs_runtime


def test_derive_watch_jobs_uses_registered_indexed_sources(tmp_path: Path):
    pal_home = tmp_path / ".pal"
    indexed = tmp_path / "indexed"
    indexed.mkdir()
    missing = tmp_path / "missing"
    unindexed = tmp_path / "unindexed"
    unindexed.mkdir()

    source_registry = {
        "bookmarks": [
            {"path": str(indexed)},
            {"path": str(missing)},
            {"path": str(unindexed)},
        ]
    }
    llmli_registry = {
        "docs-aaaa1111": {"path": str(indexed.resolve()), "display_name": "Docs"},
    }

    jobs, warnings = jobs_runtime.derive_watch_jobs(
        source_registry,
        llmli_registry,
        pal_home=pal_home,
        db_path=tmp_path / "db",
        manager="launchd",
    )

    assert [job.slug for job in jobs] == ["docs-aaaa1111"]
    assert jobs[0].service_name == "io.llmlibrarian.watch.docs-aaaa1111"
    assert jobs[0].log_path.endswith("watch-docs-aaaa1111.log")
    assert len(warnings) == 2


def test_derive_watch_jobs_prefers_hashed_slug_for_duplicate_registry_path(tmp_path: Path):
    pal_home = tmp_path / ".pal"
    indexed = tmp_path / "journalLinker"
    indexed.mkdir()
    source_registry = {"bookmarks": [{"path": str(indexed)}]}
    llmli_registry = {
        "journallinker": {"path": str(indexed.resolve()), "display_name": "journalLinker"},
        "journallinker-397f11d4": {"path": str(indexed.resolve()), "display_name": "journalLinker"},
    }

    jobs, warnings = jobs_runtime.derive_watch_jobs(
        source_registry,
        llmli_registry,
        pal_home=pal_home,
        db_path=tmp_path / "db",
        manager="launchd",
    )

    assert warnings == []
    assert [job.slug for job in jobs] == ["journallinker-397f11d4"]
    assert jobs[0].service_name == "io.llmlibrarian.watch.journallinker-397f11d4"


def test_platform_sync_deduplicates_jobs_by_source_path(monkeypatch, tmp_path: Path):
    source = tmp_path / "journalLinker"
    source.mkdir()
    manager = jobs_runtime.PlatformManager("systemd", home=tmp_path)
    written_slugs: list[str] = []
    activated_slugs: list[str] = []

    def _write(job, **_kwargs):
        written_slugs.append(job.slug)
        return tmp_path / f"{job.slug}.service"

    def _activate(job):
        activated_slugs.append(job.slug)
        return True, None

    monkeypatch.setattr(manager, "write_service", _write)
    monkeypatch.setattr(manager, "activate", _activate)
    monkeypatch.setattr(manager, "existing_service_paths", lambda: [])
    monkeypatch.setattr(jobs_runtime, "_run_command", lambda _cmd: (0, ""))

    old = jobs_runtime.JobSpec(
        id="watch_silo:journallinker",
        kind="watch_silo",
        slug="journallinker",
        source_path=str(source),
        service_name="llmlibrarian-watch-journallinker.service",
        log_path=str(tmp_path / "old.log"),
        interval=60,
        debounce=30,
    )
    new = jobs_runtime.JobSpec(
        id="watch_silo:journallinker-397f11d4",
        kind="watch_silo",
        slug="journallinker-397f11d4",
        source_path=str(source),
        service_name="llmlibrarian-watch-journallinker-397f11d4.service",
        log_path=str(tmp_path / "new.log"),
        interval=60,
        debounce=30,
    )

    result = manager.sync(
        [old, new],
        python_executable="/tmp/python",
        pal_path="/tmp/pal.py",
        workdir="/tmp",
        env={"PAL_HOME": str(tmp_path / ".pal")},
    )

    assert written_slugs == ["journallinker-397f11d4"]
    assert activated_slugs == ["journallinker-397f11d4"]
    assert result["written"] == ["llmlibrarian-watch-journallinker-397f11d4.service"]


def test_render_launchd_plist_contains_expected_paths(tmp_path: Path):
    job = jobs_runtime.JobSpec(
        id="watch_silo:docs",
        kind="watch_silo",
        slug="docs-aaaa1111",
        source_path="/tmp/docs",
        service_name="io.llmlibrarian.watch.docs-aaaa1111",
        log_path=str(tmp_path / "watch.log"),
        interval=60,
        debounce=30,
    )

    raw = jobs_runtime.render_launchd_plist(
        job,
        python_executable="/tmp/venv/bin/python",
        pal_path="/tmp/repo/pal.py",
        workdir="/tmp/repo",
        env={"LLMLIBRARIAN_DB": "/tmp/db", "PAL_HOME": str(tmp_path / ".pal")},
        stderr_path=str(tmp_path / "watch.err.log"),
    )
    payload = plistlib.loads(raw.encode("utf-8"))

    assert payload["Label"] == job.service_name
    assert payload["ProgramArguments"][:3] == ["/tmp/venv/bin/python", "/tmp/repo/pal.py", "pull"]
    assert payload["ProgramArguments"][3:] == ["/tmp/docs", "--watch", "--interval", "60", "--debounce", "30"]
    assert payload["StandardOutPath"] == job.log_path
    assert payload["StandardErrorPath"].endswith("watch.err.log")


def test_render_systemd_unit_contains_restart_and_logs(tmp_path: Path):
    job = jobs_runtime.JobSpec(
        id="watch_silo:docs",
        kind="watch_silo",
        slug="docs-aaaa1111",
        source_path="/tmp/docs",
        service_name="llmlibrarian-watch-docs-aaaa1111.service",
        log_path=str(tmp_path / "watch.log"),
        interval=60,
        debounce=30,
    )

    unit = jobs_runtime.render_systemd_unit(
        job,
        python_executable="/tmp/venv/bin/python",
        pal_path="/tmp/repo/pal.py",
        workdir="/tmp/repo",
        env={"LLMLIBRARIAN_DB": "/tmp/db", "PAL_HOME": str(tmp_path / ".pal")},
        stderr_path=str(tmp_path / "watch.err.log"),
    )

    assert "Restart=on-failure" in unit
    assert "StartLimitBurst=3" in unit
    assert "RestartSec=15" in unit
    assert "StandardOutput=append:" in unit
    assert "ExecStart=/tmp/venv/bin/python /tmp/repo/pal.py pull /tmp/docs --watch --interval 60 --debounce 30" in unit
