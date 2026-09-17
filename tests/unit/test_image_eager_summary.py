"""Opt-in eager image summaries.

Text-free photos default to a deferred placeholder, filled in lazily at query
time. That lazy path only exists in run_ask's image-collection branch, so MCP
retrieval (run_retrieve) returns the placeholder verbatim and a photo silo
answers "deferred visual summary". LLMLIBRARIAN_IMAGE_EAGER_SUMMARY=1 trades one
vision call per image at ingest for real descriptions in every retrieval path.
"""

from __future__ import annotations

import pytest

import processors


@pytest.mark.parametrize("value", ["1", "true", "yes", "TRUE"])
def test_flag_enables_eager_summaries(monkeypatch, value):
    monkeypatch.setenv("LLMLIBRARIAN_IMAGE_EAGER_SUMMARY", value)
    assert processors._eager_image_summary_enabled()


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_flag_defaults_off(monkeypatch, value):
    monkeypatch.setenv("LLMLIBRARIAN_IMAGE_EAGER_SUMMARY", value)
    assert not processors._eager_image_summary_enabled()


def test_unset_flag_is_off(monkeypatch):
    monkeypatch.delenv("LLMLIBRARIAN_IMAGE_EAGER_SUMMARY", raising=False)
    assert not processors._eager_image_summary_enabled()


def test_text_free_photo_is_deferred_by_default(monkeypatch):
    """A photo with no OCR text stays deferred unless the flag is set."""
    monkeypatch.delenv("LLMLIBRARIAN_IMAGE_EAGER_SUMMARY", raising=False)
    signal = processors._image_ocr_signal_assessment(None)
    assert signal["eager_summary"] is False


def test_flag_makes_text_free_photo_eager(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_IMAGE_EAGER_SUMMARY", "1")
    signal = processors._image_ocr_signal_assessment(None)
    assert signal["eager_summary"] is True
