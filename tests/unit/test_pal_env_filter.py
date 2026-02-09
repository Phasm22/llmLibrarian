from pal import _build_pull_env


def test_build_pull_env_filters_unrelated_vars(monkeypatch):
    monkeypatch.setenv("RANDOM_UNSAFE_VAR", "x")
    monkeypatch.setenv("LLMLIBRARIAN_DB", "/tmp/db")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = _build_pull_env("/tmp/status.json")

    assert env.get("LLMLIBRARIAN_DB") == "/tmp/db"
    assert env.get("PATH") == "/usr/bin"
    assert env.get("LLMLIBRARIAN_STATUS_FILE") == "/tmp/status.json"
    assert env.get("LLMLIBRARIAN_QUIET") == "1"
    assert "RANDOM_UNSAFE_VAR" not in env
