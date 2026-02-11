from pathlib import Path

from query.catalog import (
    parse_file_list_year_request,
    validate_catalog_freshness,
    list_files_from_year,
)
from state import update_silo
from file_registry import _write_file_manifest


def test_parse_file_list_year_request_happy_path():
    req = parse_file_list_year_request("what files are from 2022'")
    assert req == {"year": "2022"}


def test_parse_file_list_year_request_skips_summary():
    req = parse_file_list_year_request("summary of architecture files from 2022")
    assert req is None


def test_list_files_from_year_uses_mtime_and_sorts_dedup(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "stuff"
    root.mkdir(parents=True, exist_ok=True)
    a = root / "b.txt"
    b = root / "a.txt"
    a.write_text("x", encoding="utf-8")
    b.write_text("y", encoding="utf-8")
    m_2022 = 1661990400.0
    m_2023 = 1693526400.0
    manifest = {
        "silos": {
            "stuff-12345678": {
                "path": str(root.resolve()),
                "files": {
                    str(a.resolve()): {"mtime": m_2022, "size": 1, "hash": "h1"},
                    str(b.resolve()): {"mtime": m_2022, "size": 1, "hash": "h2"},
                    str((root / "c.txt").resolve()): {"mtime": m_2023, "size": 1, "hash": "h3"},
                },
            }
        }
    }
    _write_file_manifest(db, manifest)
    out = list_files_from_year(str(db), "stuff-12345678", 2022, cap=50)
    assert out["matched_count"] == 2
    assert out["files"] == sorted(out["files"])


def test_validate_catalog_freshness_detects_stale_change(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "stuff"
    root.mkdir(parents=True, exist_ok=True)
    f = root / "x.txt"
    f.write_text("old", encoding="utf-8")
    st = f.stat()
    update_silo(
        str(db),
        "stuff-12345678",
        str(root.resolve()),
        files_indexed=1,
        chunks_count=1,
        updated_iso="2026-02-11T00:00:00+00:00",
        display_name="Stuff",
    )
    _write_file_manifest(
        db,
        {
            "silos": {
                "stuff-12345678": {
                    "path": str(root.resolve()),
                    "files": {str(f.resolve()): {"mtime": st.st_mtime, "size": st.st_size, "hash": "h1"}},
                }
            }
        },
    )
    f.write_text("new content", encoding="utf-8")
    fresh = validate_catalog_freshness(str(db), "stuff-12345678")
    assert fresh["stale"] is True
    assert fresh["stale_reason"] == "file_changed_since_index"


def test_catalog_completeness_not_topk_limited(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "stuff"
    root.mkdir(parents=True, exist_ok=True)
    files_map = {}
    year_2022 = 1651363200.0
    for i in range(200):
        p = root / f"doc-{i:03d}.txt"
        p.write_text("x", encoding="utf-8")
        files_map[str(p.resolve())] = {"mtime": year_2022, "size": 1, "hash": f"h{i}"}
    _write_file_manifest(
        db,
        {"silos": {"stuff-12345678": {"path": str(root.resolve()), "files": files_map}}},
    )
    out = list_files_from_year(str(db), "stuff-12345678", 2022, cap=50)
    assert out["matched_count"] == 200
    assert len(out["files"]) == 50
    assert out["cap_applied"] is True
