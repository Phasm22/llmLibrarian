import os
from pathlib import Path

import pytest


def test_bootstrap_prefers_llmlibrarian_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from env_bootstrap import bootstrap_llmlibrarian_env

    repo = tmp_path / "repo"
    repo.mkdir()

    env_file = tmp_path / "secrets.env"
    env_file.write_text("OPENAI_API_KEY=from_file\nOTHER=2\n", encoding="utf-8")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OTHER", raising=False)
    monkeypatch.delenv("LLMLIBRARIAN_ENV_BOOTSTRAPPED", raising=False)

    monkeypatch.setenv("LLMLIBRARIAN_ENV_FILE", str(env_file))
    bootstrap_llmlibrarian_env(repo_root=repo)

    assert os.environ["OPENAI_API_KEY"] == "from_file"
    assert os.environ["OTHER"] == "2"


def test_bootstrap_does_not_override_existing_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from env_bootstrap import bootstrap_llmlibrarian_env

    repo = tmp_path / "repo"
    repo.mkdir()

    env_file = tmp_path / "secrets.env"
    env_file.write_text("OPENAI_API_KEY=from_file\n", encoding="utf-8")

    monkeypatch.setenv("OPENAI_API_KEY", "from_env")
    monkeypatch.delenv("LLMLIBRARIAN_ENV_BOOTSTRAPPED", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_ENV_FILE", str(env_file))

    bootstrap_llmlibrarian_env(repo_root=repo)
    assert os.environ["OPENAI_API_KEY"] == "from_env"


def test_bootstrap_legacy_dotenv_requires_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from env_bootstrap import bootstrap_llmlibrarian_env

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("OPENAI_API_KEY=legacy\n", encoding="utf-8")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLMLIBRARIAN_ENV_FILE", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("LLMLIBRARIAN_DOTENV", raising=False)
    monkeypatch.delenv("LLMLIBRARIAN_ENV_BOOTSTRAPPED", raising=False)

    # No user config file in home by default in this test; ensure we don't auto-read repo .env.
    bootstrap_llmlibrarian_env(repo_root=repo)
    assert "OPENAI_API_KEY" not in os.environ

    monkeypatch.delenv("LLMLIBRARIAN_ENV_BOOTSTRAPPED", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_DOTENV", "1")
    bootstrap_llmlibrarian_env(repo_root=repo)
    assert os.environ["OPENAI_API_KEY"] == "legacy"


def test_bootstrap_missing_explicit_env_file_falls_through_to_legacy_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from env_bootstrap import bootstrap_llmlibrarian_env

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("OPENAI_API_KEY=legacy\n", encoding="utf-8")

    missing = tmp_path / "missing.env"

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLMLIBRARIAN_ENV_BOOTSTRAPPED", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_ENV_FILE", str(missing))
    monkeypatch.setenv("LLMLIBRARIAN_DOTENV", "1")

    bootstrap_llmlibrarian_env(repo_root=repo)
    assert os.environ["OPENAI_API_KEY"] == "legacy"
