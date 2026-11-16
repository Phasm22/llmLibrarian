import processors
from constants import DEFAULT_MODEL, DEFAULT_VISION_MODEL


def test_local_model_defaults_use_qwen35(monkeypatch):
    monkeypatch.delenv("LLMLIBRARIAN_VISION_MODEL", raising=False)

    assert DEFAULT_MODEL == "qwen3.5:9b"
    assert DEFAULT_VISION_MODEL == "qwen3.5:9b"
    assert processors._configured_vision_model() == "qwen3.5:9b"


def test_vision_model_environment_override_is_preserved(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_VISION_MODEL", "custom-vision-model")

    assert processors._configured_vision_model() == "custom-vision-model"
