from __future__ import annotations

from pathlib import Path


def test_hatch_editable_includes_repo_root_and_src():
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    raw = pyproject.read_text(encoding="utf-8")
    assert 'dev-mode-dirs = [".", "src"]' in raw
