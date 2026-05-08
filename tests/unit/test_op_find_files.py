import json
from datetime import date
from pathlib import Path

import pytest

from operations_find import op_find_files


def _write_manifest(db_path: Path, silos: dict) -> None:
    db_path.mkdir(parents=True, exist_ok=True)
    (db_path / "llmli_file_manifest.json").write_text(json.dumps({"silos": silos}))


def _silo(root: str, files: dict[str, dict]) -> dict:
    return {"path": root, "files": files}


def test_no_filters_returns_all_files_sorted_by_name_date_desc(tmp_path: Path):
    silo_root = str(tmp_path / "journal")
    files = {
        f"{silo_root}/2026-05-04.md": {"mtime": 1000.0, "size": 10, "hash": "a"},
        f"{silo_root}/2026-05-06.md": {"mtime": 1200.0, "size": 12, "hash": "c"},
        f"{silo_root}/2026-05-05.md": {"mtime": 1100.0, "size": 11, "hash": "b"},
    }
    _write_manifest(tmp_path, {"journal": _silo(silo_root, files)})

    result = op_find_files(tmp_path)

    paths = [f["path"] for f in result["files"]]
    assert paths == [
        f"{silo_root}/2026-05-06.md",
        f"{silo_root}/2026-05-05.md",
        f"{silo_root}/2026-05-04.md",
    ]
    assert result["total_matched"] == 3
    assert result["total_scanned"] == 3
    assert result["truncated"] is False


def test_date_range_filters_by_name_date(tmp_path: Path):
    silo_root = str(tmp_path / "j")
    files = {
        f"{silo_root}/2026-05-04.md": {"mtime": 1000.0, "size": 10, "hash": "a"},
        f"{silo_root}/2026-05-06.md": {"mtime": 1200.0, "size": 12, "hash": "c"},
        f"{silo_root}/2026-06-01.md": {"mtime": 1300.0, "size": 13, "hash": "d"},
    }
    _write_manifest(tmp_path, {"j": _silo(silo_root, files)})

    result = op_find_files(
        tmp_path,
        date_start=date(2026, 5, 1),
        date_end=date(2026, 5, 31),
        date_field="name_date",
    )
    paths = [f["path"] for f in result["files"]]
    assert paths == [f"{silo_root}/2026-05-06.md", f"{silo_root}/2026-05-04.md"]
    for f in result["files"]:
        assert f["date_source"] == "name_date"


def test_date_field_either_marks_both_when_signals_agree(tmp_path: Path):
    silo_root = str(tmp_path / "j")
    # Build mtime that resolves locally to 2026-05-06 (use noon UTC to dodge tz edges)
    import datetime as _dt
    mtime = _dt.datetime(2026, 5, 6, 12, 0, 0).timestamp()
    files = {
        f"{silo_root}/2026-05-06.md": {"mtime": mtime, "size": 10, "hash": "a"},
    }
    _write_manifest(tmp_path, {"j": _silo(silo_root, files)})

    result = op_find_files(
        tmp_path,
        date_start=date(2026, 5, 6),
        date_end=date(2026, 5, 6),
        date_field="either",
    )
    assert len(result["files"]) == 1
    assert result["files"][0]["date_source"] == "both"


def test_date_field_either_uses_mtime_when_name_date_absent(tmp_path: Path):
    silo_root = str(tmp_path / "j")
    import datetime as _dt
    mtime = _dt.datetime(2026, 5, 6, 12, 0, 0).timestamp()
    files = {
        f"{silo_root}/notes.md": {"mtime": mtime, "size": 10, "hash": "a"},
    }
    _write_manifest(tmp_path, {"j": _silo(silo_root, files)})

    result = op_find_files(
        tmp_path,
        date_start=date(2026, 5, 6),
        date_end=date(2026, 5, 6),
        date_field="either",
    )
    assert len(result["files"]) == 1
    hit = result["files"][0]
    assert hit["date_source"] == "mtime"
    assert hit["name_date"] is None


def test_name_date_lazy_derive_from_path_no_writeback(tmp_path: Path):
    """If manifest entry lacks name_date, op_find_files derives it on the fly."""
    silo_root = str(tmp_path / "j")
    files = {
        f"{silo_root}/2026-05-06.md": {"mtime": 1000.0, "size": 10, "hash": "a"},
    }
    _write_manifest(tmp_path, {"j": _silo(silo_root, files)})

    result = op_find_files(
        tmp_path,
        date_start=date(2026, 5, 6),
        date_end=date(2026, 5, 6),
        date_field="name_date",
    )
    assert len(result["files"]) == 1
    assert result["files"][0]["name_date"] == "2026-05-06"
    assert result["files"][0]["name_date_precision"] == "day"

    # And the manifest on disk is unchanged.
    raw = json.loads((tmp_path / "llmli_file_manifest.json").read_text())
    stored = raw["silos"]["j"]["files"][f"{silo_root}/2026-05-06.md"]
    assert "name_date" not in stored


def test_stored_name_date_is_preferred_over_filename(tmp_path: Path):
    silo_root = str(tmp_path / "j")
    files = {
        # Filename would parse to 2026-05-06, but stored value overrides.
        f"{silo_root}/2026-05-06.md": {
            "mtime": 1000.0,
            "size": 10,
            "hash": "a",
            "name_date": "2024-01-15",
            "name_date_precision": "day",
        },
    }
    _write_manifest(tmp_path, {"j": _silo(silo_root, files)})

    result = op_find_files(tmp_path)
    assert result["files"][0]["name_date"] == "2024-01-15"


def test_month_precision_only_matches_full_month_range(tmp_path: Path):
    silo_root = str(tmp_path / "j")
    files = {
        f"{silo_root}/2026-05.md": {"mtime": 1000.0, "size": 10, "hash": "a"},
    }
    _write_manifest(tmp_path, {"j": _silo(silo_root, files)})

    # Single-day range does not cover the full month.
    r1 = op_find_files(
        tmp_path,
        date_start=date(2026, 5, 6),
        date_end=date(2026, 5, 6),
        date_field="name_date",
    )
    assert r1["files"] == []

    # Range that covers full month matches.
    r2 = op_find_files(
        tmp_path,
        date_start=date(2026, 5, 1),
        date_end=date(2026, 5, 31),
        date_field="name_date",
    )
    assert len(r2["files"]) == 1


def test_name_glob_filter(tmp_path: Path):
    silo_root = str(tmp_path / "j")
    files = {
        f"{silo_root}/journal/2026-05-06.md": {"mtime": 1000.0, "size": 10, "hash": "a"},
        f"{silo_root}/notes/random.md": {"mtime": 1000.0, "size": 10, "hash": "b"},
    }
    # Create the silo root so relpath works
    Path(silo_root).mkdir(parents=True, exist_ok=True)
    _write_manifest(tmp_path, {"j": _silo(silo_root, files)})

    result = op_find_files(tmp_path, name_glob="journal/*")
    paths = [f["path"] for f in result["files"]]
    assert paths == [f"{silo_root}/journal/2026-05-06.md"]


def test_silos_filter_skips_others(tmp_path: Path):
    silo_a = str(tmp_path / "a")
    silo_b = str(tmp_path / "b")
    _write_manifest(
        tmp_path,
        {
            "a": _silo(silo_a, {f"{silo_a}/2026-05-06.md": {"mtime": 1.0, "size": 1, "hash": "x"}}),
            "b": _silo(silo_b, {f"{silo_b}/2026-05-06.md": {"mtime": 1.0, "size": 1, "hash": "y"}}),
        },
    )

    result = op_find_files(tmp_path, silos=["a"])
    paths = [f["path"] for f in result["files"]]
    assert paths == [f"{silo_a}/2026-05-06.md"]


def test_unknown_silo_emits_warning(tmp_path: Path):
    _write_manifest(tmp_path, {})
    result = op_find_files(tmp_path, silos=["does_not_exist"])
    assert result["files"] == []
    assert any("does_not_exist" in w for w in result["warnings"])


def test_limit_truncates_and_flags(tmp_path: Path):
    silo_root = str(tmp_path / "j")
    files = {
        f"{silo_root}/2026-05-{d:02d}.md": {"mtime": 1.0, "size": 1, "hash": str(d)}
        for d in range(1, 11)
    }
    _write_manifest(tmp_path, {"j": _silo(silo_root, files)})

    result = op_find_files(tmp_path, limit=3)
    assert len(result["files"]) == 3
    assert result["total_matched"] == 10
    assert result["truncated"] is True


def test_empty_range_returns_warning(tmp_path: Path):
    _write_manifest(tmp_path, {})
    result = op_find_files(
        tmp_path, date_start=date(2026, 5, 10), date_end=date(2026, 5, 1)
    )
    assert result["files"] == []
    assert any("empty range" in w for w in result["warnings"])


def test_does_not_open_chroma_when_chunk_count_disabled(tmp_path: Path, monkeypatch):
    silo_root = str(tmp_path / "j")
    files = {
        f"{silo_root}/2026-05-06.md": {"mtime": 1.0, "size": 1, "hash": "a"},
    }
    _write_manifest(tmp_path, {"j": _silo(silo_root, files)})

    # If anyone tries to import chroma_client, blow up.
    sentinel = {"called": False}

    def boom(*a, **k):
        sentinel["called"] = True
        raise AssertionError("chroma_client.get_client should not be called")

    monkeypatch.setattr("chroma_client.get_client", boom)
    op_find_files(tmp_path, include_chunk_count=False)
    assert sentinel["called"] is False


def test_no_match_with_strict_name_date_when_no_filename_date_present(tmp_path: Path):
    silo_root = str(tmp_path / "j")
    import datetime as _dt
    mtime = _dt.datetime(2026, 5, 6, 12, 0, 0).timestamp()
    files = {
        f"{silo_root}/notes.md": {"mtime": mtime, "size": 1, "hash": "a"},
    }
    _write_manifest(tmp_path, {"j": _silo(silo_root, files)})

    result = op_find_files(
        tmp_path,
        date_start=date(2026, 5, 6),
        date_end=date(2026, 5, 6),
        date_field="name_date",
    )
    assert result["files"] == []
