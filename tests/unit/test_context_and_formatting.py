from datetime import datetime, timezone

import style
from query.context import (
    context_block,
    query_implies_recency,
    query_mentioned_years,
    recency_score,
)
from query.formatting import format_source, render_sources_footer, shorten_path, snippet_preview, style_answer


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


def test_context_block_uses_region_when_line_and_page_absent():
    block = context_block(
        doc="image snippet",
        meta={
            "source": "/tmp/file.jpg",
            "region_index": 2,
            "mtime": 1700000000,
            "silo": "photos",
            "doc_type": "other",
        },
        show_silo=False,
    )
    assert "(region 2)" in block


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


def test_format_source_supports_region_location():
    out = format_source(
        doc="region text",
        meta={"source": "/tmp/test.jpg", "region_index": 3, "source_modality": "image"},
        distance=1.0,
        no_color=True,
    )
    assert "test.jpg" in out or "/tmp/test.jpg" in out
    assert "(region 3)" in out


def test_format_source_can_omit_snippet():
    out = format_source(
        doc="line1\nline2",
        meta={"source": "/tmp/test.txt", "line_start": 7},
        distance=None,
        include_snippet=False,
        no_color=True,
    )
    assert "test.txt" in out or "/tmp/test.txt" in out
    assert "(line 7)" in out
    assert "\n    " not in out


def test_format_source_omits_blank_snippet_line_when_doc_empty():
    out = format_source(
        doc="",
        meta={"source": "/tmp/test.txt", "line_start": 7},
        distance=None,
        no_color=True,
    )
    assert "test.txt" in out or "/tmp/test.txt" in out
    assert "(line 7)" in out
    assert "\n    " not in out


def test_render_sources_footer_defaults_to_summary_only():
    out = render_sources_footer(
        docs=["alpha context", "beta context"],
        metas=[
            {"source": "/tmp/a.txt", "line_start": 7},
            {"source": "/tmp/b.txt", "line_start": 11},
        ],
        dists=[1.0, 3.0],
        no_color=True,
    )
    assert out == ["Sources: 2 sources | median match 0.38"]


def test_render_sources_footer_detailed_mode_uses_compact_file_lines():
    out = render_sources_footer(
        docs=["alpha context"],
        metas=[{"source": "/tmp/a.txt", "line_start": 7}],
        dists=[1.0],
        no_color=True,
        detailed=True,
    )
    assert out[0] == "Sources: 1 source | median match 0.50"
    assert "a.txt" in out[1]
    assert "alpha context" not in out[1]


def test_render_sources_footer_aggregates_image_regions():
    out = render_sources_footer(
        docs=["summary", "region one", "region two"],
        metas=[
            {"source": "/tmp/a.jpg", "region_index": 0, "source_modality": "image"},
            {"source": "/tmp/a.jpg", "region_index": 1, "source_modality": "image"},
            {"source": "/tmp/a.jpg", "region_index": 2, "source_modality": "image"},
        ],
        dists=[0.1, 0.2, 0.3],
        no_color=True,
        detailed=True,
    )
    assert out[0] == "Sources: 1 source | median match 0.91"
    assert "(region 0, 1, 2)" in out[1]


def test_snippet_preview_truncates_and_flattens():
    text = "a\nb\nc " + ("x" * 300)
    out = snippet_preview(text, max_len=32)
    assert "\n" not in out
    assert len(out) <= 32


def test_shorten_path_falls_back_for_empty():
    assert shorten_path("") == "?"
