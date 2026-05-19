"""Structured JSON telemetry for pal silo watchers (monitoring / jq parsers)."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

EVENT_WATCH_START = "watch_start"
EVENT_RECONCILE = "reconcile"
EVENT_BATCH_DRAIN = "batch_drain"
EVENT_QUEUE_LOOP_ERROR = "queue_loop_error"


def watch_json_enabled() -> bool:
    raw = os.environ.get("PAL_WATCH_JSON", "1")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def emit_watch_event(logger: logging.Logger, event: str, **fields: object) -> None:
    """Emit one JSON log line when PAL_WATCH_JSON is enabled (default on)."""
    if not watch_json_enabled():
        return
    payload: dict[str, object] = {
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    logger.info(json.dumps(payload, separators=(",", ":")))
