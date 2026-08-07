from __future__ import annotations

from pathlib import Path


def test_persistent_client_is_only_constructed_in_chroma_client():
    repo_root = Path(__file__).resolve().parents[2]
    src_root = repo_root / "src"
    allowlist = {
        repo_root / "src" / "chroma_client.py",
    }
    needle = "PersistentClient("
    offenders: list[str] = []
    for path in src_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if path in allowlist:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if needle in text:
            offenders.append(str(path.relative_to(repo_root)))
    assert offenders == [], (
        "Direct PersistentClient construction is forbidden outside src/chroma_client.py: "
        + ", ".join(offenders)
    )


def test_client_is_constructed_through_one_helper():
    """Even inside chroma_client, both modes go through _open_raw_client.

    writer_client used to hand-roll its own PersistentClient with a duplicate
    Settings literal, so any change to client construction had to be made twice
    or silently diverged between the read and write paths.
    """
    src = (Path(__file__).resolve().parents[2] / "src" / "chroma_client.py").read_text()
    assert src.count("chromadb.PersistentClient(") == 1
    assert src.count("chromadb.HttpClient(") == 1


def test_env_flag_parsing_is_not_reimplemented():
    """One accepted spelling set for booleans, from constants.env_flag.

    Ad-hoc copies had drifted into three different sets, so `on` was truthy for
    MCP auth and falsy for every Chroma variable.
    """
    repo_root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for rel in ("src/chroma_client.py", "src/chroma_lock.py", "mcp_server.py"):
        text = (repo_root / rel).read_text(encoding="utf-8")
        for marker in ('in ("1", "true", "yes")', 'in {"1", "true", "yes"}'):
            if marker in text:
                offenders.append(f"{rel}: {marker}")
    assert offenders == [], "use constants.env_flag instead: " + ", ".join(offenders)
