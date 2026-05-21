"""
Concurrent multi-process regression tests for ChromaDB SQLite ↔ HNSW desync.

A prior single-process test (`tests/integration/test_hnsw_sqlite_consistency.py`)
reproducing the "warm singleton + repair" pattern PASSED, which suggests the
real-world corruption doesn't originate from a single Python process holding two
PersistentClient handles. The leading remaining hypothesis is true cross-process
races: the MCP HTTP server (long-lived `python mcp_server.py`) and the pal
watcher daemon (which spawns `llmli` ingest subprocesses) hit the same
LLMLIBRARIAN_DB persist dir concurrently.

`src/chroma_lock.py` provides a flock-based shared/exclusive coordinator. In
theory this prevents corruption. The tests below try to FALSIFY that theory by
driving real subprocesses against a shared DB and asserting on-disk consistency
through a fresh PersistentClient after each scenario.

Scenarios:
  t1 — two `llmli add` subprocesses on different folders sharing one DB.
  t2 — long-lived "MCP-like" reader subprocess hammering the collection while
       another subprocess runs `llmli repair`.
  t3 — long-running "watcher-like" subprocess that keeps re-running
       `llmli add` on a churning folder while another subprocess runs
       `llmli add --full` against the same path.

Notes on faithfulness to production:
- We do NOT spin up a real fastmcp HTTP server or a real systemd/launchd
  daemon in t2/t3. Both are expensive to install in tests, OS-specific, and
  what matters from Chroma's perspective is the *pattern* of access: a
  long-lived process that holds a PersistentClient and repeatedly calls
  query/get while a second writer process runs ingest/repair. The reader
  subprocess in t2 reproduces exactly that surface using the same code paths
  MCP uses (`chroma_shared_lock` + `collection.query`/`collection.get`).
  t3 reproduces what the daemon actually does on each filesystem event:
  spawn a fresh `llmli` ingest process. Either gate is set with
  LLMLI_RACE_TEST=1 to opt into the heavier scenarios; t1 always runs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

# Reuse fixture + invariant from the single-process regression test.
from tests.integration.test_hnsw_sqlite_consistency import (  # type: ignore[import-not-found]
    _make_fixture,
    assert_silo_hnsw_consistent,
)

import chroma_client
from constants import LLMLI_COLLECTION
from embeddings import get_embedding_function
from state import slugify


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRATCH_ROOT = Path.home() / ".cache" / "llmli_concurrent_subprocess_tests"
_RACE_OPT_IN = os.environ.get("LLMLI_RACE_TEST", "").strip() not in {"", "0", "false", "no"}


# ---------------------------------------------------------------------------
# Scratch dir fixture (same rationale as the sibling test — avoid /tmp because
# ADD_DEFAULT_EXCLUDE filters anything matching "tmp").
# ---------------------------------------------------------------------------


@pytest.fixture
def scratch_dir() -> Path:
    base = _SCRATCH_ROOT / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)
        try:
            _SCRATCH_ROOT.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Helpers for invoking the real CLIs as subprocesses with a shared DB.
# ---------------------------------------------------------------------------


def _subprocess_env(db_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["LLMLIBRARIAN_DB"] = str(db_path.resolve())
    # Keep test output sane and ensure the lock timeout is generous enough that
    # legitimate serialization doesn't spuriously fail the race subprocesses.
    env.setdefault("LLMLIBRARIAN_CHROMA_LOCK_TIMEOUT_SECONDS", "60")
    env.setdefault("TOKENIZERS_PARALLELISM", "false")
    env.setdefault("TQDM_DISABLE", "1")
    # Force a stable PYTHONPATH so the subprocess imports the in-repo modules.
    env["PYTHONPATH"] = (
        f"{_REPO_ROOT}{os.pathsep}{_REPO_ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    )
    return env


def _llmli_cmd(*args: str) -> list[str]:
    # Invoke the CLI module directly so we don't depend on the console-script
    # being installed in the test environment.
    return [sys.executable, str(_REPO_ROOT / "cli.py"), *args]


def _spawn(cmd: list[str], env: dict[str, str], log: Path) -> subprocess.Popen:
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "wb")
    return subprocess.Popen(
        cmd,
        env=env,
        stdout=fh,
        stderr=subprocess.STDOUT,
        cwd=str(_REPO_ROOT),
    )


def _wait(proc: subprocess.Popen, timeout: float) -> int:
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise


def _terminate(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _read_log_tail(log: Path, n: int = 60) -> str:
    if not log.exists():
        return "<no log>"
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"<unreadable log: {exc}>"
    lines = text.splitlines()[-n:]
    return "\n".join(lines)


def _fresh_collection(db_path: Path):
    """
    Drop any in-process singleton and open a brand-new PersistentClient so we
    are asserting on the on-disk state, not on a stale handle.
    """
    chroma_client.release()
    client = chroma_client.get_client(str(db_path.resolve()))
    ef = get_embedding_function(batch_size=8)
    coll = client.get_or_create_collection(name=LLMLI_COLLECTION, embedding_function=ef)
    # Materialise both code paths.
    coll.peek(1)
    coll.query(query_texts=["seed"], n_results=1)
    return coll


# ---------------------------------------------------------------------------
# Long-lived reader subprocess used as a stand-in for the MCP HTTP server.
# Mirrors MCP's pattern: hold a single PersistentClient open and repeatedly
# call query/get under chroma_shared_lock until SIGTERM.
# ---------------------------------------------------------------------------


_READER_SCRIPT = r"""
import os, sys, time, signal
sys.path.insert(0, os.environ["_LLMLI_REPO_ROOT"])
sys.path.insert(0, os.path.join(os.environ["_LLMLI_REPO_ROOT"], "src"))

# Mirror MCP's long-lived-reader behavior: exit when another process bumps
# the Chroma write generation, so a supervisor can restart with fresh state.
os.environ["LLMLIBRARIAN_EXIT_ON_STALE_GENERATION"] = "1"

from chroma_client import get_client
from chroma_lock import chroma_shared_lock
from constants import LLMLI_COLLECTION
from embeddings import get_embedding_function

db_path = os.environ["LLMLIBRARIAN_DB"]
client = get_client(db_path)
ef = get_embedding_function(batch_size=8)
coll = client.get_or_create_collection(name=LLMLI_COLLECTION, embedding_function=ef)
coll.peek(1)
coll.query(query_texts=["warmup"], n_results=1)

_running = True
def _stop(_sig, _frm):
    global _running
    _running = False
signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)

i = 0
while _running:
    try:
        # Refresh client handle each iter — get_client() does the
        # generation check and sys.exit(99)s if the writer has moved on.
        client = get_client(db_path)
        coll = client.get_or_create_collection(name=LLMLI_COLLECTION, embedding_function=ef)
        with chroma_shared_lock(db_path):
            coll.query(query_texts=[f"probe {i}"], n_results=5)
            coll.get(limit=5, include=["metadatas"])
    except SystemExit:
        raise
    except Exception as exc:
        print(f"reader iter {i}: {type(exc).__name__}: {exc}", flush=True)
    i += 1
    time.sleep(0.05)
print(f"reader exiting after {i} iterations", flush=True)
"""


def _spawn_reader(db_path: Path, log: Path) -> subprocess.Popen:
    env = _subprocess_env(db_path)
    env["_LLMLI_REPO_ROOT"] = str(_REPO_ROOT)
    return _spawn([sys.executable, "-c", _READER_SCRIPT], env, log)


# A watcher-stand-in subprocess: loops calling `llmli add` on the same path,
# mimicking what pal_daemon does on filesystem events (spawn fresh ingest).
_WATCHER_SCRIPT = r"""
import os, sys, subprocess, time, signal
repo = os.environ["_LLMLI_REPO_ROOT"]
target = os.environ["_LLMLI_WATCH_TARGET"]
cli = [sys.executable, os.path.join(repo, "cli.py"), "add", target]

_running = True
def _stop(_sig, _frm):
    global _running
    _running = False
signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)

i = 0
while _running:
    try:
        rc = subprocess.call(cli, cwd=repo)
        print(f"watcher iter {i} rc={rc}", flush=True)
    except Exception as exc:
        print(f"watcher iter {i} exc {type(exc).__name__}: {exc}", flush=True)
    i += 1
    time.sleep(0.2)
print(f"watcher exiting after {i} iterations", flush=True)
"""


def _spawn_watcher(db_path: Path, target: Path, log: Path) -> subprocess.Popen:
    env = _subprocess_env(db_path)
    env["_LLMLI_REPO_ROOT"] = str(_REPO_ROOT)
    env["_LLMLI_WATCH_TARGET"] = str(target)
    return _spawn([sys.executable, "-c", _WATCHER_SCRIPT], env, log)


# ---------------------------------------------------------------------------
# t1: two `llmli add` subprocesses racing on the same DB, different folders.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_t1_two_concurrent_llmli_add_subprocesses(scratch_dir: Path) -> None:
    db_path = scratch_dir / "db"
    folder_a = _make_fixture(scratch_dir / "silo_a", n_files=30)
    folder_b = _make_fixture(scratch_dir / "silo_b", n_files=30)
    log_a = scratch_dir / "logs" / "add_a.log"
    log_b = scratch_dir / "logs" / "add_b.log"

    env = _subprocess_env(db_path)
    p_a: subprocess.Popen | None = None
    p_b: subprocess.Popen | None = None
    try:
        p_a = _spawn(_llmli_cmd("add", str(folder_a)), env, log_a)
        # Stagger by a hair so they actually overlap on the chroma init, not the spawn.
        time.sleep(0.05)
        p_b = _spawn(_llmli_cmd("add", str(folder_b)), env, log_b)

        rc_a = _wait(p_a, timeout=600)
        rc_b = _wait(p_b, timeout=600)
        assert rc_a == 0, f"add A failed (rc={rc_a}):\n{_read_log_tail(log_a)}"
        assert rc_b == 0, f"add B failed (rc={rc_b}):\n{_read_log_tail(log_b)}"
    finally:
        _terminate(p_a)
        _terminate(p_b)

    coll = _fresh_collection(db_path)
    slug_a = slugify(folder_a.name, str(folder_a))
    slug_b = slugify(folder_b.name, str(folder_b))
    assert_silo_hnsw_consistent(coll, slug_a, db_path)
    assert_silo_hnsw_consistent(coll, slug_b, db_path)


# ---------------------------------------------------------------------------
# t2: long-lived reader subprocess (MCP-like) concurrently with `llmli repair`.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not _RACE_OPT_IN,
    reason="t2 spawns subprocesses; set LLMLI_RACE_TEST=1 to enable",
)
def test_t2_reader_subprocess_vs_repair(scratch_dir: Path) -> None:
    """
    Cross-process race coverage. With LLMLIBRARIAN_EXIT_ON_STALE_GENERATION=1
    in the reader, `llmli repair` bumps the write generation file, and the
    reader self-exits (rc=99) on its next get_client() rather than SIGSEGV'ing
    against ChromaDB's stale process-global Rust state.
    """
    db_path = scratch_dir / "db"
    folder = _make_fixture(scratch_dir / "silo_t2", n_files=30)
    logs = scratch_dir / "logs"

    env = _subprocess_env(db_path)
    rc = _wait(_spawn(_llmli_cmd("add", str(folder)), env, logs / "t2_seed.log"), timeout=600)
    assert rc == 0, f"baseline ingest failed:\n{_read_log_tail(logs / 't2_seed.log')}"

    slug = slugify(folder.name, str(folder))
    reader: subprocess.Popen | None = None
    repair: subprocess.Popen | None = None
    try:
        reader = _spawn_reader(db_path, logs / "t2_reader.log")
        time.sleep(1.0)
        if reader.poll() is not None:
            pytest.fail(
                f"reader exited prematurely (rc={reader.returncode}):\n"
                f"{_read_log_tail(logs / 't2_reader.log')}"
            )

        repair = _spawn(_llmli_cmd("repair", slug), env, logs / "t2_repair.log")
        rc_repair = _wait(repair, timeout=600)
        assert rc_repair == 0, (
            f"repair failed (rc={rc_repair}):\n{_read_log_tail(logs / 't2_repair.log')}"
        )

        # Reader should detect the generation bump and exit on its own with 99.
        try:
            rc_reader = reader.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _terminate(reader)
            pytest.fail(
                f"reader did not exit after writer activity:\n"
                f"{_read_log_tail(logs / 't2_reader.log')}"
            )
        reader = None  # already exited
        assert rc_reader == 99, (
            f"reader expected to exit 99 on stale generation; got rc={rc_reader}\n"
            f"{_read_log_tail(logs / 't2_reader.log')}"
        )
    finally:
        _terminate(reader)
        _terminate(repair)

    # Final consistency check runs in a fresh subprocess: the pytest process's
    # ChromaDB global runtime has cached state from prior test_t1, and the
    # on-disk segments have been rebuilt by `llmli repair` since then.
    # Opening a "fresh" PersistentClient in this process still reuses that
    # tainted runtime and SIGSEGVs. A subprocess starts cold.
    _subprocess_assert_consistent(db_path, slug)


def _subprocess_assert_consistent(db_path: Path, slug: str) -> None:
    script = r"""
import os, sys
sys.path.insert(0, os.environ["_LLMLI_REPO_ROOT"])
sys.path.insert(0, os.path.join(os.environ["_LLMLI_REPO_ROOT"], "src"))
from chroma_client import get_client
from constants import LLMLI_COLLECTION
from embeddings import get_embedding_function
from silo_audit import verify_silo_hnsw_consistency

db_path = os.environ["LLMLIBRARIAN_DB"]
slug = os.environ["_LLMLI_SLUG"]
client = get_client(db_path)
ef = get_embedding_function(batch_size=8)
coll = client.get_or_create_collection(name=LLMLI_COLLECTION, embedding_function=ef)
report = verify_silo_hnsw_consistency(coll, slug, db_path)
print(f"REPORT: {report}", flush=True)
if not report.get("consistent"):
    sys.exit(2)
"""
    env = _subprocess_env(db_path)
    env["_LLMLI_REPO_ROOT"] = str(_REPO_ROOT)
    env["_LLMLI_SLUG"] = slug
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"subprocess verify failed (rc={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------
# t3: long-running watcher-like subprocess re-indexing on a churning folder,
#     concurrent with a `llmli add --full` subprocess on the same silo path.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not _RACE_OPT_IN,
    reason="t3 spawns a watcher loop and is slow; set LLMLI_RACE_TEST=1 to enable",
)
def test_t3_watcher_loop_vs_full_reindex(scratch_dir: Path) -> None:
    db_path = scratch_dir / "db"
    folder = _make_fixture(scratch_dir / "silo_t3", n_files=30)
    logs = scratch_dir / "logs"

    # Seed first so the watcher loop has something to incrementally update against.
    env = _subprocess_env(db_path)
    rc = _wait(_spawn(_llmli_cmd("add", str(folder)), env, logs / "t3_seed.log"), timeout=600)
    assert rc == 0, f"baseline ingest failed:\n{_read_log_tail(logs / 't3_seed.log')}"

    slug = slugify(folder.name, str(folder))

    watcher: subprocess.Popen | None = None
    full: subprocess.Popen | None = None
    try:
        watcher = _spawn_watcher(db_path, folder, logs / "t3_watcher.log")
        # Let the watcher get into its loop and overlap with the next writer.
        time.sleep(1.5)
        if watcher.poll() is not None:
            pytest.fail(
                f"watcher exited prematurely (rc={watcher.returncode}):\n"
                f"{_read_log_tail(logs / 't3_watcher.log')}"
            )

        # Concurrent churn: mutate a file mid-flight to force the watcher's next
        # incremental pass to actually do work while the --full reindex runs.
        churn = folder / "doc_00.md"
        if churn.exists():
            churn.write_text(
                churn.read_text(encoding="utf-8") + "\n\nadded by t3 race\n",
                encoding="utf-8",
            )

        full = _spawn(_llmli_cmd("add", "--full", str(folder)), env, logs / "t3_full.log")
        rc_full = _wait(full, timeout=900)
        assert rc_full == 0, (
            f"full reindex failed (rc={rc_full}):\n{_read_log_tail(logs / 't3_full.log')}"
        )

        # Let the watcher do at least one more pass against the rebuilt silo.
        time.sleep(2.0)
    finally:
        _terminate(watcher)
        _terminate(full)

    coll = _fresh_collection(db_path)
    assert_silo_hnsw_consistent(coll, slug, db_path)
