"""Coverage for the pure helpers behind `pal ls --status`.

Focus: the rendering + action-suggestion functions, since those are the
parts that drive what the user actually sees and is told to do. The
end-to-end command path stitches subprocess + filesystem reads, which
the rest of the suite already covers indirectly.
"""

from __future__ import annotations

import pal


def test_render_health_summary_healthy_when_no_issues():
    out = pal._render_health_summary(
        registry=[{"slug": "docs-1234", "chunks_count": 42}],
        dupes=[],
        overlaps=[],
        mismatches=[],
    )
    assert "State: Healthy" in out
    assert "Silos: 1" in out
    assert "Chunks: 42" in out
    assert "No action needed." in out


def test_render_health_summary_flags_attention_with_any_issue():
    out = pal._render_health_summary(
        registry=[{"slug": "a", "chunks_count": 1}, {"slug": "b", "chunks_count": 2}],
        dupes=[],
        overlaps=[{"type": "same_path", "silos": ["a", "b"], "path": "/x"}],
        mismatches=[],
    )
    assert "State: Needs Attention" in out
    assert "Path overlaps: 1" in out
    assert "Silos: 2" in out
    assert "Chunks: 3" in out
    assert "No action needed." not in out


def test_render_health_summary_handles_empty_registry():
    out = pal._render_health_summary(registry=[], dupes=[], overlaps=[], mismatches=[])
    assert "State: Healthy" in out
    assert "Silos: 0" in out
    assert "Chunks: 0" in out


def test_render_health_summary_tolerates_missing_chunks_count():
    """Some legacy registry rows may lack chunks_count entirely."""
    out = pal._render_health_summary(
        registry=[{"slug": "legacy"}, {"slug": "ok", "chunks_count": 7}],
        dupes=[],
        overlaps=[],
        mismatches=[],
    )
    assert "Silos: 2" in out
    assert "Chunks: 7" in out


def test_render_health_summary_counts_each_issue_class():
    out = pal._render_health_summary(
        registry=[],
        dupes=[{"hash": "h1", "files": ["a", "b"]}, {"hash": "h2", "files": ["c", "d"]}],
        overlaps=[{"type": "same_path", "silos": ["x", "y"], "path": "/p"}],
        mismatches=[{"slug": "z", "registry_files": 5, "manifest_files": 7}],
    )
    assert "Duplicate groups: 2" in out
    assert "Path overlaps: 1" in out
    assert "Count mismatches: 1" in out
    assert "State: Needs Attention" in out


def test_status_action_for_mismatch_eval_silo_with_zero_manifest_recommends_removal():
    action = pal._status_action_for_mismatch(
        slug="__adversarial_eval__",
        registry_files=12,
        manifest_files=0,
        path="/some/eval/path",
    )
    assert action == "remove transient eval silo: pal remove __adversarial_eval__"


def test_status_action_for_mismatch_with_known_path_targets_that_path():
    action = pal._status_action_for_mismatch(
        slug="docs-abcd",
        registry_files=10,
        manifest_files=15,
        path="/home/me/docs",
    )
    assert action == 're-index this source: pal pull --full "/home/me/docs"'


def test_status_action_for_mismatch_without_path_falls_back_to_full():
    action = pal._status_action_for_mismatch(
        slug="docs-abcd",
        registry_files=10,
        manifest_files=15,
        path=None,
    )
    assert action == "re-index all sources: pal pull --full"


def test_status_action_for_mismatch_eval_with_nonzero_manifest_uses_path_path():
    """If the eval silo still has manifest content, treat it like any other mismatch."""
    action = pal._status_action_for_mismatch(
        slug="__adversarial_eval__",
        registry_files=4,
        manifest_files=4,
        path="/eval/scratch",
    )
    assert "remove transient" not in action
    assert "/eval/scratch" in action
