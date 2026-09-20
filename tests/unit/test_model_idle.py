import sys
import threading
import time
import types

import model_idle


def _fake_modules(monkeypatch):
    emb = types.SimpleNamespace(_ef_cache={("", "m", "cpu", None): object()}, _ef_cache_lock=threading.Lock())
    img = types.SimpleNamespace(_IMAGE_ADAPTER_CACHE={"default": object()})
    rr = types.SimpleNamespace(_model_cache={}, _cache_lock=threading.Lock())
    monkeypatch.setitem(sys.modules, "embeddings", emb)
    monkeypatch.setitem(sys.modules, "image_embeddings", img)
    monkeypatch.setitem(sys.modules, "reranker", rr)
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    return emb, img


def test_unload_clears_every_imported_cache(monkeypatch):
    emb, img = _fake_modules(monkeypatch)
    assert model_idle.unload_models() == 2
    assert emb._ef_cache == {} and img._IMAGE_ADAPTER_CACHE == {}
    assert model_idle.unload_models() == 0


def test_timeout_env(monkeypatch):
    monkeypatch.delenv("LLMLIBRARIAN_MODEL_IDLE_UNLOAD_SECONDS", raising=False)
    assert model_idle.idle_timeout_seconds() == 900.0
    monkeypatch.setenv("LLMLIBRARIAN_MODEL_IDLE_UNLOAD_SECONDS", "0")
    assert model_idle.start_idle_reaper() is None
    monkeypatch.setenv("LLMLIBRARIAN_MODEL_IDLE_UNLOAD_SECONDS", "junk")
    assert model_idle.idle_timeout_seconds() == 900.0


def test_reaper_waits_for_idle_and_not_busy(monkeypatch):
    emb, _img = _fake_modules(monkeypatch)
    busy = {"on": True}
    stop = threading.Event()
    model_idle.touch()
    thread = model_idle.start_idle_reaper(is_busy=lambda: busy["on"], timeout=0.05, stop=stop)
    try:
        time.sleep(0.3)
        assert emb._ef_cache, "must not unload while a background job is running"
        busy["on"] = False
        deadline = time.time() + 2
        while emb._ef_cache and time.time() < deadline:
            time.sleep(0.05)
        assert emb._ef_cache == {}
    finally:
        stop.set()
        thread.join(timeout=1)


def test_getters_mark_use(monkeypatch):
    import embeddings

    monkeypatch.setattr(model_idle, "_last_used", 0.0)
    monkeypatch.setitem(embeddings._ef_cache, ("", "all-mpnet-base-v2", "cpu", None), object())
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_DEVICE", "cpu")
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING", raising=False)
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("LLMLIBRARIAN_EMBEDDING_BATCH_SIZE", raising=False)
    embeddings.get_embedding_function(batch_size=1)
    assert model_idle.idle_seconds() < 5
