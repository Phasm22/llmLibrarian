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
