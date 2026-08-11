"""Coverage for mcp_runtime_status runtime visibility payload."""

from __future__ import annotations

import pytest


@pytest.fixture
def mcp_module(monkeypatch, tmp_path):
    import mcp_server

    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    return mcp_server


def test_mcp_runtime_status_shape(monkeypatch, mcp_module):
    monkeypatch.setattr(
        mcp_module,
        "_collect_health_summary",
        lambda include_audit=False: {
            "db_exists": True,
            "chroma_transport": "http",
            "chroma_server_ok": True,
            "chroma_server_host": "127.0.0.1",
            "chroma_server_port": 8000,
            "query_health": {"recent_error_count": 2},
            "ingest_failures": {"last_failure_count": 1},
            "hnsw_consistency": {"desynced_count": 0},
            "active_background_jobs": {"docs": {"kind": "trigger_reindex"}},
            "last_background_reindex": {"docs": {"ok": True}},
        },
    )
    monkeypatch.setattr(
        mcp_module,
        "_read_mcp_pid_lock_snapshot",
        lambda: {
            "pid_lock_path": "/tmp/llmlibrarian-mcp-1000.pid",
            "lock_file_exists": True,
            "lock_holder_pid": 123,
            "lock_holder_alive": True,
        },
    )
    monkeypatch.setattr(
        mcp_module,
        "_mcp_process_snapshot",
        lambda verbose=False: {"mcp_process_count": 1, "multiple_mcp_processes": False},
    )
    monkeypatch.setattr(mcp_module, "_derive_recommended_actions", lambda *_a, **_k: ["from-health"])

    out = mcp_module.mcp_runtime_status()

    assert out["db_exists"] is True
    assert out["mcp_http"]["lock_holder_pid"] == 123
    assert out["chroma"]["transport"] == "http"
    assert out["health_counts"] == {
        "query_error_count": 2,
        "ingest_failure_count": 1,
        "hnsw_desynced_count": 0,
    }
    assert out["jobs"]["active_count"] == 1
    assert out["jobs"]["active_background_jobs"]["docs"]["kind"] == "trigger_reindex"
    assert "from-health" in out["recommended_actions"]


def test_mcp_runtime_status_adds_runtime_actions(monkeypatch, mcp_module):
    monkeypatch.setattr(
        mcp_module,
        "_collect_health_summary",
        lambda include_audit=False: {
            "db_exists": True,
            "chroma_transport": "http",
            "chroma_server_ok": True,
            "query_health": {"recent_error_count": 0},
            "ingest_failures": {"last_failure_count": 0},
            "hnsw_consistency": {"desynced_count": 0},
            "active_background_jobs": {},
            "last_background_reindex": {},
        },
    )
    monkeypatch.setattr(
        mcp_module,
        "_read_mcp_pid_lock_snapshot",
        lambda: {
            "pid_lock_path": "/tmp/llmlibrarian-mcp-1000.pid",
            "lock_file_exists": True,
            "lock_holder_pid": 4321,
            "lock_holder_alive": False,
        },
    )
    monkeypatch.setattr(
        mcp_module,
        "_mcp_process_snapshot",
        lambda verbose=False: {
            "mcp_process_count": 2,
            "multiple_mcp_processes": True,
            "mcp_stdio_count": 2,
            "multiple_stdio_processes": True,
        },
    )
    monkeypatch.setattr(mcp_module, "_derive_recommended_actions", lambda *_a, **_k: [])

    out = mcp_module.mcp_runtime_status()
    actions = out["recommended_actions"]

    assert any("Multiple stdio MCP servers" in row for row in actions)
    assert any("PID lock file points to a dead process" in row for row in actions)


def test_healthy_topology_does_not_recommend_killing_processes(monkeypatch, mcp_module):
    """One shared http service + one stdio client must not warn.

    Regression: the advice was keyed on the total process count, so the
    supported topology told the operator to stop orphan processes that were
    not orphans.
    """
    monkeypatch.setattr(
        mcp_module,
        "_collect_health_summary",
        lambda include_audit=False: {
            "db_exists": True,
            "chroma_transport": "http",
            "chroma_server_ok": True,
            "query_health": {"recent_error_count": 0},
            "ingest_failures": {"last_failure_count": 0},
            "hnsw_consistency": {"desynced_count": 0},
            "active_background_jobs": {},
            "last_background_reindex": {},
        },
    )
    monkeypatch.setattr(
        mcp_module,
        "_read_mcp_pid_lock_snapshot",
        lambda: {"pid_lock_path": "/tmp/p.pid", "lock_file_exists": False},
    )
    monkeypatch.setattr(
        mcp_module,
        "_mcp_process_snapshot",
        lambda verbose=False: {
            "mcp_process_count": 2,
            "multiple_mcp_processes": True,
            "mcp_stdio_count": 1,
            "multiple_stdio_processes": False,
        },
    )
    monkeypatch.setattr(mcp_module, "_derive_recommended_actions", lambda *_a, **_k: [])

    actions = mcp_module.mcp_runtime_status()["recommended_actions"]

    assert not any("stdio MCP servers" in row for row in actions)
    assert not any("orphan" in row.lower() for row in actions)


def test_mcp_runtime_status_verbose_includes_summary(monkeypatch, mcp_module):
    summary = {
        "db_exists": True,
        "chroma_transport": "embedded",
        "chroma_server_ok": True,
        "query_health": {"recent_error_count": 0},
        "ingest_failures": {"last_failure_count": 0},
        "hnsw_consistency": {"desynced_count": 0},
        "active_background_jobs": {},
        "last_background_reindex": {},
    }
    monkeypatch.setattr(mcp_module, "_collect_health_summary", lambda include_audit=False: summary)
    monkeypatch.setattr(
        mcp_module,
        "_read_mcp_pid_lock_snapshot",
        lambda: {"pid_lock_path": "/tmp/x", "lock_file_exists": False, "lock_holder_pid": None, "lock_holder_alive": None},
    )
    monkeypatch.setattr(
        mcp_module,
        "_mcp_process_snapshot",
        lambda verbose=False: {
            "mcp_process_count": 1,
            "multiple_mcp_processes": False,
            **({"processes": [{"pid": 1, "cmdline": "python mcp_server.py"}]} if verbose else {}),
        },
    )
    monkeypatch.setattr(mcp_module, "_derive_recommended_actions", lambda *_a, **_k: [])

    out = mcp_module.mcp_runtime_status(verbose=True)

    assert out["summary_raw"] == summary
    assert "processes" in out["mcp_http"]


# ── process introspection: both backends, on every host ─────────────────────
# These exercise the procfs and ps paths regardless of the OS running the
# suite. Without that, Linux CI would only ever cover procfs and macOS only
# ever ps, so a regression in the other backend ships unnoticed — which is
# how "0 MCP processes" went undetected on macOS while three were live.

_MCP_CMD = "/repo/.venv/bin/python /Users/x/llmLibrarian/mcp_server.py"
_OTHER_CMD = "/usr/bin/python /some/other/thing.py"


def test_process_snapshot_uses_ps_when_procfs_absent(monkeypatch, mcp_module):
    monkeypatch.setattr(mcp_module.Path, "is_dir", lambda self: False)
    monkeypatch.setattr(
        mcp_module,
        "_mcp_rows_from_ps",
        lambda: [{"pid": 11, "cmdline": _MCP_CMD}, {"pid": 12, "cmdline": _MCP_CMD}],
    )

    out = mcp_module._mcp_process_snapshot()

    assert out["introspection_source"] == "ps"
    assert out["mcp_process_count"] == 2
    assert out["multiple_mcp_processes"] is True
    assert "introspection_error" not in out


def test_process_snapshot_prefers_procfs_when_present(monkeypatch, mcp_module):
    monkeypatch.setattr(mcp_module.Path, "is_dir", lambda self: True)
    monkeypatch.setattr(mcp_module, "_mcp_rows_from_proc", lambda: [{"pid": 9, "cmdline": _MCP_CMD}])
    monkeypatch.setattr(
        mcp_module,
        "_mcp_rows_from_ps",
        lambda: pytest.fail("ps must not run when procfs is available"),
    )

    out = mcp_module._mcp_process_snapshot()

    assert out["introspection_source"] == "procfs"
    assert out["mcp_process_count"] == 1
    assert out["multiple_mcp_processes"] is False


def test_ps_backend_parses_and_filters(monkeypatch, mcp_module):
    """Only llmLibrarian MCP rows count; ps's own header/noise must not."""
    stdout = "\n".join(
        [
            f"  11 {_MCP_CMD}",
            f"  12 {_OTHER_CMD}",
            f"{mcp_module.os.getpid()} {_MCP_CMD}",
            "notapid whatever",
            "",
        ]
    )
    monkeypatch.setattr(
        mcp_module.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": stdout, "returncode": 0})(),
    )

    rows = mcp_module._mcp_rows_from_ps()

    assert [r["pid"] for r in rows] == [11, mcp_module.os.getpid()]
    assert rows[1]["self"] is True


def test_process_snapshot_reports_error_when_backend_raises(monkeypatch, mcp_module):
    monkeypatch.setattr(mcp_module.Path, "is_dir", lambda self: False)

    def _boom():
        raise OSError("ps missing")

    monkeypatch.setattr(mcp_module, "_mcp_rows_from_ps", _boom)

    out = mcp_module._mcp_process_snapshot()

    assert out["mcp_process_count"] == 0
    assert "OSError" in out["introspection_error"]


def test_process_snapshot_drops_cmdline_unless_verbose(monkeypatch, mcp_module):
    monkeypatch.setattr(mcp_module.Path, "is_dir", lambda self: False)
    monkeypatch.setattr(mcp_module, "_mcp_rows_from_ps", lambda: [{"pid": 11, "cmdline": _MCP_CMD}])

    assert "processes" not in mcp_module._mcp_process_snapshot()

    monkeypatch.setattr(mcp_module, "_mcp_rows_from_ps", lambda: [{"pid": 11, "cmdline": _MCP_CMD}])
    verbose = mcp_module._mcp_process_snapshot(verbose=True)
    assert verbose["processes"][0]["cmdline"] == _MCP_CMD


# ── server vs launcher discrimination ───────────────────────────────────────
# Regression: the predicate matched "mcp_server.py" in the command line, but
# servers replace argv with a proctitle, so it counted the launchers that
# still mention the script and missed every real server. Observed live as
# "3 MCP processes" — three launchers, one uncounted server.

_PROCTITLE_STDIO = "llmLibrarian-mcp:stdio:claude"
_PROCTITLE_HTTP = "llmLibrarian-mcp:http:8766"
# ps appends inherited env after a proctitle.
_PROCTITLE_WITH_ENV = "llmLibrarian-mcp:http:8766 __PYVENV_LAUNCHER__=/repo/.venv/bin/python"
_BARE_PYTHON = "/repo/.venv/bin/python /Users/x/llmLibrarian/mcp_server.py"

_LAUNCHERS = [
    "uv --directory /Users/x/llmLibrarian run mcp_server.py",
    "/Applications/Claude.app/Contents/Helpers/disclaimer /repo/.venv/bin/python /Users/x/llmLibrarian/mcp_server.py",
    '/path/claude --mcp-config {"llmlibrarian":{"args":["/Users/x/llmLibrarian/mcp_server.py"]}}',
]


@pytest.mark.parametrize("cmd", [_PROCTITLE_STDIO, _PROCTITLE_HTTP, _PROCTITLE_WITH_ENV, _BARE_PYTHON])
def test_counts_real_servers(mcp_module, cmd):
    assert mcp_module._is_mcp_cmdline(cmd) is True


@pytest.mark.parametrize("cmd", _LAUNCHERS)
def test_rejects_launchers_that_merely_name_the_script(mcp_module, cmd):
    assert mcp_module._is_mcp_cmdline(cmd) is False


@pytest.mark.parametrize(
    "cmd,expected",
    [
        (_PROCTITLE_STDIO, "stdio"),
        (_PROCTITLE_HTTP, "http"),
        (_PROCTITLE_WITH_ENV, "http"),
        (_BARE_PYTHON, None),
        ("llmLibrarian-mcp", None),
    ],
)
def test_transport_parsed_from_proctitle(mcp_module, cmd, expected):
    assert mcp_module._mcp_transport(cmd) == expected


def test_shared_http_plus_one_stdio_is_not_flagged_as_duplicate(monkeypatch, mcp_module):
    """The documented topology: one shared http service + per-client stdio."""
    monkeypatch.setattr(mcp_module.Path, "is_dir", lambda self: False)
    monkeypatch.setattr(
        mcp_module,
        "_mcp_rows_from_ps",
        lambda: [{"pid": 1, "cmdline": _PROCTITLE_HTTP}, {"pid": 2, "cmdline": _PROCTITLE_STDIO}],
    )

    out = mcp_module._mcp_process_snapshot()

    assert out["mcp_process_count"] == 2
    assert out["mcp_stdio_count"] == 1
    assert out["multiple_stdio_processes"] is False


def test_two_stdio_servers_are_flagged(monkeypatch, mcp_module):
    """The real leak shape — e.g. a stale server the host never reaped."""
    monkeypatch.setattr(mcp_module.Path, "is_dir", lambda self: False)
    monkeypatch.setattr(
        mcp_module,
        "_mcp_rows_from_ps",
        lambda: [{"pid": 1, "cmdline": _PROCTITLE_STDIO}, {"pid": 2, "cmdline": _PROCTITLE_STDIO}],
    )

    out = mcp_module._mcp_process_snapshot()

    assert out["mcp_stdio_count"] == 2
    assert out["multiple_stdio_processes"] is True


def test_snapshot_reports_transport_per_process(monkeypatch, mcp_module):
    monkeypatch.setattr(mcp_module.Path, "is_dir", lambda self: False)
    monkeypatch.setattr(
        mcp_module,
        "_mcp_rows_from_ps",
        lambda: [{"pid": 1, "cmdline": _PROCTITLE_HTTP}, {"pid": 2, "cmdline": _BARE_PYTHON}],
    )

    procs = mcp_module._mcp_process_snapshot(verbose=True)["processes"]

    assert procs[0]["transport"] == "http"
    assert "transport" not in procs[1]  # no proctitle -> unknown, not guessed


def test_ps_backend_end_to_end_filters_launchers(monkeypatch, mcp_module):
    """Full ps parse: the live case that produced the wrong count."""
    stdout = "\n".join(
        [
            f"  100 {_LAUNCHERS[0]}",
            f"  101 {_LAUNCHERS[1]}",
            f"  102 {_PROCTITLE_STDIO}",
            f"  103 {_PROCTITLE_HTTP}",
        ]
    )
    monkeypatch.setattr(
        mcp_module.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"stdout": stdout, "returncode": 0})(),
    )

    assert [r["pid"] for r in mcp_module._mcp_rows_from_ps()] == [102, 103]
