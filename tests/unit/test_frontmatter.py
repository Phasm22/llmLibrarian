"""YAML frontmatter parsing and Chroma-safe flattening."""
from frontmatter import (
    chroma_metadata_from_frontmatter,
    compact_frontmatter_preamble,
    frontmatter_fields_for_response,
    list_values_from_meta,
    parse_markdown_frontmatter,
)


SAMPLE = """---
id: roast-chicken
title: Roast Chicken
category: dinner
course: main
servings: 4
prep_time: 20
cook_time: 70
total_time: 90
main_protein: chicken
dietary_flags:
  - gluten-free
tags: [comfort, sunday]
era: family
confidence: high
needs_review: false
source_file: grandma.pdf
source_pages: [12, 13]
nested:
  skip: me
---

# Roast Chicken

1. Salt the bird.
2. Roast until done.
"""


def test_parse_frontmatter_extracts_mapping_and_body():
    raw, body = parse_markdown_frontmatter(SAMPLE)
    assert raw["title"] == "Roast Chicken"
    assert raw["servings"] == 4
    assert "Salt the bird" in body
    assert not body.lstrip().startswith("---")
    assert "id: roast-chicken" not in body


def test_malformed_frontmatter_does_not_break():
    bad = "---\n: this is not yaml: [[{\n---\n# Still a recipe\n"
    raw, body = parse_markdown_frontmatter(bad)
    assert raw == {}
    assert body == bad


def test_missing_frontmatter_returns_original():
    text = "# Just a note\n\nHello.\n"
    raw, body = parse_markdown_frontmatter(text)
    assert raw == {}
    assert body == text


def test_chroma_metadata_flattens_lists_and_skips_nested():
    raw, _ = parse_markdown_frontmatter(SAMPLE)
    meta = chroma_metadata_from_frontmatter(raw)
    assert meta["title"] == "Roast Chicken"
    assert meta["servings"] == 4
    assert meta["total_time"] == 90
    assert meta["needs_review"] is False
    assert meta["tags"] == ",comfort,sunday,"
    assert meta["dietary_flags"] == ",gluten-free,"
    assert meta["source_pages"] == ",12,13,"
    assert "nested" not in meta


def test_reserved_keys_are_not_overwritten_from_frontmatter():
    raw = {"title": "x", "source": "/evil", "silo": "leak", "doc_type": "tax_return"}
    meta = chroma_metadata_from_frontmatter(raw)
    assert meta == {"title": "x"}


def test_compact_preamble_is_short_and_useful():
    raw, _ = parse_markdown_frontmatter(SAMPLE)
    meta = chroma_metadata_from_frontmatter(raw)
    line = compact_frontmatter_preamble(meta)
    assert "Roast Chicken" in line
    assert "main_protein=chicken" in line
    assert "servings=4" in line
    assert "---" not in line
    assert len(line) < 400


def test_list_values_and_response_projection():
    assert list_values_from_meta(",comfort,sunday,") == ["comfort", "sunday"]
    assert list_values_from_meta(["A", "b"]) == ["a", "b"]
    projected = frontmatter_fields_for_response(
        {"title": "T", "tags": ",a,b,", "source": "/x", "chunk_hash": "h"}
    )
    assert projected == {"title": "T", "tags": ["a", "b"]}
