from __future__ import annotations

import pytest

import image_embeddings


def test_image_embedding_error_names_image_extra_when_dependencies_missing(monkeypatch):
    monkeypatch.setattr(image_embeddings, "_open_clip_available", lambda: False)
    monkeypatch.setattr(image_embeddings, "_IMAGE_ADAPTER_CACHE", {})
    monkeypatch.setattr(image_embeddings, "_IMAGE_ADAPTER_ERROR", None)

    try:
        image_embeddings.ensure_image_embedding_adapter_ready()
    except image_embeddings.ImageEmbeddingError as exc:
        message = str(exc)
    else:
        raise AssertionError("missing image dependencies should fail readiness")

    assert "uv sync --extra image" in message
    assert "open_clip, torch, torchvision, or Pillow is not installed" in message


def test_image_embedding_error_preserves_initialization_failure(monkeypatch):
    monkeypatch.setattr(image_embeddings, "_open_clip_available", lambda: True)
    monkeypatch.setattr(image_embeddings, "_IMAGE_ADAPTER_CACHE", {})
    monkeypatch.setattr(image_embeddings, "_IMAGE_ADAPTER_ERROR", None)
    monkeypatch.setattr(
        image_embeddings.OpenCLIPAdapter,
        "create",
        classmethod(lambda _cls: (_ for _ in ()).throw(RuntimeError("operator torchvision::nms does not exist"))),
    )

    try:
        image_embeddings.ensure_image_embedding_adapter_ready()
    except image_embeddings.ImageEmbeddingError as exc:
        message = str(exc)
    else:
        raise AssertionError("broken image dependencies should fail readiness")

    assert "RuntimeError: operator torchvision::nms does not exist" in message
    assert "uv sync --extra image" in message


def test_heic_requires_decoder_before_ingest(monkeypatch):
    real_find_spec = image_embeddings.importlib.util.find_spec
    monkeypatch.setattr(
        image_embeddings.importlib.util,
        "find_spec",
        lambda name: None if name == "pillow_heif" else real_find_spec(name),
    )

    with pytest.raises(image_embeddings.ImageEmbeddingError, match="pillow-heif"):
        image_embeddings.ensure_image_decoders_ready(["/photos/IMG_1.HEIC"])

    image_embeddings.ensure_image_decoders_ready(["/photos/IMG_1.JPG"])
