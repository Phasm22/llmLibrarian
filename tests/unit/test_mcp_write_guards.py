"""Every mutating MCP tool goes through one confirm guard.

The guard was copy-pasted five times with hand-written messages and, on
add_silo, an inverted default that made it a no-op for the one tool that reads
an arbitrary filesystem path.
"""

from __future__ import annotations

import pytest

import mcp_server

WRITE_TOOLS = ["add_silo", "trigger_reindex", "repair_silo", "update_file", "remove_file"]


@pytest.fixture
def db(monkeypatch, tmp_path):
    d = tmp_path / "db"
    d.mkdir()
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(d))
    return d


@pytest.mark.parametrize("tool_name", WRITE_TOOLS)
def test_every_write_tool_defaults_to_confirm_false(tool_name):
    import inspect

    sig = inspect.signature(getattr(mcp_server, tool_name))
    assert sig.parameters["confirm"].default is False, (
        f"{tool_name} must require an explicit confirm=True"
    )


def test_add_silo_refuses_without_confirm(db, tmp_path):
    target = tmp_path / "some_folder"
    target.mkdir()
    out = mcp_server.add_silo(str(target))
    assert out["status"] == "not_started"
    assert "confirm=True" in out["message"]


def test_trigger_reindex_refuses_without_confirm(db):
    out = mcp_server.trigger_reindex("anything")
    assert out["status"] == "not_started"
    assert "confirm=True" in out["message"]


def test_repair_silo_refuses_without_confirm(db):
    out = mcp_server.repair_silo("anything")
    assert out["status"] == "not_started"
    assert "confirm=True" in out["message"]


def test_update_file_refuses_without_confirm(db):
    out = mcp_server.update_file("silo", "/tmp/whatever.txt")
    assert out["status"] == "not_started"
    assert "confirm=True" in out["message"]


def test_remove_file_refuses_without_confirm(db):
    out = mcp_server.remove_file("silo", "/tmp/whatever.txt")
    assert out["status"] == "not_started"
    assert "confirm=True" in out["message"]


def test_guard_refuses_before_touching_the_filesystem(db, tmp_path):
    """The guard runs first: a bad path must not be reported ahead of it."""
    out = mcp_server.add_silo(str(tmp_path / "does_not_exist"))
    assert out["status"] == "not_started"


def test_require_confirm_passes_through_when_confirmed():
    assert mcp_server._require_confirm(True, "do the thing") is None


def test_require_confirm_message_includes_the_action():
    out = mcp_server._require_confirm(False, "wipe the silo.")
    assert out["status"] == "not_started"
    assert "wipe the silo." in out["message"]
