"""Pal bookmark registry JSON read/write (path supplied by caller)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from registry_lock import registry_transaction  # noqa: E402


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Replace ``path`` in one step, via a uniquely-named temp file.

    A plain ``open(path, "w")`` truncates first, so a crash or a concurrent
    reader mid-write sees an empty or half-written registry. The temp file name
    must be unique too: a fixed ``<name>.tmp`` shared by several writers makes
    the loser's ``os.replace`` fail after the winner moved it away.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
            tmp_path = Path(f.name)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def read_pal_registry(registry_path: Path) -> dict[str, Any]:
    """Read the pal registry. Migrates legacy 'sources' key to 'bookmarks' transparently."""
    empty: dict[str, Any] = {"bookmarks": []}
    if not registry_path.exists():
        return empty
    try:
        with open(registry_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return empty
    if "sources" in data and "bookmarks" not in data:
        raw = data.get("sources") or []
        data["bookmarks"] = [
            {k: v for k, v in entry.items() if k in ("path", "name", "silo")}
            for entry in raw
            if isinstance(entry, dict) and entry.get("path")
        ]
        del data["sources"]
    return data


def cleanup_stale_registry_entries(llmli_registry_path: Path) -> bool:
    """Drop legacy hash-less silo slugs superseded by a hashed slug for the same path.

    This exists for one migration: slugs used to be a bare name (``desktop``) and
    became ``name-<hash8>``, leaving both registered against one folder.

    **It deletes registry entries, so it must never run as a side effect of a
    read.** It used to, from ``pal._read_llmli_registry``, and that is how
    ``llmlibrarian-46ad0cbe`` disappeared: once silo paths were canonicalized
    through symlinks, the two llmLibrarian silos reported the same ``path``, this
    grouped them as duplicates, and a plain ``pal`` command deleted one — leaving
    its 2467 chunks orphaned in Chroma with nothing pointing at them.

    Two guards now keep that from recurring:

    - only *legacy-shaped* slugs are removable. A slug carrying a ``-<hash8>``
      suffix is a real silo; two of those sharing a path are a duplicate for a
      human to resolve (``llmli rm``), not something to delete silently.
    - the whole read-modify-write runs under the registry lock and lands
      atomically.

    Returns True when something was removed.
    """
    if not llmli_registry_path.exists():
        return False

    with registry_transaction(llmli_registry_path):
        try:
            with open(llmli_registry_path, "r", encoding="utf-8") as f:
                reg = json.load(f)
        except Exception:
            return False
        if not isinstance(reg, dict):
            return False

        path_to_slugs: dict[str, list[str]] = {}
        for slug, entry in reg.items():
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if path:
                path_to_slugs.setdefault(str(path), []).append(slug)

        to_delete: set[str] = set()
        for _path, slugs in path_to_slugs.items():
            if len(slugs) < 2:
                continue
            hashed = [s for s in slugs if _has_hash_suffix(s)]
            if not hashed:
                # All legacy. Nothing supersedes anything; leave them alone.
                continue
            for slug in slugs:
                # Only ever remove the pre-migration shape.
                if not _has_hash_suffix(slug):
                    to_delete.add(slug)

        if not to_delete:
            return False

        for slug in to_delete:
            del reg[slug]
        _atomic_write_json(llmli_registry_path, reg)
    return True


def _has_hash_suffix(slug: str) -> bool:
    """True for the current ``name-<8 hex>`` slug shape produced by state.slugify."""
    _, _, tail = slug.rpartition("-")
    return len(tail) == 8 and all(c in "0123456789abcdef" for c in tail)


def write_pal_registry(registry_path: Path, data: dict[str, Any]) -> None:
    with registry_transaction(registry_path):
        _atomic_write_json(registry_path, data)
