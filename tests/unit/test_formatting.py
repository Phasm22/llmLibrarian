from pathlib import Path

from query.formatting import (
    shorten_path,
    source_url,
    sanitize_answer_metadata_artifacts,
    normalize_answer_direct_address,
)


def test_shorten_path_root_fallback():
    assert shorten_path("/") == "/"


def test_source_url_encodes_spaces(tmp_path):
    p = tmp_path / "file with space.txt"
    p.write_text("x", encoding="utf-8")
    url = source_url(str(p))
    assert "%20" in url
    assert " " not in url


def test_source_url_defaults_to_file_scheme(tmp_path, monkeypatch):
    monkeypatch.delenv("LLMLIBRARIAN_EDITOR_SCHEME", raising=False)
    p = tmp_path / "file.txt"
    p.write_text("x", encoding="utf-8")
    url = source_url(str(p), line=7)
    assert url.startswith("file://")


def test_sanitize_answer_metadata_artifacts_rewrites_internal_header_tokens():
    raw = (
        'Found in "file=TD-resume.docx (line 24) mtime=2025-08-12 '
        'silo=job-related-stuff-35edf4b2 doc_type=other".'
    )
    out = sanitize_answer_metadata_artifacts(raw)
    assert "file=" not in out
    assert "mtime=" not in out
    assert "silo=" not in out
    assert "doc_type=" not in out
    assert "TD-resume.docx (line 24)" in out


def test_normalize_answer_direct_address_rewrites_common_third_person_terms():
    raw = (
        "A patient named Tandon Jenkins had normal levels. "
        "The patient's visit was brief. The patient had normal levels. "
        "This patient should retest. The user requested follow-up."
    )
    out = normalize_answer_direct_address(raw)
    assert "The patient" not in out
    assert "The patient's" not in out
    assert "A patient named" not in out
    assert "This patient" not in out
    assert "The user" not in out
    assert "you's" not in out
    assert "You had normal levels." in out
    assert "Your visit was brief." in out
    assert "You should retest." in out
    assert "You requested follow-up." in out
