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


def image_collection_name(base_collection_name: str) -> str:
    if not base_collection_name or base_collection_name == "llmli":
        return "llmli_image"
    return f"{base_collection_name}_image"


def _open_clip_available() -> bool:
    return (
        importlib.util.find_spec("open_clip") is not None
        and importlib.util.find_spec("torch") is not None
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

        inputs: list[np.ndarray[Any, Any]] = []
        for raw_path in image_paths:
            with Image.open(Path(raw_path)) as img:
                inputs.append(np.asarray(img.convert("RGB")))
        return [vec.tolist() for vec in self._embedder(inputs)]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [vec.tolist() for vec in self._embedder(texts)]


def get_image_embedding_adapter() -> ImageEmbeddingAdapter | None:
    cached = _IMAGE_ADAPTER_CACHE.get("default")
    if cached is not None:
        return cached
    if not _open_clip_available():
        return None
    try:
        adapter = OpenCLIPAdapter.create()
    except Exception:
        return None
    _IMAGE_ADAPTER_CACHE["default"] = adapter
    return adapter


def ensure_image_embedding_adapter_ready() -> ImageEmbeddingAdapter:
    adapter = get_image_embedding_adapter()
    if adapter is None:
        raise ImageEmbeddingError(
            "Standalone image embeddings require open_clip + torch. "
            "Run `uv sync` so the image embedding dependencies are installed."
        )
    return adapter


def image_embedding_backend_name() -> str | None:
    if _open_clip_available():
        return "open_clip"
    return None
