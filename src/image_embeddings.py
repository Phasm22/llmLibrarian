"""
Image embedding adapters for standalone image retrieval.

The query/ingest stack talks to this module instead of a provider directly so
we can swap multimodal embedding backends without reshaping the rest of the app.
"""

from __future__ import annotations

import importlib.util
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from chromadb.utils.embedding_functions import OpenCLIPEmbeddingFunction

import model_idle


class ImageEmbeddingError(Exception):
    """Raised when standalone image embeddings are required but unavailable."""


class ImageEmbeddingAdapter(Protocol):
    backend_name: str

    def embed_image_paths(self, image_paths: list[str]) -> list[list[float]]:
        ...

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        ...


_IMAGE_ADAPTER_CACHE: dict[str, ImageEmbeddingAdapter] = {}
_IMAGE_ADAPTER_ERROR: str | None = None
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff"})


def image_collection_name(base_collection_name: str) -> str:
    if not base_collection_name or base_collection_name == "llmli":
        return "llmli_image"
    return f"{base_collection_name}_image"


def _open_clip_available() -> bool:
    return (
        importlib.util.find_spec("open_clip") is not None
        and importlib.util.find_spec("torch") is not None
        and importlib.util.find_spec("torchvision") is not None
        and importlib.util.find_spec("PIL") is not None
    )


def _preferred_device() -> str:
    """CPU unless LLMLIBRARIAN_IMAGE_EMBEDDING_DEVICE names another torch device.

    On Apple Silicon, CLIP ViT-B/32 on MPS pinned ~1.6 GB of GPU memory in the
    long-lived MCP server (the MPS heap is not returned even after unloading)
    while buying nothing: measured per-image embed time was 127 ms on MPS vs
    124 ms on CPU, since image decode dominates, and a text query was 8 ms vs
    25 ms. CPU holds the same model in about half the memory.
    """
    return os.environ.get("LLMLIBRARIAN_IMAGE_EMBEDDING_DEVICE", "").strip() or "cpu"


@dataclass(frozen=True)
class OpenCLIPAdapter:
    backend_name: str
    _embedder: Any

    @classmethod
    def create(cls) -> "OpenCLIPAdapter":
        embedder = OpenCLIPEmbeddingFunction(device=_preferred_device())
        return cls(backend_name="open_clip", _embedder=embedder)

    def embed_image_paths(self, image_paths: list[str]) -> list[list[float]]:
        from PIL import Image
        try:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        except ImportError:
            pass

        inputs: list[np.ndarray[Any, Any]] = []
        for raw_path in image_paths:
            with Image.open(Path(raw_path)) as img:
                inputs.append(np.asarray(img.convert("RGB")))
        return [vec.tolist() for vec in self._embedder(inputs)]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [vec.tolist() for vec in self._embedder(texts)]


def get_image_embedding_adapter() -> ImageEmbeddingAdapter | None:
    global _IMAGE_ADAPTER_ERROR
    model_idle.touch()
    cached = _IMAGE_ADAPTER_CACHE.get("default")
    if cached is not None:
        return cached
    if not _open_clip_available():
        _IMAGE_ADAPTER_ERROR = "open_clip, torch, torchvision, or Pillow is not installed"
        return None
    try:
        adapter = OpenCLIPAdapter.create()
    except Exception as exc:
        _IMAGE_ADAPTER_ERROR = f"{type(exc).__name__}: {exc}"
        return None
    _IMAGE_ADAPTER_ERROR = None
    _IMAGE_ADAPTER_CACHE["default"] = adapter
    return adapter


def ensure_image_embedding_adapter_ready() -> ImageEmbeddingAdapter:
    adapter = get_image_embedding_adapter()
    if adapter is None:
        detail = f" Initialization failed: {_IMAGE_ADAPTER_ERROR}." if _IMAGE_ADAPTER_ERROR else ""
        raise ImageEmbeddingError(
            "Standalone image embeddings require a working open_clip + torch + torchvision stack."
            f"{detail} Run `uv sync --extra image` in the project checkout."
        )
    return adapter


def ensure_image_decoders_ready(image_paths: list[str | Path]) -> None:
    """Fail before ingest when a selected format lacks its decoder."""
    needs_heif = any(Path(path).suffix.lower() in {".heic", ".heif"} for path in image_paths)
    if needs_heif and importlib.util.find_spec("pillow_heif") is None:
        raise ImageEmbeddingError(
            "HEIC/HEIF indexing requires pillow-heif. "
            "Run `uv sync --extra image` in the project checkout."
        )


def image_embedding_backend_name() -> str | None:
    global _IMAGE_ADAPTER_ERROR
    if not _open_clip_available():
        _IMAGE_ADAPTER_ERROR = "open_clip, torch, torchvision, or Pillow is not installed"
        return None
    try:
        # find_spec alone reports a broken torch/torchvision installation as
        # available. Import the stack so `capabilities` reflects runtime truth
        # without constructing/downloading the OpenCLIP model.
        import open_clip  # noqa: F401
        import torch  # noqa: F401
        import torchvision  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception as exc:
        _IMAGE_ADAPTER_ERROR = f"{type(exc).__name__}: {exc}"
        return None
    _IMAGE_ADAPTER_ERROR = None
    return "open_clip"


def image_embedding_unavailable_reason() -> str | None:
    """Return the last adapter initialization failure for observable fallbacks."""
    return _IMAGE_ADAPTER_ERROR


# Canonical image-backend capability. Every consumer (ingest gating, the MCP
# startup/timer check, /healthz `image_backend`, the watcher's recovery poll)
# reads this one cached value instead of probing on its own.
IMAGE_BACKEND_RECHECK_SECONDS = 300.0
IMAGE_BACKEND_MISSING_REASON = "image embeddings unavailable; run `uv sync --extra image`"
_IMAGE_BACKEND_CHECK: dict[str, Any] | None = None


def image_backend_status(
    *,
    max_age: float = IMAGE_BACKEND_RECHECK_SECONDS,
    now: float | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Cached backend capability: ``{"status": "ok"|"missing", "reason", "checked_at"}``.

    Re-probes through ``image_embedding_backend_name`` (import-only, no model
    load) when the cached answer is older than ``max_age`` or ``refresh`` is set.
    """
    global _IMAGE_BACKEND_CHECK
    ts = time.time() if now is None else now
    cached = _IMAGE_BACKEND_CHECK
    if not refresh and cached is not None and ts - float(cached["checked_at"]) < max_age:
        return dict(cached)
    ok = image_embedding_backend_name() is not None
    _IMAGE_BACKEND_CHECK = {
        "status": "ok" if ok else "missing",
        "reason": None if ok else (image_embedding_unavailable_reason() or "unknown"),
        "checked_at": ts,
    }
    return dict(_IMAGE_BACKEND_CHECK)


IMAGE_EXTRA_MISSING_MESSAGE = (
    "image files are in a configured folder but the image extra is not installed; "
    "they will park until fixed. Run `uv sync --extra image` in the project checkout."
)
_IMAGE_SCAN_MAX_ENTRIES = 50_000


def folder_has_image_files(root: str | Path, *, max_entries: int = _IMAGE_SCAN_MAX_ENTRIES) -> bool:
    """True when ``root`` holds a file the image backend would index.

    Early exit on the first image; hidden directories are skipped and the walk
    is capped at ``max_entries`` so a huge tree cannot stall startup. Returns
    only a boolean: no filenames leave this function.
    """
    base = Path(root)
    if not base.is_dir():
        return False
    seen = 0
    for _dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix.lower() in IMAGE_SUFFIXES:
                return True
            seen += 1
            if seen >= max_entries:
                return False
    return False
