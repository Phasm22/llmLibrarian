import image_embeddings


def test_image_embeddings_default_to_cpu(monkeypatch):
    # MPS pinned ~1.6 GB in the resident MCP server for no speedup; see _preferred_device.
    monkeypatch.delenv("LLMLIBRARIAN_IMAGE_EMBEDDING_DEVICE", raising=False)
    assert image_embeddings._preferred_device() == "cpu"


def test_image_embeddings_device_override(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_IMAGE_EMBEDDING_DEVICE", "mps")
    assert image_embeddings._preferred_device() == "mps"
