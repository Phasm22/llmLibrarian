"""list_silos doc_type_breakdown uses the same taxonomy as ingest chunk metadata."""

from operations import _doc_type_breakdown


def test_doc_type_breakdown_matches_extension_taxonomy():
    got = _doc_type_breakdown({".py": 3, ".PDF": 2, ".md": 4, ".tsx": 1})
    assert got["code"] == 4  # .py + .tsx
    assert got["pdf"] == 2
    assert got["other"] == 4


def test_doc_type_breakdown_empty():
    assert _doc_type_breakdown({}) == {}
    assert _doc_type_breakdown(None) == {}
