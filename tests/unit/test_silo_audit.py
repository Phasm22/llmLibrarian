from pathlib import Path

from silo_audit import (
    find_duplicate_hashes,
    find_path_overlaps,
    find_count_mismatches,
    format_report,
)


def test_find_duplicate_hashes():
    file_registry = {
        "by_hash": {
            "h1": [{"silo": "a", "path": "/x/a.txt"}, {"silo": "b", "path": "/y/a.txt"}],
            "h2": [{"silo": "a", "path": "/x/b.txt"}],
        }
    }
    dupes = find_duplicate_hashes(file_registry)
    assert len(dupes) == 1
    assert dupes[0]["hash"] == "h1"
    assert dupes[0]["silos"] == ["a", "b"]


def test_find_path_overlaps(tmp_path):
    p1 = tmp_path / "root"
    p2 = p1 / "child"
    p1.mkdir()
    p2.mkdir()
    reg = [
        {"slug": "root", "path": str(p1)},
        {"slug": "child", "path": str(p2)},
    ]
    overlaps = find_path_overlaps(reg)
    assert overlaps
    assert overlaps[0]["parent"] == "root"
    assert overlaps[0]["child"] == "child"


def test_find_count_mismatches():
    registry = [{"slug": "a", "files_indexed": 2}]
    manifest = {"silos": {"a": {"files": {"x": {}, "y": {}, "z": {}}}}}
    mismatches = find_count_mismatches(registry, manifest)
    assert mismatches
    assert mismatches[0]["registry_files"] == 2
    assert mismatches[0]["manifest_files"] == 3


def test_format_report_empty():
    report = format_report([], [], [], [])
    assert "No issues detected." in report
