from __future__ import annotations

from pathlib import Path


def test_hatch_editable_includes_repo_root_and_src():
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    raw = pyproject.read_text(encoding="utf-8")
    assert 'dev-mode-dirs = [".", "src"]' in raw


def test_ocr_extra_installs_paddle_runtime_without_python_tesseract_package():
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    raw = pyproject.read_text(encoding="utf-8")
    ocr_extra = raw.split("ocr = [", 1)[1].split("]", 1)[0]

    assert '"paddleocr>=3.4.0"' in ocr_extra
    assert '"paddlepaddle>=3.0.0"' in ocr_extra
    assert '"tesseract' not in ocr_extra
