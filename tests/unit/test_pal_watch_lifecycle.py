import json
from pathlib import Path

import pal


def _write_lock(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_status_records_filter_by_path(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("pal.WATCH_LOCKS_DIR", tmp_path / "watch_locks")
    _write_lock(
        pal._watch_lock_path(tmp_path / "db", "alpha"),
        {"version": 2, "pid": 111, "uid": 1, "user": "u", "silo": "alpha", "db_path": str(tmp_path / "db"), "root_path": "/tmp/alpha"},
    )
    _write_lock(
        pal._watch_lock_path(tmp_path / "db", "beta"),
        {"version": 2, "pid": 222, "uid": 1, "user": "u", "silo": "beta", "db_path": str(tmp_path / "db"), "root_path": "/tmp/beta"},
    )
    monkeypatch.setattr("pal._pid_is_running", lambda _pid: False)
    monkeypatch.setattr(
        "pal._read_llmli_registry",
        lambda _db: {
            "alpha": {"path": str(Path("/tmp/alpha").resolve())},
            "beta": {"path": str(Path("/tmp/beta").resolve())},
        },
    )
    records, err = pal._status_records(str(tmp_path / "db"), Path("/tmp/alpha"))
    assert err is None
    assert len(records) == 1
    assert records[0]["silo"] == "alpha"


def test_legacy_lock_is_detected(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("pal.WATCH_LOCKS_DIR", tmp_path / "watch_locks")
    _write_lock(
        pal._watch_lock_path(tmp_path / "db", "demo"),
        {"pid": 999, "silo": "demo", "db_path": str(tmp_path / "db"), "started_at": 123},
    )
    monkeypatch.setattr("pal._pid_is_running", lambda _pid: False)
    records, err = pal._status_records(str(tmp_path / "db"), None)
    assert err is None
    assert len(records) == 1
    assert records[0]["legacy_lock"] is True


def test_prune_stale_removes_only_dead(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("pal.WATCH_LOCKS_DIR", tmp_path / "watch_locks")
    stale_lock = pal._watch_lock_path(tmp_path / "db", "stale")
    live_lock = pal._watch_lock_path(tmp_path / "db", "live")
    _write_lock(stale_lock, {"version": 2, "pid": 10, "uid": 1, "user": "u", "silo": "stale", "db_path": str(tmp_path / "db")})
    _write_lock(live_lock, {"version": 2, "pid": 11, "uid": 1, "user": "u", "silo": "live", "db_path": str(tmp_path / "db")})
    monkeypatch.setattr("pal._pid_is_running", lambda pid: int(pid) == 11)
    records, _ = pal._status_records(str(tmp_path / "db"), None)
    out = pal._prune_stale_locks(records)
    assert out["removed"] == 1
    assert stale_lock.exists() is False
    assert live_lock.exists() is True


def test_stop_process_same_user_stops_and_removes_lock(monkeypatch, tmp_path: Path):
    lock_path = tmp_path / "watch_locks" / "demo.pid"
    _write_lock(lock_path, {"pid": 321})
    alive = {"running": True}
    signals: list[int] = []

    def _fake_is_running(_pid):
        return alive["running"]

    def _fake_kill(_pid, sig):
        signals.append(sig)
        if sig == pal.signal.SIGTERM:
            alive["running"] = False

    monkeypatch.setattr("pal._pid_is_running", _fake_is_running)
    monkeypatch.setattr("pal._current_uid", lambda: 501)
    monkeypatch.setattr("pal._process_command_signature", lambda _pid: "pal pull /tmp/demo --watch")
    monkeypatch.setattr("pal.os.kill", _fake_kill)
    record = {"lock_path": str(lock_path), "pid": 321, "uid": 501, "silo": "demo"}
    out = pal._stop_watch_process(record, timeout=0.1)
    assert out["status"] == "stopped"
    assert out["lock_removed"] is True
    assert signals == [pal.signal.SIGTERM]
    assert lock_path.exists() is False


def test_stop_process_rejects_uid_mismatch(monkeypatch, tmp_path: Path):
    lock_path = tmp_path / "watch_locks" / "demo.pid"
    _write_lock(lock_path, {"pid": 321})
    monkeypatch.setattr("pal._pid_is_running", lambda _pid: True)
    monkeypatch.setattr("pal._current_uid", lambda: 501)
    monkeypatch.setattr("pal._process_command_signature", lambda _pid: "pal pull /tmp/demo --watch")
    out = pal._stop_watch_process({"lock_path": str(lock_path), "pid": 321, "uid": 777, "silo": "demo"}, timeout=0.1)
    assert out["status"] == "refused_uid_mismatch"
    assert lock_path.exists() is True


def test_stop_process_rejects_signature_mismatch(monkeypatch, tmp_path: Path):
    lock_path = tmp_path / "watch_locks" / "demo.pid"
    _write_lock(lock_path, {"pid": 321})
    monkeypatch.setattr("pal._pid_is_running", lambda _pid: True)
    monkeypatch.setattr("pal._current_uid", lambda: 501)
    monkeypatch.setattr("pal._process_command_signature", lambda _pid: "python /tmp/other_script.py")
    out = pal._stop_watch_process({"lock_path": str(lock_path), "pid": 321, "uid": 501, "silo": "demo"}, timeout=0.1)
    assert out["status"] == "refused_signature_mismatch"
    assert lock_path.exists() is True


def test_stop_process_cleans_dead_lock(monkeypatch, tmp_path: Path):
    lock_path = tmp_path / "watch_locks" / "demo.pid"
    _write_lock(lock_path, {"pid": 321})
    monkeypatch.setattr("pal._pid_is_running", lambda _pid: False)
    out = pal._stop_watch_process({"lock_path": str(lock_path), "pid": 321, "silo": "demo"}, timeout=0.1)
    assert out["status"] == "stale_lock_removed"
    assert out["lock_removed"] is True
    assert lock_path.exists() is False


def test_resolve_stop_target_ambiguous_display_name(monkeypatch):
    records = [
        {"pid": 1, "silo": "alpha", "db_path": "/tmp/db"},
        {"pid": 2, "silo": "beta", "db_path": "/tmp/db"},
    ]
    monkeypatch.setattr(
        "pal._read_llmli_registry",
        lambda _db: {
            "alpha": {"display_name": "Data"},
            "beta": {"display_name": "Data"},
        },
    )
    record, err = pal._resolve_stop_target("data", records)
    assert record is None
    assert err is not None
    assert "ambiguous" in err.lower()
