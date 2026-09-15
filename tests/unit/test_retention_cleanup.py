from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from retention_cleanup import plan_deletes, run_cleanup  # noqa: E402


def _old(path: Path, days: int) -> None:
    when = datetime.now(timezone.utc) - timedelta(days=days)
    os.utime(path, (when.timestamp(), when.timestamp()))


def test_plans_only_generated_old_logs(tmp_path: Path):
    old_log = tmp_path / "watch-alpha.log"
    old_log.write_text("old", encoding="utf-8")
    _old(old_log, 45)
    durable = tmp_path / "registry.json"
    durable.write_text("{}", encoding="utf-8")
    _old(durable, 45)
    recent = tmp_path / "llmlibrarian-chroma.stdout.log"
    recent.write_text("recent", encoding="utf-8")

    planned = plan_deletes(tmp_path, log_days=30, keep_newest=0)
    assert [item.path for item in planned] == [old_log]


def test_apply_deletes_planned_log_only(tmp_path: Path):
    old_log = tmp_path / "llmlibrarian-chroma.stderr.log"
    old_log.write_text("old", encoding="utf-8")
    _old(old_log, 45)
    silo_file = tmp_path / "my_brain_db.sqlite"
    silo_file.write_text("durable", encoding="utf-8")
    _old(silo_file, 45)

    result = run_cleanup(log_dir=tmp_path, apply=True, log_days=30, keep_newest=0)
    assert result["deleted"] == [str(old_log)]
    assert not old_log.exists()
    assert silo_file.exists()
