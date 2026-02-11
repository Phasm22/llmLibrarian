from pathlib import Path

from file_registry import _write_file_manifest
from query.scope_binding import (
    bind_scope_from_query,
    detect_filetype_hints,
    rank_silos_by_catalog_tokens,
    strip_scope_phrase,
)
from state import update_silo


def _seed_silo(db: Path, slug: str, display: str, root: Path) -> None:
    update_silo(
        str(db),
        slug,
        str(root.resolve()),
        files_indexed=1,
        chunks_count=1,
        updated_iso="2026-02-11T00:00:00+00:00",
        display_name=display,
    )


def test_bind_scope_from_query_exact_match(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir()
    root = tmp_path / "stuff"
    root.mkdir()
    _seed_silo(db, "stuff-deadbeef", "Stuff", root)
    out = bind_scope_from_query("main idea in stuff", str(db))
    assert out["bound_slug"] == "stuff-deadbeef"
    assert out["confidence"] == 1.0


def test_bind_scope_from_query_normalized_match(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir()
    root = tmp_path / "market-man"
    root.mkdir()
    _seed_silo(db, "marketman-f06d360e", "Market Man", root)
    out = bind_scope_from_query("summary within my market-man", str(db))
    assert out["bound_slug"] == "marketman-f06d360e"
    assert out["confidence"] == 0.9


def test_bind_scope_from_query_unique_prefix(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir()
    root = tmp_path / "stuff"
    root.mkdir()
    _seed_silo(db, "stuff-deadbeef", "Stuff", root)
    out = bind_scope_from_query("main idea from stuf", str(db))
    assert out["bound_slug"] == "stuff-deadbeef"
    assert out["confidence"] == 0.8


def test_bind_scope_from_query_ambiguous_prefix_returns_none(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir()
    r1 = tmp_path / "stuff"
    r2 = tmp_path / "stuffplus"
    r1.mkdir()
    r2.mkdir()
    _seed_silo(db, "stuff-deadbeef", "Stuff", r1)
    _seed_silo(db, "stuffplus-cafebabe", "Stuff Plus", r2)
    out = bind_scope_from_query("main idea in stuf", str(db))
    assert out["bound_slug"] is None
    assert out["confidence"] == 0.0


def test_strip_scope_phrase_variants():
    assert strip_scope_phrase("main idea of apt simulation in my stuff") == "main idea of apt simulation"
    assert strip_scope_phrase("summary within stuff") == "summary"
    assert strip_scope_phrase("plan from stuff for class") == "plan"


def test_detect_filetype_hints_powerpoint():
    hints = detect_filetype_hints("main idea of this powerpoint slides deck")
    assert ".pptx" in hints["extensions"]
    assert ".ppt" in hints["extensions"]


def test_rank_silos_by_catalog_tokens_prefers_extension_and_name(tmp_path: Path):
    db = tmp_path / "db"
    db.mkdir()
    s1 = tmp_path / "stuff"
    s2 = tmp_path / "other"
    s1.mkdir()
    s2.mkdir()
    _seed_silo(db, "stuff-deadbeef", "Stuff", s1)
    _seed_silo(db, "other-cafebabe", "Other", s2)
    (s1 / "APT Simulation and Analysis.pptx").write_text("x", encoding="utf-8")
    (s2 / "random.docx").write_text("x", encoding="utf-8")
    _write_file_manifest(
        db,
        {
            "silos": {
                "stuff-deadbeef": {
                    "path": str(s1.resolve()),
                    "files": {str((s1 / "APT Simulation and Analysis.pptx").resolve()): {"mtime": 1, "size": 1, "hash": "h1"}},
                },
                "other-cafebabe": {
                    "path": str(s2.resolve()),
                    "files": {str((s2 / "random.docx").resolve()): {"mtime": 1, "size": 1, "hash": "h2"}},
                },
            }
        },
    )
    ranked = rank_silos_by_catalog_tokens(
        "main idea of apt simulation powerpoint in my stuff",
        str(db),
        {"extensions": [".pptx", ".ppt"], "reason": "powerpoint_terms"},
    )
    assert ranked
    assert ranked[0]["slug"] == "stuff-deadbeef"
