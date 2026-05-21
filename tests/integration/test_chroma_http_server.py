"""
Opt-in: verify concurrent CLI ingest against a local chroma run server.

  LLMLI_CHROMA_SERVER_TEST=1 uv run pytest tests/integration/test_chroma_http_server.py -v
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

_OPT_IN = os.environ.get("LLMLI_CHROMA_SERVER_TEST", "").strip() not in {"", "0", "false", "no"}
pytestmark = pytest.mark.skipif(not _OPT_IN, reason="set LLMLI_CHROMA_SERVER_TEST=1 to run")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRATCH = Path.home() / ".cache" / "llmli_chroma_http_tests"


@pytest.fixture
def chroma_server_env(scratch_db: Path) -> dict[str, str]:
    port = 18000 + (os.getpid() % 1000)
    host = "127.0.0.1"
    env = os.environ.copy()
    env["LLMLIBRARIAN_DB"] = str(scratch_db)
    env["LLMLIBRARIAN_CHROMA_HOST"] = host
    env["LLMLIBRARIAN_CHROMA_PORT"] = str(port)
    env["LLMLIBRARIAN_SKIP_CHROMA_WRITE_PREFLIGHT"] = "1"
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{_REPO_ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    proc = subprocess.Popen(
        [
            str(_REPO_ROOT / ".venv" / "bin" / "chroma"),
            "run",
            "--path",
            str(scratch_db),
            "--host",
            host,
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    env["_chroma_proc"] = str(proc.pid)
    deadline = time.time() + 30
    ok = False
    while time.time() < deadline:
        import urllib.request

        try:
            with urllib.request.urlopen(f"http://{host}:{port}/api/v1/heartbeat", timeout=1) as resp:
                if resp.status == 200:
                    ok = True
                    break
        except Exception:
            time.sleep(0.5)
    if not ok:
        proc.kill()
        pytest.fail("chroma run did not become ready")
    try:
        yield env
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def scratch_db() -> Path:
    base = _SCRATCH / uuid.uuid4().hex
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _llmli_add(folder: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(_REPO_ROOT / "cli.py"), "add", str(folder)]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=str(_REPO_ROOT))


def test_concurrent_adds_over_http(chroma_server_env: dict[str, str], scratch_db: Path, tmp_path: Path):
    from tests.integration.test_hnsw_sqlite_consistency import _make_fixture, assert_silo_hnsw_consistent

    from chroma_client import get_client, release
    from constants import LLMLI_COLLECTION
    from state import slugify

    a = _make_fixture(tmp_path / "alpha")
    b = _make_fixture(tmp_path / "beta")
    env = {k: v for k, v in chroma_server_env.items() if not k.startswith("_")}
    p1 = subprocess.Popen(
        [sys.executable, str(_REPO_ROOT / "cli.py"), "add", str(a)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    p2 = subprocess.Popen(
        [sys.executable, str(_REPO_ROOT / "cli.py"), "add", str(b)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    rc1 = p1.wait(timeout=120)
    rc2 = p2.wait(timeout=120)
    assert rc1 == 0, p1.stdout.read() if p1.stdout else ""
    assert rc2 == 0, p2.stdout.read() if p2.stdout else ""
    release()
    client = get_client(str(scratch_db))
    coll = client.get_or_create_collection(name=LLMLI_COLLECTION)
    for folder in (a, b):
        slug = slugify(folder.name)
        assert_silo_hnsw_consistent(coll, slug, scratch_db)
