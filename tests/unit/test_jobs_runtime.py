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

    assert "Restart=always" in unit
    assert "RestartSec=15" in unit
    assert "StandardOutput=append:" in unit
    assert "ExecStart=/tmp/venv/bin/python /tmp/repo/pal.py pull /tmp/docs --watch --interval 60 --debounce 30" in unit
