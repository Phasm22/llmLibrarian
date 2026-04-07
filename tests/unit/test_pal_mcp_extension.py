import json

from typer.testing import CliRunner

import pal


runner = CliRunner()


def test_extension_pack_writes_hash_record(monkeypatch, tmp_path):
    monkeypatch.setattr(pal, "PAL_HOME", tmp_path)
    mcp = tmp_path / "mcp_server.py"
    mcp.write_text("print('stub')\n", encoding="utf-8")
    monkeypatch.setattr(pal, "_mcp_server_py_path", lambda: mcp.resolve())

    monkeypatch.setenv("LLMLIBRARIAN_MCP_PACK_CMD", "true")
    res = runner.invoke(pal.app, ["extension", "pack"])
    assert res.exit_code == 0, res.stdout + (res.stderr or "")
    rec_path = tmp_path / "mcpb_source_hash"
    assert rec_path.is_file()
    data = json.loads(rec_path.read_text(encoding="utf-8"))
    assert data.get("sha256") and len(data["sha256"]) == 64
    assert data.get("packed_at")


def test_extension_pack_requires_env(monkeypatch):
    monkeypatch.delenv("LLMLIBRARIAN_MCP_PACK_CMD", raising=False)
    res = runner.invoke(pal.app, ["extension", "pack"])
    assert res.exit_code == 2
    assert "LLMLIBRARIAN_MCP_PACK_CMD" in (res.stderr or "")


def test_warn_mcp_stale_when_record_missing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(pal, "PAL_HOME", tmp_path)
    mcp = tmp_path / "mcp_server.py"
    mcp.write_text("x", encoding="utf-8")
    monkeypatch.setattr(pal, "_mcp_server_py_path", lambda: mcp.resolve())

    pal._warn_mcp_desktop_extension_stale()
    err = capsys.readouterr().err
    assert "stale" in err.lower() or "pack" in err.lower()


def test_warn_mcp_not_emitted_when_hash_matches(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(pal, "PAL_HOME", tmp_path)
    mcp = tmp_path / "mcp_server.py"
    mcp.write_text("same content", encoding="utf-8")
    monkeypatch.setattr(pal, "_mcp_server_py_path", lambda: mcp.resolve())

    import hashlib

    digest = hashlib.sha256(mcp.read_bytes()).hexdigest()
    (tmp_path / "mcpb_source_hash").write_text(json.dumps({"sha256": digest}) + "\n", encoding="utf-8")

    pal._warn_mcp_desktop_extension_stale()
    assert capsys.readouterr().err == ""
