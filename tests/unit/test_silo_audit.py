from pathlib import Path

from silo_audit import (
    find_duplicate_hashes,
    find_orphaned_sources,
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
    assert "All clean." in report


def test_find_orphaned_sources_flags_missing_path(tmp_path):
    gone = str(tmp_path / "gone")  # never created
    reg = [{"slug": "missing-silo", "path": gone}]
    orphans = find_orphaned_sources(reg)
    assert len(orphans) == 1
    assert orphans[0]["slug"] == "missing-silo"
    assert orphans[0]["path"] == gone


def test_find_orphaned_sources_ignores_existing_path(tmp_path):
    existing = tmp_path / "real"
    existing.mkdir()
    reg = [{"slug": "live-silo", "path": str(existing)}]
    orphans = find_orphaned_sources(reg)
    assert orphans == []


def test_find_orphaned_sources_mixed(tmp_path):
    existing = tmp_path / "real"
    existing.mkdir()
    gone = str(tmp_path / "gone")
    reg = [
        {"slug": "live-silo", "path": str(existing)},
        {"slug": "dead-silo", "path": gone},
    ]
    orphans = find_orphaned_sources(reg)
    assert len(orphans) == 1
    assert orphans[0]["slug"] == "dead-silo"


def test_format_report_includes_orphans(tmp_path):
    orphans = [{"slug": "dead-silo", "path": "/gone/path"}]
    report = format_report([], [], [], [], orphans=orphans)
    assert "Orphaned sources" in report
    assert "dead-silo" in report
    assert "llmli rm" in report
    assert "All clean." not in report


def test_format_report_orphans_count_in_header(tmp_path):
    orphans = [{"slug": "x", "path": "/x"}]
    report = format_report([], [], [], [], orphans=orphans)
    assert "Orphans: 1" in report
