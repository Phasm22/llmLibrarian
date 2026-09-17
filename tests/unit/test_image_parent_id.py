"""Identical images in one silo must not collide on the image-vector id.

Regression: the id was the file content hash, so two byte-identical images —
ordinary in a photo library (same shot imported twice, "IMG_1.JPG" beside
"IMG_1 copy.JPG") — produced the same id. Chroma rejected the whole image batch
with "Expected IDs to be unique", aborting ingest before the silo registered.
"""

from __future__ import annotations

from ingest import _image_parent_id


def test_identical_content_at_different_paths_gets_distinct_ids():
    a = _image_parent_id("/photos/IMG_1.JPG", "samehash", 1.0)
    b = _image_parent_id("/photos/IMG_1 copy.JPG", "samehash", 2.0)
    assert a != b


def test_same_path_and_content_is_stable():
    a = _image_parent_id("/photos/IMG_1.JPG", "samehash", 1.0)
    b = _image_parent_id("/photos/IMG_1.JPG", "samehash", 999.0)
    assert a == b, "id must not drift with mtime when content is unchanged"


def test_changed_content_changes_id():
    a = _image_parent_id("/photos/IMG_1.JPG", "hash-a", 1.0)
    b = _image_parent_id("/photos/IMG_1.JPG", "hash-b", 1.0)
    assert a != b


def test_hashless_fallback_still_distinct_per_path():
    a = _image_parent_id("/photos/a.JPG", None, 1.0)
    b = _image_parent_id("/photos/b.JPG", None, 1.0)
    assert a != b
