from datetime import datetime, timezone

import style
from query.context import (
    context_block,
    query_implies_recency,
    query_mentioned_years,
    recency_score,
)
from query.formatting import format_source, shorten_path, snippet_preview, style_answer


def test_recency_score_prefers_newer_mtime():
    now = datetime.now(timezone.utc).timestamp()
    recent = recency_score(now - 86400)  # 1 day old
    old = recency_score(now - (86400 * 365 * 3))  # 3 years old
    assert recent > old
    assert recent > 0


def test_recency_score_handles_invalid_mtime():
    assert recency_score(0) == 0.0
    assert recency_score(-1) == 0.0


def test_query_implies_recency_for_latest_and_year():
    assert query_implies_recency("what is the latest version?")
    assert query_implies_recency("show docs from 2024")
    assert not query_implies_recency("explain recursion")


def test_query_mentioned_years_dedupes_in_order():
    years = query_mentioned_years("compare 2024 and 2023 then 2024 again")
    assert years == ["2024", "2023"]


def test_context_block_contains_file_header_fields():
    block = context_block(
        doc="hello world",
        meta={
            "source": "/tmp/file.txt",
            "line_start": 42,
            "mtime": 1700000000,
            "silo": "stuff",
            "doc_type": "other",
        },
        show_silo=True,
    )
    assert "[silo: stuff]" in block
    assert "file=" in block
    assert "(line 42)" in block
    assert "doc_type=other" in block
    assert "hello world" in block


def test_style_answer_returns_original_when_no_color():
    text = "**bold** and `code`"
    assert style_answer(text, no_color=True) == text


def test_style_answer_applies_markup_when_color_enabled(monkeypatch):
    monkeypatch.setattr(style, "use_color", lambda _no_color=False: True)
    out = style_answer("**bold** and `code`", no_color=False)
    assert "**" not in out
    assert "`code`" not in out
    assert "\x1b[" in out


def test_format_source_includes_location_and_score():
    out = format_source(
        doc="line1\nline2",
        meta={"source": "/tmp/test.txt", "line_start": 7},
        distance=1.0,
        no_color=True,
    )
    assert "test.txt" in out or "/tmp/test.txt" in out
    assert "(line 7)" in out
    assert "0.50" in out


def test_snippet_preview_truncates_and_flattens():
    text = "a\nb\nc " + ("x" * 300)
    out = snippet_preview(text, max_len=32)
    assert "\n" not in out
    assert len(out) <= 32


def test_shorten_path_falls_back_for_empty():
    assert shorten_path("") == "?"
