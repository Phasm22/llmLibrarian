"""Coalesce identical MCP retrieval calls and remember them briefly.

On 2026-10-02 a small model sent the same query and silo 6× (and, elsewhere, 10×)
within one second — parallel tool calls in a single turn. Each ran its own
embedding and Chroma round-trip and returned the same chunks.

Two mechanisms, both above Chroma:
- single-flight: while a call is computing, identical calls wait for its result
  instead of starting their own (a TTL alone misses a burst that arrives before
  the first call finishes);
- a short TTL (default 30s) for calls that arrive after it finished.

It only ever removes Chroma calls; it never adds concurrency below it, so the
HNSW constraints in CLAUDE.md are unaffected. Results that describe a transient
state (busy, error, retryable, an in-flight rebuild) are never cached. Callers
put an index-state stamp in the key, so a finished ingest or a privacy flip
misses the cache.
"""
from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

_FOLLOWER_WAIT_SECONDS = 120.0


def is_cacheable(response: dict[str, Any]) -> bool:
    if not isinstance(response, dict):
        return False
    if any(response.get(k) for k in ("busy", "error", "errors", "retryable", "write_in_progress")):
        return False
    return True


class _Entry:
    __slots__ = ("event", "done", "ok", "value", "created", "hits", "first_seen_wall")

    def __init__(self, now: float) -> None:
        self.event = threading.Event()
        self.done = False
        self.ok = False
        self.value: Any = None
        self.created = now
        self.hits = 1
        self.first_seen_wall = time.time()


class ResultCache:
    def __init__(self, *, max_entries: int = 64, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[Any, _Entry] = OrderedDict()
        self._max = max_entries
        self._clock = clock

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def get_or_compute(
        self,
        key: Any,
        compute: Callable[[], Any],
        *,
        ttl_seconds: float,
        cacheable: Callable[[Any], bool] = lambda value: is_cacheable(value[0] if isinstance(value, tuple) else value),
    ) -> tuple[Any, dict[str, Any]]:
        """Return (value, meta). meta: cached (bool), repeat_count (1 = first call),
        first_seen (epoch seconds of the first identical call in the window)."""
        if ttl_seconds <= 0:
            return compute(), {"cached": False, "repeat_count": 1, "first_seen": time.time()}

        with self._lock:
            now = self._clock()
            entry = self._entries.get(key)
            if entry is not None and entry.done and now - entry.created > ttl_seconds:
                self._entries.pop(key, None)
                entry = None
            if entry is not None:
                entry.hits += 1
                hits = entry.hits
                self._entries.move_to_end(key)
                leader = False
            else:
                entry = _Entry(now)
                self._entries[key] = entry
                self._evict_locked()
                hits = 1
                leader = True

        meta = {"cached": not leader, "repeat_count": hits, "first_seen": entry.first_seen_wall}
        if not leader:
            if entry.event.wait(_FOLLOWER_WAIT_SECONDS) and entry.ok:
                return copy.deepcopy(entry.value), meta
            # The leader failed or is stuck: do the work ourselves, uncached.
            return compute(), {**meta, "cached": False}

        try:
            value = compute()
        except BaseException:
            with self._lock:
                if self._entries.get(key) is entry:
                    self._entries.pop(key, None)
            entry.event.set()
            raise
        entry.value = copy.deepcopy(value)
        entry.ok = True
        entry.done = True
        with self._lock:
            if not cacheable(value) and self._entries.get(key) is entry:
                # Waiters already queued still get this result; later calls recompute.
                self._entries.pop(key, None)
        entry.event.set()
        return value, meta

    def _evict_locked(self) -> None:
        while len(self._entries) > self._max:
            for old_key, old in self._entries.items():
                if old.done:
                    self._entries.pop(old_key, None)
                    break
            else:
                return


def repeat_fields(meta: dict[str, Any], *, window_seconds: float) -> dict[str, Any]:
    """Response fields telling the caller its call was a repeat."""
    count = int(meta.get("repeat_count") or 1)
    if count < 2:
        return {}
    first = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(meta.get("first_seen") or time.time())))
    out: dict[str, Any] = {"repeat_of": {"first_at": first, "count": count}}
    if count >= 3:
        out["repeat_notice"] = (
            f"This is identical call #{count} within {window_seconds:g}s and returned the same "
            "result. Answer from it, or change the query or silo — repeating it will not change it."
        )
    return out
