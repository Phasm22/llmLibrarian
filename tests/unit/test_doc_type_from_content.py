"""Tests for content-based doc_type heuristics (markdown vs PDF tax cues)."""

from ingest import _doc_type_from_content


def test_w2_example_prose_markdown_is_not_tax_return():
    # Mirrors README-style documentation that cites W-2 in example queries.
    sample = 'pal ask in <silo> "W-2 employer name"'
    assert _doc_type_from_content(sample, ".md") == "other"


def test_w2_cue_with_pdf_suffix_still_tax_return():
    sample = "Box 1 wages 50000 employer ACME (W-2)"
    assert _doc_type_from_content(sample, ".pdf") == "tax_return"


def test_w2_cue_without_path_suffix_still_tax_return():
    sample = "W-2 Copy B federal wages"
    assert _doc_type_from_content(sample, "") == "tax_return"


def test_readme_style_irs_citations_on_markdown_not_tax_return():
    sample = (
        "See IRS Pub 17 for adjusted gross income examples. "
        "This repo documents Form 1040-related queries for testing only."
    )
    assert _doc_type_from_content(sample, ".md") == "other"


def test_agents_md_style_preface_not_tax_return():
    sample = (
        "# AGENTS\n\nPrimary source of truth. Use retrieve with doc_type filters; "
        "W-2 and 1099 fields come from tax_ledger when present."
    )
    assert _doc_type_from_content(sample, ".md") == "other"
