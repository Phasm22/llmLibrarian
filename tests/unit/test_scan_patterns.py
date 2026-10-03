"""Exclude patterns match whole path segments, not substrings.

Substring matching ("env/" -> "env" in path) silently dropped
environment-setup.md, build_notes.md, distance.py, vendors.md and
Firefox-tips.md from every silo, and env_bootstrap.py from this repo's.
"""
from __future__ import annotations

import pytest

from scan_patterns import ADD_DEFAULT_EXCLUDE, ADD_DEFAULT_INCLUDE, exclude_matches
from watch_scan import should_descend_into_dir, should_index


@pytest.mark.parametrize(
    "path",
    [
        "/n/environment-setup.md",
        "/n/build_notes.md",
        "/n/distance.py",
        "/n/vendors.md",
        "/n/Firefox-tips.md",
        "/repo/src/env_bootstrap.py",
        "/repo/macos/build.sh",
        "/repo/.github/workflows/tests.yml",
    ],
)
def test_names_that_only_contain_a_pattern_are_indexed(path):
    assert should_index(path, ADD_DEFAULT_INCLUDE, ADD_DEFAULT_EXCLUDE)


@pytest.mark.parametrize(
    "path",
    [
        "/r/node_modules/x.js",
        "/r/.venv/lib/a.py",
        "/r/ScribeVenv/lib/a.py",
        "/r/build/out.js",
        "/r/macos/.build/debug/description.json",
        "/r/dist/a.js",
        "/r/.git/config",
        "/r/__pycache__/m.py",
        "/r/.env",
        "/r/.env.local",
        "/r/key.pem",
        "/vault/cortex/a.md",
        "/r/.claude/worktrees/w/a.py",
        "/r/.pytest_cache/README.md",
        "/r/Foo.app/Contents/Info.json",
        "/db/llmli_registry.json",
        "/r/my_brain_db/x.json",
        "/r/Old Firefox Data/x.md",
    ],
)
def test_excluded_paths_stay_excluded(path):
    assert not should_index(path, ADD_DEFAULT_INCLUDE, ADD_DEFAULT_EXCLUDE)


def test_directory_descent_uses_segments():
    assert not should_descend_into_dir("/r/build", ADD_DEFAULT_EXCLUDE)
    assert should_descend_into_dir("/r/buildkite", ADD_DEFAULT_EXCLUDE)
    assert should_descend_into_dir("/r/environments", ADD_DEFAULT_EXCLUDE)


def test_multi_segment_and_glob_patterns():
    assert exclude_matches("/a/.claude/worktrees/x/y.py", "/.claude/worktrees/")
    assert not exclude_matches("/a/.claude/rules/y.md", "/.claude/worktrees/")
    assert exclude_matches("/a/docs/x.md", "docs/*.md")
    assert not exclude_matches("/a/docs/sub/x.md", "docs/*.md")
    assert exclude_matches("/A/NODE_MODULES/x.js", "node_modules/")  # case-insensitive
    assert not exclude_matches("/a/b.py", "")
