from pathlib import Path

from query.formatting import shorten_path, source_url


def test_shorten_path_root_fallback():
    assert shorten_path("/") == "/"


def test_source_url_encodes_spaces(tmp_path):
    p = tmp_path / "file with space.txt"
    p.write_text("x", encoding="utf-8")
    url = source_url(str(p))
    assert "%20" in url
    assert " " not in url
