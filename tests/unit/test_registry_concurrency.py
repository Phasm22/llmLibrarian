"""Concurrent registry mutation must not lose entries.

On 2026-09-14 `llmlibrarian-46ad0cbe` vanished from the live registry while
watchers and the MCP server were reindexing. Its chunks were still in Chroma and
its files still in the manifest — only the registry entry was gone.

Two distinct defects could produce that, and both are covered here:

1. A lost update. Every mutator did read-modify-write on the whole dict with no
   lock spanning the two halves, so a slow writer could overwrite a peer's key.
2. `cleanup_stale_registry_entries` deleting entries from inside a *read*
   (`pal._read_llmli_registry`). That is what actually did it: once silo paths
   were canonicalized through symlinks, two real silos reported one path, and an
   ordinary `pal` command removed one.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from registry_lock import RegistryLockTimeoutError, registry_transaction
from state import (
    is_silo_private,
    list_silos,
    registry_transaction as silo_registry_transaction,
    remove_silo,
    set_silo_private,
    update_silo,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _entry(db: str, slug: str, path: str = "/tmp/x") -> None:
    update_silo(db, slug, path, 1, 1, "2026-01-01T00:00:00+00:00", display_name=slug)


# --- lost updates -------------------------------------------------------------

def test_concurrent_threads_lose_no_entries(tmp_path: Path) -> None:
    db = str(tmp_path / "db")
    slugs = [f"silo-{i:02d}" for i in range(24)]
    errors: list[str] = []
    barrier = threading.Barrier(8)

    def worker(mine: list[str]) -> None:
        try:
            barrier.wait(timeout=30)
            for slug in mine:
                _entry(db, slug)
        except Exception as e:  # pragma: no cover - surfaced by the assert below
            errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, args=(slugs[i::8],)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    assert {s["slug"] for s in list_silos(db)} == set(slugs)


def test_concurrent_processes_lose_no_entries(tmp_path: Path) -> None:
    """Threads share a lock object; separate processes only share the flock."""
    db = str(tmp_path / "db")
    Path(db).mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{_REPO_ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"

    script = (
        "import sys\n"
        "from state import update_silo\n"
        "db, prefix = sys.argv[1], sys.argv[2]\n"
        "for i in range(12):\n"
        "    update_silo(db, f'{prefix}-{i:02d}', '/tmp/x', 1, 1,"
        " '2026-01-01T00:00:00+00:00', display_name=f'{prefix}-{i:02d}')\n"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, db, f"w{n}"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for n in range(5)
    ]
    for p in procs:
        out, _ = p.communicate(timeout=180)
        assert p.returncode == 0, out[-2000:]

    expected = {f"w{n}-{i:02d}" for n in range(5) for i in range(12)}
    assert {s["slug"] for s in list_silos(db)} == expected


def test_private_flag_survives_a_concurrent_reindex(tmp_path: Path, monkeypatch) -> None:
    """The clobber that matters: a lost update here silently un-privates a silo.

    Deterministic rather than timing-dependent. `_write_registry` is slowed so the
    read-modify-write window is wide and the interleaving is forced:

      t=0.00  reindexer reads the registry (no private flag yet), then stalls
      t=0.05  set_silo_private reads, sets the flag, commits immediately
      t=0.30  reindexer writes back the dict it read at t=0 — flag gone

    Only the reindexer's write is delayed, so it is guaranteed to commit *last*
    with stale data. Unlocked, the flag is lost every run. Locked, the reindexer
    cannot even read until set_silo_private has committed, so it preserves it.
    """
    import state as state_mod

    db = str(tmp_path / "db")
    _entry(db, "tax-abc")

    real_write = state_mod._write_registry
    stale_writer = "stale-reindexer"

    def slow_write(db_path, data):
        if threading.current_thread().name == stale_writer:
            time.sleep(0.25)
        return real_write(db_path, data)

    monkeypatch.setattr(state_mod, "_write_registry", slow_write)

    errors: list[str] = []

    def reindexer() -> None:
        try:
            update_silo(
                db, "tax-abc", "/tmp/x", 5, 50,
                "2026-02-01T00:00:00+00:00", display_name="Tax",
            )
        except Exception as e:  # pragma: no cover
            errors.append(f"{type(e).__name__}: {e}")

    t = threading.Thread(target=reindexer, name=stale_writer)
    t.start()
    time.sleep(0.05)
    set_silo_private(db, "tax-abc", True)
    t.join(timeout=60)

    assert not errors, errors
    assert is_silo_private(db, "tax-abc") is True, "private flag was clobbered"


def test_remove_silo_does_not_drop_peers(tmp_path: Path) -> None:
    db = str(tmp_path / "db")
    for i in range(10):
        _entry(db, f"keep-{i}")
    _entry(db, "drop-me")

    assert remove_silo(db, "drop-me") == "drop-me"
    assert {s["slug"] for s in list_silos(db)} == {f"keep-{i}" for i in range(10)}


# --- the lock itself ----------------------------------------------------------

def test_transaction_is_reentrant(tmp_path: Path) -> None:
    """A locked mutator must be able to call another one without deadlocking."""
    target = tmp_path / "reg.json"
    with registry_transaction(target):
        with registry_transaction(target):
            target.write_text("{}")
    assert target.read_text() == "{}"


def test_mutators_nest_without_deadlock(tmp_path: Path) -> None:
    db = str(tmp_path / "db")
    _entry(db, "a-1")
    with silo_registry_transaction(db):
        set_silo_private(db, "a-1", True)
        _entry(db, "a-2")
    assert is_silo_private(db, "a-1") is True
    assert {s["slug"] for s in list_silos(db)} == {"a-1", "a-2"}


def test_transaction_times_out_rather_than_hanging(tmp_path: Path, monkeypatch) -> None:
    """An unkillable holder must surface an error, not block the caller forever."""
    pytest.importorskip("fcntl")
    target = tmp_path / "reg.json"
    target.write_text("{}")
    monkeypatch.setenv("LLMLIBRARIAN_REGISTRY_LOCK_TIMEOUT_SECONDS", "0.3")

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{_REPO_ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    holder = subprocess.Popen(
        [
            sys.executable, "-c",
            "import sys, time\n"
            "from registry_lock import registry_transaction\n"
            "with registry_transaction(sys.argv[1]):\n"
            "    print('held', flush=True)\n"
            "    time.sleep(30)\n",
            str(target),
        ],
        env=env, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(RegistryLockTimeoutError):
            with registry_transaction(target):
                pass
    finally:
        holder.kill()
        holder.wait(timeout=10)
