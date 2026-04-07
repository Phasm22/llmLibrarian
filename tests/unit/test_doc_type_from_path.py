"""Regression: path-based doc_type aligns with list_silos / doc_type filters."""

from ingest import _doc_type_from_path


def test_doc_type_path_code_extension():
    assert _doc_type_from_path("/proj/src/foo.py") == "code"
    assert _doc_type_from_path("notes.RS") == "code"


def test_doc_type_path_structured_docs():
    assert _doc_type_from_path("/a/x.pdf") == "pdf"
    assert _doc_type_from_path("/a/x.DOCX") == "docx"
    assert _doc_type_from_path("/a/x.xls") == "xlsx"
    assert _doc_type_from_path("/slides/a.pptx") == "pptx"


def test_doc_type_path_keyword_beats_extension():
    assert _doc_type_from_path("/courses/transcript_backup.pdf") == "transcript"
