"""Drop cached embedding/rerank models after a stretch without use.

The MCP server is resident for days but queried in bursts. Keeping the text
embedder (~180 MB), OpenCLIP (~600 MB) and any reranker loaded between bursts
costs ~400 MB of idle memory; reloading from the local HF cache takes a few
seconds on the next query. Callers mark use with ``touch()``; the server runs
``start_idle_reaper`` to unload once nothing has touched a model for
LLMLIBRARIAN_MODEL_IDLE_UNLOAD_SECONDS (default 900; 0 disables).

Kept dependency-free so the model modules can import it cheaply.
"""
from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time
from typing import Callable

_DEFAULT_IDLE_SECONDS = 900.0

_logger = logging.getLogger("llmLibrarian.model_idle")
_last_used = time.monotonic()


def touch() -> None:
    global _last_used
    _last_used = time.monotonic()


def idle_seconds() -> float:
    return time.monotonic() - _last_used


def idle_timeout_seconds() -> float:
    raw = os.environ.get("LLMLIBRARIAN_MODEL_IDLE_UNLOAD_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_IDLE_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_IDLE_SECONDS


def unload_models() -> int:
    """Clear every model cache that has been imported; return how many models were dropped.

    Callers that still hold a model keep it alive until they finish; only the
    cache's reference goes away, so an in-flight query is never interrupted.
    """
    dropped = 0
    embeddings = sys.modules.get("embeddings")
    if embeddings is not None:
        with embeddings._ef_cache_lock:
            dropped += len(embeddings._ef_cache)
            embeddings._ef_cache.clear()
    image_embeddings = sys.modules.get("image_embeddings")
    if image_embeddings is not None:
        dropped += len(image_embeddings._IMAGE_ADAPTER_CACHE)
        image_embeddings._IMAGE_ADAPTER_CACHE.clear()
    reranker = sys.modules.get("reranker")
    if reranker is not None:
        with reranker._cache_lock:
            dropped += len(reranker._model_cache)
            reranker._model_cache.clear()
    if not dropped:
        return 0
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
            elif torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
    return dropped


def start_idle_reaper(
    is_busy: Callable[[], bool] = lambda: False,
    timeout: float | None = None,
    stop: threading.Event | None = None,
) -> threading.Thread | None:
    """Start a daemon thread that unloads models once idle for ``timeout`` seconds.

    ``is_busy`` lets the server defer while a background ingest holds a model
    between embed calls. Returns None when unloading is disabled.
    """
    limit = idle_timeout_seconds() if timeout is None else timeout
    if limit <= 0:
        return None
    poll = min(limit, 60.0)
    stop = stop or threading.Event()

    def _loop() -> None:
        while not stop.wait(poll):
            if idle_seconds() < limit:
                continue
            try:
                if is_busy():
                    continue
                dropped = unload_models()
            except Exception:
                _logger.exception("idle model unload failed")
                continue
            if dropped:
                _logger.info("unloaded %d idle model(s) after %.0fs without use", dropped, limit)

    thread = threading.Thread(target=_loop, name="model-idle-reaper", daemon=True)
    thread.start()
    return thread
