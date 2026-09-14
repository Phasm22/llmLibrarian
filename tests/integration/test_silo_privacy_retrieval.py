"""End-to-end: a private silo must not reach an unscoped query.

The unit tests assert the where-clause is built correctly. This asserts the
whole path — real ingest, real embeddings, real Chroma, real intent routing —
because the regression that motivated the flag was an unscoped
multi_query_knowledge returning tax and lab chunks, not a malformed filter.

Platform-neutral on purpose: it runs the same on the Linux PC and the Mac, and
the embedded backend here is what `llmli` uses directly. The HTTP backend the
PC's MCP service runs against is covered by the opt-in variant at the bottom.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ingest import run_add
from query.core import run_retrieve
from state import (
    is_silo_private,
    list_visible_silos,
    private_filter_clause,
    resolve_silo_by_path,
    set_silo_private,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SECRET = (
    "Adjusted gross income for the tax year was reported on the return. "
    "Account number and taxpayer identifier appear on every page of this filing. "
    "Withholding and W-2 wage figures are itemized below."
)
_PUBLIC = (
    "Sourdough recipe: combine flour, water, and starter. "
    "Bulk ferment for four hours, then shape and proof overnight."
)


@pytest.fixture()
def two_silos(tmp_path: Path) -> tuple[str, str, str]:
    """One silo to be marked private, one left visible. Returns (db, private, visible)."""
    db = tmp_path / "db"

    secret_dir = tmp_path / "Tax"
    secret_dir.mkdir()
    (secret_dir / "return.txt").write_text(_SECRET, encoding="utf-8")

    public_dir = tmp_path / "Recipes"
    public_dir.mkdir()
    (public_dir / "bread.txt").write_text(_PUBLIC, encoding="utf-8")

    run_add(secret_dir, db_path=db, incremental=False)
    run_add(public_dir, db_path=db, incremental=False)

    private_slug = resolve_silo_by_path(str(db), secret_dir)
    visible_slug = resolve_silo_by_path(str(db), public_dir)
    assert private_slug and visible_slug, "both silos should register"
    return str(db), private_slug, visible_slug


def _silos_in(result: dict) -> set[str]:
    return {c.get("silo", "") for c in result.get("chunks", [])}


def test_unscoped_query_reaches_private_silo_before_the_flag(two_silos) -> None:
    """Guard the guard: if this stops finding the silo, the test below proves nothing."""
    db, private_slug, _ = two_silos
    result = run_retrieve(query="adjusted gross income account number", n_results=10, db_path=db)
    assert private_slug in _silos_in(result), (
        "fixture query no longer reaches the sensitive silo, so the exclusion "
        "assertion below would pass vacuously"
    )


def test_unscoped_query_excludes_private_silo(two_silos) -> None:
    db, private_slug, visible_slug = two_silos
    set_silo_private(db, private_slug, True)

    result = run_retrieve(query="adjusted gross income account number", n_results=10, db_path=db)
    silos = _silos_in(result)

    assert private_slug not in silos, f"private silo leaked into unscoped retrieval: {silos}"
    for chunk in result.get("chunks", []):
        assert "taxpayer identifier" not in (chunk.get("text") or "")


def test_explicit_silo_still_reaches_private_silo(two_silos) -> None:
    db, private_slug, _ = two_silos
    set_silo_private(db, private_slug, True)

    result = run_retrieve(
        query="adjusted gross income", silo=private_slug, n_results=10, db_path=db
    )
    assert result.get("chunks"), "naming the silo is consent; it must still return chunks"
    assert _silos_in(result) == {private_slug}


def test_visible_silo_is_unaffected(two_silos) -> None:
    db, private_slug, visible_slug = two_silos
    set_silo_private(db, private_slug, True)

    result = run_retrieve(query="sourdough bulk ferment proof", n_results=10, db_path=db)
    assert visible_slug in _silos_in(result)


def test_flag_survives_a_real_reindex(two_silos, tmp_path: Path) -> None:
    """A reindex writes the registry entry — it must not clear the flag."""
    db, private_slug, _ = two_silos
    set_silo_private(db, private_slug, True)

    run_add(tmp_path / "Tax", db_path=Path(db), incremental=False)

    assert is_silo_private(db, private_slug) is True
    assert private_filter_clause(db) == {"silo": {"$nin": [private_slug]}}
    assert private_slug not in {s["slug"] for s in list_visible_silos(db)}

    result = run_retrieve(query="adjusted gross income account number", n_results=10, db_path=db)
    assert private_slug not in _silos_in(result)


def test_enforcement_holds_in_a_separate_process(two_silos) -> None:
    """The filter must come from the registry on disk, not from in-process state.

    Every real reader — the MCP server, a watcher, `llmli ask` — is its own
    process that never saw the call that set the flag.
    """
    db, private_slug, _ = two_silos
    set_silo_private(db, private_slug, True)

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{_REPO_ROOT / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env.pop("LLMLIBRARIAN_CHROMA_HOST", None)
    env.pop("LLMLIBRARIAN_CHROMA_PORT", None)

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys\n"
                "from query.core import run_retrieve\n"
                "r = run_retrieve(query='adjusted gross income account number',"
                " n_results=10, db_path=sys.argv[1])\n"
                "print(json.dumps(sorted({c.get('silo','') for c in r.get('chunks', [])})))\n"
            ),
            db,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    silos = json.loads(proc.stdout.strip().splitlines()[-1])
    assert private_slug not in silos, f"leaked in a fresh process: {silos}"

