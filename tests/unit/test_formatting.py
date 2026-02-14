from pathlib import Path

from query.formatting import (
    shorten_path,
    source_url,
    sanitize_answer_metadata_artifacts,
    normalize_answer_direct_address,
    normalize_uncertainty_tone,
    normalize_ownership_claims,
    normalize_sentence_start,
    normalize_inline_numbered_lists,
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


def test_sanitize_answer_metadata_artifacts_rewrites_file_only_token():
    raw = 'See file=GE04 Portfolio Django MVT .docx for details.'
    out = sanitize_answer_metadata_artifacts(raw)
    assert "file=" not in out
    assert "GE04 Portfolio Django MVT .docx" in out


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


def test_normalize_uncertainty_tone_reduces_hedge_loops_when_banner_present():
    raw = (
        "Based on the provided context, it appears that you worked on parser tooling. "
        "It appears that you also adjusted tests. "
        "Without more information, it is difficult to determine exact ownership."
    )
    out = normalize_uncertainty_tone(raw, has_confidence_banner=True, strict=False)
    assert not out.lower().startswith("based on the provided context")
    assert out.lower().count("it appears that") <= 1
    assert out.lower().count("without more information") <= 1
    assert "Caveat:" in out


def test_normalize_uncertainty_tone_no_change_when_strict():
    raw = "Based on the provided context, it appears that evidence is limited."
    out = normalize_uncertainty_tone(raw, has_confidence_banner=True, strict=True)
    assert out == raw


def test_normalize_ownership_claims_rewrites_obvious_conflicts():
    raw = "This appears authored by you, suggesting they were written by someone else."
    out = normalize_ownership_claims(raw)
    assert "written by someone else" not in out.lower()
    assert "ownership is uncertain" in out.lower()


def test_normalize_sentence_start_capitalizes_first_letter():
    assert normalize_sentence_start("here is your answer.") == "Here is your answer."


def test_normalize_inline_numbered_lists_reflows_single_line_lists():
    raw = "Findings: 1. alpha 2. beta 3. gamma"
    out = normalize_inline_numbered_lists(raw)
    assert "1. alpha" in out
    assert "\n2. beta" in out
    assert "\n3. gamma" in out
