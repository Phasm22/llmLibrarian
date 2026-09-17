"""
Image embedding adapters for standalone image retrieval.

The query/ingest stack talks to this module instead of a provider directly so
we can swap multimodal embedding backends without reshaping the rest of the app.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from chromadb.utils.embedding_functions import OpenCLIPEmbeddingFunction


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
    try:
        import torch

        if bool(getattr(torch.backends, "mps", None)) and torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


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
