from pathlib import Path

from file_registry import _write_file_manifest
from query.catalog import (
    parse_structure_request,
    build_structure_outline,
    build_structure_recent,
    build_structure_inventory,
    build_structure_extension_count,
    rank_scope_candidates,
)
from state import update_silo


def test_parse_structure_request_modes():
    assert parse_structure_request("show structure snapshot") == {
        "mode": "outline",
        "wants_summary": False,
        "ext": None,
    }
    assert parse_structure_request("recent changes in this folder") == {
        "mode": "recent",
        "wants_summary": False,
        "ext": None,
    }
    assert parse_structure_request("summarize file type inventory") == {
        "mode": "inventory",
        "wants_summary": True,
        "ext": None,
    }
    assert parse_structure_request("how many .docx files are there") == {
        "mode": "ext_count",
        "wants_summary": False,
        "ext": ".docx",
    }


def test_build_structure_outline_collapses_duplicate_hashes(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "stuff"
    root.mkdir(parents=True, exist_ok=True)
    p1 = root / "A" / "Deck.pptx"
    p2 = root / "B" / "Deck copy.pptx"
    p1.parent.mkdir(parents=True, exist_ok=True)
    p2.parent.mkdir(parents=True, exist_ok=True)
    p1.write_text("x", encoding="utf-8")
    p2.write_text("x", encoding="utf-8")
    update_silo(
        str(db),
        "stuff-deadbeef",
        str(root.resolve()),
        files_indexed=2,
        chunks_count=2,
        updated_iso="2026-02-13T00:00:00+00:00",
        display_name="Stuff",
    )
    _write_file_manifest(
        db,
        {
            "silos": {
                "stuff-deadbeef": {
                    "path": str(root.resolve()),
                    "files": {
                        str(p1.resolve()): {"mtime": 1700000000.0, "size": 1, "hash": "samehash"},
                        str(p2.resolve()): {"mtime": 1700000001.0, "size": 1, "hash": "samehash"},
                    },
                }
            }
        },
    )
    out = build_structure_outline(str(db), "stuff-deadbeef", cap=200)
    assert out["mode"] == "outline"
    assert out["matched_count"] == 1
    assert out["lines"] == ["A/Deck.pptx (2 copies)"]


def test_build_structure_recent_formats_dates_and_sorts(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "stuff"
    root.mkdir(parents=True, exist_ok=True)
    a = root / "old.txt"
    b = root / "new.txt"
    a.write_text("a", encoding="utf-8")
    b.write_text("b", encoding="utf-8")
    update_silo(
        str(db),
        "stuff-deadbeef",
        str(root.resolve()),
        files_indexed=2,
        chunks_count=2,
        updated_iso="2026-02-13T00:00:00+00:00",
        display_name="Stuff",
    )
    _write_file_manifest(
        db,
        {
            "silos": {
                "stuff-deadbeef": {
                    "path": str(root.resolve()),
                    "files": {
                        str(a.resolve()): {"mtime": 1609459200.0, "size": 1, "hash": "h1"},  # 2021-01-01
                        str(b.resolve()): {"mtime": 1672531200.0, "size": 1, "hash": "h2"},  # 2023-01-01
                    },
                }
            }
        },
    )
    out = build_structure_recent(str(db), "stuff-deadbeef", cap=100)
    assert out["mode"] == "recent"
    assert out["lines"] == [
        "2023-01-01 new.txt",
        "2021-01-01 old.txt",
    ]


def test_build_structure_inventory_counts_by_unique_file_group(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "stuff"
    root.mkdir(parents=True, exist_ok=True)
    p1 = root / "a.py"
    p2 = root / "b.py"
    p3 = root / "b-copy.py"
    p4 = root / "deck.pptx"
    for p in (p1, p2, p3, p4):
        p.write_text("x", encoding="utf-8")
    update_silo(
        str(db),
        "stuff-deadbeef",
        str(root.resolve()),
        files_indexed=4,
        chunks_count=4,
        updated_iso="2026-02-13T00:00:00+00:00",
        display_name="Stuff",
    )
    _write_file_manifest(
        db,
        {
            "silos": {
                "stuff-deadbeef": {
                    "path": str(root.resolve()),
                    "files": {
                        str(p1.resolve()): {"mtime": 1672531200.0, "size": 1, "hash": "h1"},
                        str(p2.resolve()): {"mtime": 1672531200.0, "size": 1, "hash": "h2"},
                        str(p3.resolve()): {"mtime": 1672531200.0, "size": 1, "hash": "h2"},
                        str(p4.resolve()): {"mtime": 1672531200.0, "size": 1, "hash": "h3"},
                    },
                }
            }
        },
    )
    out = build_structure_inventory(str(db), "stuff-deadbeef", cap=200)
    assert out["mode"] == "inventory"
    assert out["lines"] == [".py 2", ".pptx 1"]


def test_build_structure_extension_count_is_deterministic(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "stuff"
    root.mkdir(parents=True, exist_ok=True)
    a = root / "a.docx"
    b = root / "b.docx"
    c = root / "copy.docx"
    d = root / "deck.pptx"
    for p in (a, b, c, d):
        p.write_text("x", encoding="utf-8")
    update_silo(
        str(db),
        "stuff-deadbeef",
        str(root.resolve()),
        files_indexed=4,
        chunks_count=4,
        updated_iso="2026-02-13T00:00:00+00:00",
        display_name="Stuff",
    )
    _write_file_manifest(
        db,
        {
            "silos": {
                "stuff-deadbeef": {
                    "path": str(root.resolve()),
                    "files": {
                        str(a.resolve()): {"mtime": 1672531200.0, "size": 1, "hash": "h1"},
                        str(b.resolve()): {"mtime": 1672531200.0, "size": 1, "hash": "h2"},
                        str(c.resolve()): {"mtime": 1672531200.0, "size": 1, "hash": "h2"},
                        str(d.resolve()): {"mtime": 1672531200.0, "size": 1, "hash": "h3"},
                    },
                }
            }
        },
    )
    out = build_structure_extension_count(str(db), "stuff-deadbeef", ".docx")
    assert out["mode"] == "ext_count"
    assert out["ext"] == ".docx"
    assert out["count"] == 2


def test_rank_scope_candidates_returns_top_three(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir(parents=True, exist_ok=True)
    silos = [
        ("stuff-deadbeef", "Stuff", "apt simulation and analysis.pptx"),
        ("marketman-f06d360e", "MarketMan", "architecture.md"),
        ("tax-12345678", "Tax", "2022_TaxReturn.pdf"),
        ("school-12345678", "School", "cs3300 project notes.md"),
    ]
    manifest_silos: dict[str, dict] = {}
    for idx, (slug, display, fname) in enumerate(silos):
        root = tmp_path / slug
        root.mkdir(parents=True, exist_ok=True)
        p = root / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
        update_silo(
            str(db),
            slug,
            str(root.resolve()),
            files_indexed=1,
            chunks_count=1,
            updated_iso="2026-02-13T00:00:00+00:00",
            display_name=display,
        )
        manifest_silos[slug] = {
            "path": str(root.resolve()),
            "files": {
                str(p.resolve()): {"mtime": 1672531200.0 + idx, "size": 1, "hash": f"h{idx}"},
            },
        }
    _write_file_manifest(db, {"silos": manifest_silos})

    out = rank_scope_candidates("show structure for apt simulation powerpoint", str(db), top_n=3)
    assert len(out) == 3
    assert out[0]["slug"] == "stuff-deadbeef"
