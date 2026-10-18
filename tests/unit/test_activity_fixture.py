"""The shared activity fixture must match what the Python overview produces today.

macOS reads the same ledger with its own Swift reader and checks itself against
``tests/fixtures/activity/overview-7d.json``; this test keeps that JSON honest,
so a change to the overview contract fails here until the fixture is regenerated
(``TZ=UTC uv run python tests/fixtures/activity/make_fixture.py``) and the Swift
reader is updated to match.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "activity"


def _load_generator():
    spec = importlib.util.spec_from_file_location("make_activity_fixture", FIXTURE / "make_fixture.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_committed_overview_matches_generator(tmp_path, monkeypatch):
    old_tz = os.environ.get("TZ")
    try:
        data = _load_generator().build(tmp_path)
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
    committed = json.loads((FIXTURE / "overview-7d.json").read_text())
    data = json.loads(json.dumps(data, sort_keys=True))
    data.pop("enabled", None)
    committed.pop("enabled", None)
    assert data == committed
