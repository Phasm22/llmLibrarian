"""A long-lived MCP process notices when the code under it changed.

2026-10-02: a Claude Desktop stdio server started 09-28 raised
"cannot import name 'read_visible_manifest' from 'file_registry'" on every
find_files — an old cached file_registry meeting a newer operations_find.
"""
from __future__ import annotations

import asyncio
import os
import time

import code_drift


def test_drift_detects_newer_source(tmp_path):
    (tmp_path / "src").mkdir()
    mod = tmp_path / "src" / "file_registry.py"
    mod.write_text("x = 1\n")
    start = code_drift.snapshot(tmp_path)
    assert code_drift.drift(start, tmp_path)["changed_since_start"] is False

    later = time.time() + 5
    os.utime(mod, (later, later))
    report = code_drift.drift(start, tmp_path)
    assert report["changed_since_start"] is True and report["source_files_modified"]


def test_restart_hint_names_the_right_restart():
    assert "MCP client" in code_drift.restart_hint(transport="stdio", pid=1, started_at="t")
    assert "pal mcp" in code_drift.restart_hint(transport="streamable-http", pid=1, started_at="t")


def test_import_error_in_a_tool_says_restart():
    from fastmcp import Client, FastMCP

    import mcp_server

    server = FastMCP("drift-test")
    server.add_middleware(mcp_server._ImportErrorExplainer())

    @server.tool()
    def find_files() -> dict:
        raise ImportError("cannot import name 'read_visible_manifest' from 'file_registry'")

    async def call() -> str:
        async with Client(server) as client:
            try:
                await client.call_tool("find_files", {})
            except Exception as e:  # ToolError surfaced client-side
                return str(e)
        return ""

    message = asyncio.run(call())
    assert "read_visible_manifest" in message
    assert "changed after this MCP server started" in message


def test_stdio_client_classification():
    import mcp_server

    parents = {
        500: (400, "llmLibrarian-mcp:stdio:claude"),
        400: (300, "uv"),
        300: (1, "claude"),  # live client session
        600: (450, "llmLibrarian-mcp:stdio:claude"),
        450: (1, "uv"),  # client gone: uv reparented to launchd
    }
    assert mcp_server._client_of(500, parents) == "claude"
    assert mcp_server._client_of(600, parents) is None


def test_db_fallback_ignores_a_directory_that_only_holds_a_lock(monkeypatch, tmp_path):
    """A worktree session with LLMLIBRARIAN_DB unset created <worktree>/my_brain_db
    holding only .llmli_chroma.flock; every later session resolved to that empty
    "DB" and reported db_exists=false for everything."""
    import mcp_server

    checkout = tmp_path / "worktree"
    (checkout / "src").mkdir(parents=True)
    (checkout / "cli.py").write_text("")
    (checkout / "my_brain_db").mkdir()
    (checkout / "my_brain_db" / ".llmli_chroma.flock").write_text("")
    home = tmp_path / "home"
    real = home / "llmLibrarian" / "my_brain_db"
    real.mkdir(parents=True)
    (real / "llmli_registry.json").write_text("{}")

    monkeypatch.setenv("LLMLIBRARIAN_DB", "")
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(mcp_server, "_ROOT", checkout)
    monkeypatch.setattr(mcp_server.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(
        mcp_server, "_resolve_db_path", mcp_server._resolve_db_path
    )  # explicit: exercising the real function

    assert mcp_server._resolve_db_path() == str(real.resolve())
