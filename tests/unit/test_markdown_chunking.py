"""Markdown single-chunk threshold and heading-aware splits."""
from ingest import _chunks_from_content, chunk_text
from ingest.chunking import chunk_markdown


def test_small_markdown_is_single_chunk():
    body = "# Soup\n\n" + ("Stir. " * 40)
    chunks = chunk_markdown(body, size=200, overlap=20, single_chunk_max=4000)
    assert len(chunks) == 1
    assert chunks[0][1] == 1
    assert chunks[0][0].startswith("# Soup")


def test_large_markdown_splits_on_headings_before_size():
    # Two sections, each under size, total over single_chunk_max.
    section_a = "# Ingredients\n\n" + ("flour\n" * 30)
    section_b = "# Steps\n\n" + ("mix\n" * 30)
    text = section_a + "\n" + section_b
    assert len(text) > 200
    chunks = chunk_markdown(text, size=2000, overlap=50, single_chunk_max=200)
    assert len(chunks) >= 2
    assert any(c.startswith("# Ingredients") for c, _ in chunks)
    assert any(c.startswith("# Steps") for c, _ in chunks)
    # Heading boundaries preserved — no mid-list glue across sections.
    assert all("# Ingredients" not in c or "# Steps" not in c for c, _ in chunks)


def test_oversized_heading_section_falls_back_to_size_split():
    text = "# Big\n\n" + ("x" * 500) + "\n"
    chunks = chunk_markdown(text, size=120, overlap=20, single_chunk_max=50)
    assert len(chunks) > 1
    assert all(len(c) <= 120 for c, _ in chunks)


def test_non_markdown_still_uses_size_chunker():
    text = "\n".join(f"line {i}" for i in range(50))
    chunks = chunk_text(text, size=40, overlap=8)
    assert len(chunks) > 1


def test_chunks_from_content_attaches_frontmatter_metadata():
    md = """---
title: Chili
category: dinner
main_protein: beef
servings: 6
total_time: 120
tags: [winter, pot]
---

# Chili

Brown the beef. Simmer.
"""
    chunks = _chunks_from_content(
        file_id="chili.md",
        text=md,
        source_path="/recipes/chili.md",
        mtime=1.0,
    )
    assert len(chunks) == 1
    _cid, doc, meta = chunks[0]
    assert meta["title"] == "Chili"
    assert meta["category"] == "dinner"
    assert meta["main_protein"] == "beef"
    assert meta["servings"] == 6
    assert meta["total_time"] == 120
    assert meta["tags"] == ",winter,pot,"
    assert "---" not in doc
    assert meta["chunk_index"] == 0
    assert "title: Chili" not in doc
    assert "Chili" in doc
    assert "Brown the beef" in doc


def test_malformed_frontmatter_still_indexes_body():
    md = "---\n[[[not yaml\n---\n# Ok\n\nBody text.\n"
    chunks = _chunks_from_content(
        file_id="ok.md",
        text=md,
        source_path="/recipes/ok.md",
        mtime=1.0,
    )
    assert chunks
    _cid, doc, meta = chunks[0]
    assert "title" not in meta
    assert "Body text" in doc


def test_new_ingests_number_chunks_in_order():
    from ingest import _chunks_from_csv_text, _chunks_from_image_result, _chunks_from_pdf_pages
    from processors import ExtractedImage, ExtractedPage, ImageRegion

    text = _chunks_from_content("f", "alpha\n\n" + ("word " * 400), "/n.md", 1.0)
    assert [m["chunk_index"] for _c, _d, m in text] == list(range(len(text)))
    assert all(isinstance(m["chunk_index"], int) for _c, _d, m in text)

    csv_rows = _chunks_from_csv_text("f", "a,b\n1,2\n3,4\n", "/t.csv", 1.0)
    assert [m["chunk_index"] for _c, _d, m in csv_rows] == [0, 1]

    pages = _chunks_from_pdf_pages(
        "f",
        [ExtractedPage("page one", 1), ExtractedPage("page two", 2)],
        "/t.pdf",
        1.0,
    )
    assert [m["chunk_index"] for _c, _d, m in pages] == [0, 1]

    image = _chunks_from_image_result(
        "f",
        ExtractedImage(
            summary="a dog",
            visible_text="",
            regions=(ImageRegion("the dog", "subject", 0, 0, 1, 1),),
        ),
        "/p.jpg",
        1.0,
    )
    assert [m["chunk_index"] for _c, _d, m in image] == [0, 1]
