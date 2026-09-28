"""
Opt-in: verify concurrent CLI ingest against a local chroma run server.

  LLMLI_CHROMA_SERVER_TEST=1 uv run pytest tests/integration/test_chroma_http_server.py -v
"""
from __future__ import annotations

import json
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

        # Chroma 1.x retired /api/v1 (it answers 410); v2 is the live endpoint.
        # Try v2 first and fall back, so this fixture survives either server.
        for api in ("v2", "v1"):
            try:
                with urllib.request.urlopen(f"http://{host}:{port}/api/{api}/heartbeat", timeout=1) as resp:
                    if resp.status == 200:
                        ok = True
                        break
            except Exception:
                continue
        if ok:
            break
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
    from state import resolve_silo_by_path

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
        # Ask the registry which slug the ingest actually created. `slugify(name)`
        # alone is not that slug: slugs hash "<name>|<path>", so the one-argument
        # form yields an id no silo ever had, every lookup returns zero chunks,
        # and the assertion below fails as "baseline ingest failed" while the
        # ingest was in fact fine.
        slug = resolve_silo_by_path(str(scratch_db), folder)
        assert slug, f"no silo registered for {folder}"
        assert_silo_hnsw_consistent(coll, slug, scratch_db)


def test_private_silo_excluded_over_http(chroma_server_env: dict[str, str], scratch_db: Path, tmp_path: Path):
    """Privacy enforcement must hold on the HTTP backend, not just embedded.

    This is the topology the Linux PC's MCP service actually runs: one
    `chroma run`, every client an HTTP client of it. The embedded path is
    covered by tests/integration/test_silo_privacy_retrieval.py.
    """
    secret = tmp_path / "Tax"
    secret.mkdir()
    (secret / "return.txt").write_text(
        "Adjusted gross income for the tax year was reported on the return. "
        "Account number and taxpayer identifier appear on every page.",
        encoding="utf-8",
    )
    public = tmp_path / "Recipes"
    public.mkdir()
    (public / "bread.txt").write_text(
        "Sourdough recipe: flour, water, starter. Bulk ferment, shape, proof overnight.",
        encoding="utf-8",
    )

    for folder in (secret, public):
        proc = _llmli_add(folder, chroma_server_env)
        assert proc.returncode == 0, proc.stderr[-2000:]

    probe = (
        "import json, sys\n"
        "from state import resolve_silo_by_path, set_silo_private\n"
        "from query.core import run_retrieve\n"
        "db = sys.argv[1]\n"
        "slug = resolve_silo_by_path(db, sys.argv[2])\n"
        "set_silo_private(db, slug, True)\n"
        "r = run_retrieve(query='adjusted gross income account number', n_results=10, db_path=db)\n"
        "unscoped = sorted({c.get('silo','') for c in r.get('chunks', [])})\n"
        "r2 = run_retrieve(query='adjusted gross income', silo=slug, n_results=10, db_path=db)\n"
        "scoped = sorted({c.get('silo','') for c in r2.get('chunks', [])})\n"
        "print(json.dumps({'slug': slug, 'unscoped': unscoped, 'scoped': scoped}))\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", probe, str(scratch_db), str(secret)],
        env=chroma_server_env,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=600,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    out = json.loads(run.stdout.strip().splitlines()[-1])

    assert out["slug"] not in out["unscoped"], f"private silo leaked over HTTP: {out}"
    assert out["scoped"] == [out["slug"]], f"explicit silo= must still reach it: {out}"
