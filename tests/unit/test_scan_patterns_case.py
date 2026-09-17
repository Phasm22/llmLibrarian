"""Include/exclude patterns must ignore extension case.

Regression: fnmatch normcases through os.path.normcase, which is a no-op on
POSIX, so the lowercase `*.jpg` / `*.pdf` include rules silently skipped the
uppercase spellings cameras and scanners actually produce. A folder of .JPG
photos indexed as zero files with no error. The same gap weakened the
exclusions: `*.pem` did not match `KEY.PEM`.
"""

from __future__ import annotations

import pytest

from ingest import should_index as should_index_main
from ingest.watch_scan import (
    ADD_DEFAULT_EXCLUDE,
    ADD_DEFAULT_INCLUDE,
    should_index as should_index_watch,
)

_IMPLS = (should_index_main, should_index_watch)


@pytest.mark.parametrize("should_index", _IMPLS)
@pytest.mark.parametrize(
    "name",
    ["IMG_3083.JPG", "scan.JPEG", "shot.PNG", "photo.HEIC", "report.PDF", "notes.MD"],
)
def test_uppercase_extensions_are_indexed(should_index, name):
    assert should_index(f"/tmp/silo/{name}", ADD_DEFAULT_INCLUDE, ADD_DEFAULT_EXCLUDE)


@pytest.mark.parametrize("should_index", _IMPLS)
@pytest.mark.parametrize("name", ["img.jpg", "report.pdf", "notes.md"])
def test_lowercase_extensions_still_indexed(should_index, name):
    assert should_index(f"/tmp/silo/{name}", ADD_DEFAULT_INCLUDE, ADD_DEFAULT_EXCLUDE)


@pytest.mark.parametrize("should_index", _IMPLS)
@pytest.mark.parametrize("name", ["KEY.PEM", "id.KEY", "key.pem", "secret.exe"])
def test_excluded_and_unknown_extensions_stay_out(should_index, name):
    assert not should_index(f"/tmp/silo/{name}", ADD_DEFAULT_INCLUDE, ADD_DEFAULT_EXCLUDE)
