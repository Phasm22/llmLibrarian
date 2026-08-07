"""DB-path resolution for the MCP server.

This heuristic decides where the whole index lives and previously had no tests.
Its riskiest branch is the fallback: when mcp_server.py is installed into
site-packages, resolving to the script root silently creates a second, hidden
DB inside .venv that the user never finds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_runtime import bootstrap


def _make_checkout(root: Path, *, with_db: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "cli.py").write_text("", encoding="utf-8")
    (root / "src").mkdir(exist_ok=True)
    if with_db:
        (root / "my_brain_db").mkdir(exist_ok=True)
    return root


def test_env_var_wins(monkeypatch, tmp_path):
    target = tmp_path / "explicit"
    target.mkdir()
    monkeypatch.setenv("LLMLIBRARIAN_DB", str(target))
    assert bootstrap.resolve_db_path(tmp_path) == str(target.resolve())


def test_unsubstituted_host_template_is_ignored(monkeypatch, tmp_path):
    """Claude Desktop can launch us with a literal '${user.db_path}'."""
    monkeypatch.setenv("LLMLIBRARIAN_DB", "${user.db_path}")
    checkout = _make_checkout(tmp_path / "repo", with_db=True)
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(bootstrap, "_db_path_from_desktop_settings", lambda: None)

    assert bootstrap.resolve_db_path(checkout) == str((checkout / "my_brain_db").resolve())


def test_existing_checkout_db_beats_script_root(monkeypatch, tmp_path):
    """The script root may be site-packages; a DB created there hides in .venv."""
    monkeypatch.delenv("LLMLIBRARIAN_DB", raising=False)
    monkeypatch.setattr(bootstrap, "_db_path_from_desktop_settings", lambda: None)
    checkout = _make_checkout(tmp_path / "repo", with_db=True)
    script_root = _make_checkout(tmp_path / "site-packages", with_db=False)
    monkeypatch.chdir(checkout)

    assert bootstrap.resolve_db_path(script_root) == str((checkout / "my_brain_db").resolve())


def test_falls_back_to_cwd_checkout_when_no_db_exists(monkeypatch, tmp_path):
    monkeypatch.delenv("LLMLIBRARIAN_DB", raising=False)
    monkeypatch.setattr(bootstrap, "_db_path_from_desktop_settings", lambda: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    checkout = _make_checkout(tmp_path / "repo")
    monkeypatch.chdir(checkout)

    assert bootstrap.resolve_db_path(tmp_path / "elsewhere") == str(
        (checkout / "my_brain_db").resolve()
    )


def test_looks_like_checkout_requires_both_markers(tmp_path):
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "cli.py").write_text("", encoding="utf-8")
    assert not bootstrap.looks_like_checkout(partial)
    (partial / "src").mkdir()
    assert bootstrap.looks_like_checkout(partial)


def test_silence_stdio_noise_sets_defaults_without_clobbering(monkeypatch):
    monkeypatch.setenv("TQDM_DISABLE", "0")
    monkeypatch.delenv("HF_HUB_DISABLE_PROGRESS_BARS", raising=False)
    monkeypatch.delenv("LLMLIBRARIAN_EXIT_ON_STALE_GENERATION", raising=False)

    bootstrap.silence_stdio_noise()

    import os

    assert os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] == "1"
    # An operator's explicit setting survives.
    assert os.environ["TQDM_DISABLE"] == "0"
    # Long-lived reader: exit rather than query through a stale cached client.
    assert os.environ["LLMLIBRARIAN_EXIT_ON_STALE_GENERATION"] == "1"


@pytest.mark.parametrize("transport", ["stdio", "http", "sse", "streamable-http"])
def test_valid_transports_accepted(monkeypatch, transport):
    from mcp_runtime import runtime

    monkeypatch.setenv("LLMLIBRARIAN_MCP_TRANSPORT", transport)
    assert runtime.resolve_transport() == transport


def test_invalid_transport_is_rejected(monkeypatch):
    from mcp_runtime import runtime

    monkeypatch.setenv("LLMLIBRARIAN_MCP_TRANSPORT", "carrier-pigeon")
    with pytest.raises(RuntimeError, match="must be one of"):
        runtime.resolve_transport()


def test_auth_is_never_applied_to_stdio(monkeypatch):
    """stdio is launched directly by the host; there is no network boundary."""
    from mcp_runtime import runtime

    monkeypatch.setenv("LLMLIBRARIAN_MCP_REQUIRE_AUTH", "true")
    monkeypatch.setenv("LLMLIBRARIAN_MCP_AUTH_TOKEN", "tok")
    assert runtime.auth_for_transport("stdio") is None


def test_auth_requires_a_token_when_enabled(monkeypatch):
    from mcp_runtime import runtime

    monkeypatch.setenv("LLMLIBRARIAN_MCP_REQUIRE_AUTH", "true")
    monkeypatch.setenv("LLMLIBRARIAN_MCP_AUTH_TOKEN", "")
    with pytest.raises(RuntimeError, match="LLMLIBRARIAN_MCP_AUTH_TOKEN is required"):
        runtime.auth_for_transport("streamable-http")


def test_auth_disabled_by_default(monkeypatch):
    from mcp_runtime import runtime

    monkeypatch.delenv("LLMLIBRARIAN_MCP_REQUIRE_AUTH", raising=False)
    assert runtime.auth_for_transport("streamable-http") is None


@pytest.mark.parametrize(
    "host,expected",
    [("127.0.0.1", True), ("localhost", True), ("::1", True), ("0.0.0.0", False), ("10.0.0.5", False)],
)
def test_loopback_detection(monkeypatch, host, expected):
    from mcp_runtime import runtime

    monkeypatch.setenv("LLMLIBRARIAN_MCP_HOST", host)
    assert runtime.is_loopback_bind() is expected
