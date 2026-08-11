"""The vector store must not index its own bookkeeping.

Regression: the only DB exclusion was the literal "my_brain_db/", but
LLMLIBRARIAN_DB is configurable. With a DB anywhere else, its .json
artifacts matched the "*.json" include rule and the store indexed itself —
and because ingesting rewrites the manifest, the watcher saw that as a
fresh change and re-ingested, feeding itself.
"""

from __future__ import annotations

import importlib
import pathlib
import subprocess
import sys

import pytest

from ingest.watch_scan import (
    ADD_DEFAULT_EXCLUDE,
    ADD_DEFAULT_INCLUDE,
    should_index,
)

_DB_ARTIFACTS = [
    "llmli_file_manifest.json",
    "llmli_file_registry.json",
    "llmli_query_health.json",
    "llmli_last_failures.json",
]


@pytest.mark.parametrize("db_root", ["/Users/x/my_brain_db", "/Users/x/brain", "/srv/data/vectors"])
@pytest.mark.parametrize("artifact", _DB_ARTIFACTS)
def test_db_bookkeeping_never_indexed_regardless_of_db_location(db_root, artifact):
    assert should_index(f"{db_root}/{artifact}", ADD_DEFAULT_INCLUDE, ADD_DEFAULT_EXCLUDE) is False


@pytest.mark.parametrize("db_root", ["/Users/x/my_brain_db", "/Users/x/brain"])
def test_image_artifacts_never_indexed(db_root):
    path = f"{db_root}/image_artifacts/223fd0a484cc7f1969c3220392e0db8d.json"
    assert should_index(path, ADD_DEFAULT_INCLUDE, ADD_DEFAULT_EXCLUDE) is False


def test_salvaged_db_copies_are_excluded():
    """A recovery backup beside the live DB, as produced by the 7/28 incident."""
    path = "/Users/x/llmLibrarian/my_brain_db.corrupt-20260728/llmli_query_health.json"
    assert should_index(path, ADD_DEFAULT_INCLUDE, ADD_DEFAULT_EXCLUDE) is False


@pytest.mark.parametrize(
    "path",
    [
        "/Users/x/llmLibrarian/src/query_audit.py",
        "/Users/x/llmLibrarian/package.json",
        "/Users/x/proj/data/results.json",
        "/Users/x/proj/tsconfig.json",
        # Not a DB artifact — just a user file whose name starts similarly.
        "/Users/x/notes/llmli_notes.md",
    ],
)
def test_real_project_files_still_indexed(path):
    """The DB patterns must not swallow ordinary .json or user files."""
    assert should_index(path, ADD_DEFAULT_INCLUDE, ADD_DEFAULT_EXCLUDE) is True


def test_both_scanners_share_one_pattern_list():
    """Drift guard: `llmli add` and the watcher must agree on silo contents.

    They previously held byte-identical copies; a pattern added to one and
    not the other would silently desync ingest from the watcher.
    """
    ingest = importlib.import_module("ingest")
    assert ingest.ADD_DEFAULT_EXCLUDE is ADD_DEFAULT_EXCLUDE
    assert ingest.ADD_DEFAULT_INCLUDE is ADD_DEFAULT_INCLUDE


def test_scan_patterns_stays_import_light():
    """watch_scan exists to keep daemons free of chromadb/torch.

    Checked in a subprocess: mutating sys.modules in-process to test this
    would unload torch and trip a duplicate TORCH_LIBRARY registration when
    a later test re-imports it.
    """
    src = str(pathlib.Path(__file__).resolve().parents[2] / "src")
    code = (
        "import sys; sys.path.insert(0, %r);"
        "import scan_patterns;"
        "heavy = {'chromadb', 'torch', 'sentence_transformers'} & set(sys.modules);"
        "print(sorted(heavy))" % src
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", f"scan_patterns pulled in {out.stdout.strip()}"
