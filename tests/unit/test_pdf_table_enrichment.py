from processors import (
    _extract_line_value_hints,
    _merge_pdf_page_content,
    _normalize_table_rows,
    _table_to_markdown,
)


def test_normalize_table_rows_drops_empty_rows_and_trailing_cells():
    rows = [
        [None, "  9  ", " 7,522. ", "", None],
        ["", "", ""],
        [" line 11 ", " 7,522. "],
    ]
    out = _normalize_table_rows(rows)
    assert out == [["9", "7,522."], ["line 11", "7,522."]]


def test_table_to_markdown_preserves_column_alignment():
    rows = [["line", "value"], ["9", "7,522."], ["11", "7,522."]]
    md = _table_to_markdown(rows)
    assert "| line | value |" in md
    assert "| --- | --- |" in md
    assert "| 9 | 7,522. |" in md
    assert "| 11 | 7,522. |" in md


def test_extract_line_value_hints_from_sparse_row_shape():
    rows = [["", "9", "7,522."], ["line 11", "7,522."], ["x", "y", "z"]]
    hints = _extract_line_value_hints(rows)
    assert "line 9: 7,522." in hints
    assert "line 11: 7,522." in hints


def test_merge_pdf_page_content_orders_hints_then_table_then_raw():
    merged = _merge_pdf_page_content(
        raw_text="RAW BODY",
        table_md="| line | value |\n| --- | --- |\n| 9 | 7,522. |",
        hints=["line 9: 7,522."],
    )
    assert merged.index("Structured values:") < merged.index("Extracted tables:")
    assert merged.index("Extracted tables:") < merged.index("RAW BODY")
    assert "line 9: 7,522." in merged
