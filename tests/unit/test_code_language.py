from pathlib import Path

from file_registry import _write_file_manifest
from query.code_language import (
    format_code_language_year_answer,
    get_code_sources_from_manifest_year,
    get_code_language_stats_from_manifest_year,
)


def test_get_code_language_stats_from_manifest_year_filters_by_mtime_and_code_ext(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "stuff"
    root.mkdir(parents=True, exist_ok=True)

    _write_file_manifest(
        db,
        {
            "silos": {
                "stuff-123": {
                    "path": str(root.resolve()),
                    "files": {
                        str((root / "main.py").resolve()): {"mtime": 1640995200.0, "size": 1, "hash": "h1"},  # 2022
                        str((root / "notes.md").resolve()): {"mtime": 1640995200.0, "size": 1, "hash": "h2"},  # 2022 non-code
                        str((root / "old.js").resolve()): {"mtime": 1609459200.0, "size": 1, "hash": "h3"},   # 2021
                        str((root / "run.sh").resolve()): {"mtime": 1643673600.0, "size": 1, "hash": "h4"},   # 2022
                    },
                }
            }
        },
    )

    by_ext, sample_paths = get_code_language_stats_from_manifest_year(str(db), None, 2022)
    assert by_ext == {".py": 1, ".sh": 1}
    assert any(p.endswith("main.py") for p in sample_paths[".py"])
    assert any(p.endswith("run.sh") for p in sample_paths[".sh"])


def test_get_code_language_stats_from_manifest_year_scoped_silo(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir(parents=True, exist_ok=True)
    r1 = tmp_path / "s1"
    r2 = tmp_path / "s2"
    r1.mkdir(parents=True, exist_ok=True)
    r2.mkdir(parents=True, exist_ok=True)

    _write_file_manifest(
        db,
        {
            "silos": {
                "s1": {
                    "path": str(r1.resolve()),
                    "files": {
                        str((r1 / "a.py").resolve()): {"mtime": 1640995200.0, "size": 1, "hash": "h1"},  # 2022
                    },
                },
                "s2": {
                    "path": str(r2.resolve()),
                    "files": {
                        str((r2 / "b.js").resolve()): {"mtime": 1640995200.0, "size": 1, "hash": "h2"},  # 2022
                    },
                },
            }
        },
    )

    by_ext, sample_paths = get_code_language_stats_from_manifest_year(str(db), "s2", 2022)
    assert by_ext == {".js": 1}
    assert any(p.endswith("b.js") for p in sample_paths[".js"])


def test_get_code_language_stats_from_manifest_year_unscoped_aggregates_all_silos(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir(parents=True, exist_ok=True)
    r1 = tmp_path / "s1"
    r2 = tmp_path / "s2"
    r1.mkdir(parents=True, exist_ok=True)
    r2.mkdir(parents=True, exist_ok=True)

    _write_file_manifest(
        db,
        {
            "silos": {
                "s1": {
                    "path": str(r1.resolve()),
                    "files": {
                        str((r1 / "a.py").resolve()): {"mtime": 1640995200.0, "size": 1, "hash": "h1"},  # 2022
                    },
                },
                "s2": {
                    "path": str(r2.resolve()),
                    "files": {
                        str((r2 / "b.js").resolve()): {"mtime": 1640995200.0, "size": 1, "hash": "h2"},  # 2022
                    },
                },
            }
        },
    )

    by_ext, _samples = get_code_language_stats_from_manifest_year(str(db), None, 2022)
    assert by_ext == {".js": 1, ".py": 1}


def test_get_code_sources_from_manifest_year_returns_all_code_paths(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "stuff"
    root.mkdir(parents=True, exist_ok=True)

    _write_file_manifest(
        db,
        {
            "silos": {
                "stuff-123": {
                    "path": str(root.resolve()),
                    "files": {
                        str((root / "a.py").resolve()): {"mtime": 1640995200.0, "size": 1, "hash": "h1"},   # 2022
                        str((root / "b.sh").resolve()): {"mtime": 1641081600.0, "size": 1, "hash": "h2"},   # 2022
                        str((root / "notes.md").resolve()): {"mtime": 1641081600.0, "size": 1, "hash": "h3"},  # non-code
                    },
                }
            }
        },
    )

    out = get_code_sources_from_manifest_year(str(db), None, 2022)
    assert any(p.endswith("a.py") for p in out)
    assert any(p.endswith("b.sh") for p in out)
    assert not any(p.endswith("notes.md") for p in out)


def test_format_code_language_year_answer_no_results_is_deterministic():
    out = format_code_language_year_answer(
        year=2022,
        by_ext={},
        sample_paths={},
        source_label="llmli",
        no_color=True,
    )
    assert "No code files found from 2022 in llmli." in out


def test_format_code_language_year_answer_includes_year_and_samples():
    out = format_code_language_year_answer(
        year=2022,
        by_ext={".py": 2, ".js": 1},
        sample_paths={".py": ["/tmp/a.py"], ".js": ["/tmp/b.js"]},
        source_label="llmli",
        no_color=True,
    )
    assert "In 2022, your most common coding language was Python (2 files)." in out
    assert "a.py" in out
