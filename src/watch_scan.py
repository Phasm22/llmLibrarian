"""Lightweight file scan helpers for watch daemons (no chromadb/torch imports)."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

from file_registry import _read_file_manifest
from load_config import load_config

DEFAULT_MAX_DEPTH = 10
DEFAULT_MAX_FILES_PER_ZIP = 500
DEFAULT_MAX_EXTRACTED_BYTES_PER_ZIP = 50 * 1024 * 1024

from scan_patterns import ADD_DEFAULT_EXCLUDE, ADD_DEFAULT_INCLUDE  # noqa: F401  (re-exported)

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff"})
_PREVIEW_SKIPPED_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".wav", ".mp3", ".aac"})


def _path_matches(path_str: str, pattern: str) -> bool:
    """Match pattern against path. If pattern has no path sep, match against basename only (so *.pdf works).

    Matching is case-insensitive: fnmatch normcases via os.path.normcase, which
    is a no-op on POSIX, so a `*.jpg` include would silently skip `IMG_1.JPG`
    (the spelling most cameras and scanners use).
    """
    lowered = path_str.lower()
    pattern = pattern.lower()
    if "/" in pattern or "\\" in pattern:
        return fnmatch.fnmatch(lowered, pattern)
    return fnmatch.fnmatch(lowered, pattern) or fnmatch.fnmatch(os.path.basename(lowered), pattern)


def should_index(file_path: str | Path, include_patterns: list[str], exclude_patterns: list[str]) -> bool:
    path_str = str(file_path)
    base = os.path.basename(path_str)
    if base.startswith("~$"):
        return False
    for pattern in exclude_patterns:
        if pattern.rstrip("/") in path_str or _path_matches(path_str, pattern):
            return False
    for pattern in include_patterns:
        if _path_matches(path_str, pattern):
            return True
    return False


def should_descend_into_dir(dir_path: str | Path, exclude_patterns: list[str]) -> bool:
    path_str = str(dir_path).rstrip("/") + "/"
    for pattern in exclude_patterns:
        if pattern.rstrip("/") in path_str or fnmatch.fnmatch(path_str, pattern):
            return False
    return True


def collect_files(
    root: Path,
    include: list[str],
    exclude: list[str],
    max_depth: int,
    max_file_bytes: int,
    current_depth: int = 0,
    follow_symlinks: bool = False,
    stats: dict[str, Any] | None = None,
) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    if current_depth > max_depth:
        return out
    try:
        for item in sorted(root.iterdir()):
            if item.name.startswith("."):
                continue
            if item.is_symlink() and not follow_symlinks:
                continue
            path_str = str(item)
            if item.is_dir():
                if not should_descend_into_dir(path_str, exclude):
                    continue
                out.extend(
                    collect_files(
                        item, include, exclude, max_depth, max_file_bytes,
                        current_depth + 1, follow_symlinks=follow_symlinks, stats=stats,
                    )
                )
            else:
                if not should_index(path_str, include, exclude):
                    if stats is not None:
                        suf = item.suffix.lower()
                        if suf in _PREVIEW_SKIPPED_EXTENSIONS:
                            skipped = stats.setdefault("skipped_ext", {})
                            skipped[suf] = int(skipped.get(suf, 0)) + 1
                    continue
                try:
                    if item.stat().st_size > max_file_bytes:
                        continue
                except OSError:
                    continue
                suf = item.suffix.lower()
                if suf == ".zip":
                    kind = "zip"
                elif suf == ".pdf":
                    kind = "pdf"
                elif suf == ".docx":
                    kind = "docx"
                elif suf == ".xlsx":
                    kind = "xlsx"
                elif suf == ".pptx":
                    kind = "pptx"
                elif suf in IMAGE_EXTENSIONS:
                    kind = "image"
                else:
                    kind = "code"
                out.append((item, kind))
                if stats is not None:
                    supported = stats.setdefault("supported_kind", {})
                    supported[kind] = int(supported.get(kind, 0)) + 1
    except (PermissionError, OSError):
        pass
    return out


def _load_limits_config() -> tuple[int, int, int, int, int]:
    limits_cfg: dict[str, Any] = {}
    try:
        config = load_config()
        limits_cfg = config.get("limits") or {}
    except Exception:
        pass
    max_file_bytes = int((limits_cfg.get("max_file_size_mb") or 20) * 1024 * 1024)
    max_depth = int(limits_cfg.get("max_depth") or DEFAULT_MAX_DEPTH)
    max_archive_bytes = int((limits_cfg.get("max_archive_size_mb") or 100) * 1024 * 1024)
    max_files_per_zip = int(limits_cfg.get("max_files_per_zip") or DEFAULT_MAX_FILES_PER_ZIP)
    max_extracted_per_zip = int(
        limits_cfg.get("max_extracted_bytes_per_zip") or DEFAULT_MAX_EXTRACTED_BYTES_PER_ZIP
    )
    return max_file_bytes, max_depth, max_archive_bytes, max_files_per_zip, max_extracted_per_zip


def image_share(root: str | Path, *, limit: int = 2000) -> tuple[int, int]:
    """(image files, indexable files) among the first ``limit`` indexable files.

    Cheap pre-check for add_silo: walks with the default patterns only, stops
    early, reads no file contents.
    """
    root = Path(root)
    if root.is_file():
        return (1 if root.suffix.lower() in IMAGE_EXTENSIONS else 0), 1
    images = total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if should_descend_into_dir(os.path.join(dirpath, d), ADD_DEFAULT_EXCLUDE)
        ]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if not should_index(path, ADD_DEFAULT_INCLUDE, ADD_DEFAULT_EXCLUDE):
                continue
            total += 1
            if os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS:
                images += 1
            if total >= limit:
                return images, total
    return images, total


def vision_off_warning(images: int, total: int) -> str | None:
    """The one-line warning for a mostly-photo silo indexed without vision.

    dad-new-car (9 of 9 photos) was added with vision off; its chunks held only
    OCR, and "what kind of car did my dad get" scored 0.054.
    """
    if images < 3 or images * 2 < total:
        return None
    return (
        f"{images} of {total} files are images and image vision is off: photos are indexed by "
        "OCR text and EXIF only, so questions about what a photo shows will miss. To describe "
        "them, add with image vision on (`pal pull <path> --image-vision`, or add_silo "
        "image_vision=True); expect roughly 20-40 s per image with the local vision model."
    )


__all__ = [
    "ADD_DEFAULT_INCLUDE",
    "ADD_DEFAULT_EXCLUDE",
    "_read_file_manifest",
    "_load_limits_config",
    "collect_files",
    "image_share",
    "should_index",
    "vision_off_warning",
]
