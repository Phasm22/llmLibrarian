"""Coverage for `_normalize_natural_ask_scope` — the parser behind
`pal ask in <scope> <question...>` shorthand.

Existing CLI contract tests cover the happy paths through Typer; this
file unit-tests the parser directly so the malformed/edge branches are
asserted without going through a full CliRunner roundtrip.
"""

from __future__ import annotations

from types import SimpleNamespace
import sys

import pal


def _stub_state(monkeypatch, *, slug_for=None, prefix_for=None, registry=None):
    """Install a stubbed `state` module + registry reader."""
    monkeypatch.setitem(
        sys.modules,
        "state",
        SimpleNamespace(
            resolve_silo_to_slug=lambda _db, name: (slug_for or {}).get(name),
            resolve_silo_prefix=lambda _db, prefix: (prefix_for or {}).get(prefix),
        ),
    )
    monkeypatch.setattr("pal._read_llmli_registry", lambda _db: registry or {})


def test_explicit_in_silo_short_circuits(monkeypatch):
    _stub_state(monkeypatch)
    in_silo, query, err = pal._normalize_natural_ask_scope(
        ["what", "is", "this"],
        explicit_in_silo="docs-1234",
        db_path="/tmp/db",
    )
    assert in_silo == "docs-1234"
    assert query == ["what", "is", "this"]
    assert err is None


def test_short_query_no_in_returns_unchanged(monkeypatch):
    _stub_state(monkeypatch)
    res = pal._normalize_natural_ask_scope(["hi"], None, "/tmp/db")
    assert res == (None, ["hi"], None)


def test_first_token_not_in_returns_unchanged(monkeypatch):
    _stub_state(monkeypatch)
    res = pal._normalize_natural_ask_scope(
        ["explain", "this", "thing"], None, "/tmp/db"
    )
    assert res == (None, ["explain", "this", "thing"], None)


def test_in_my_form_skips_my_filler(monkeypatch):
    _stub_state(monkeypatch, slug_for={"docs": "docs-1234"})
    in_silo, query, err = pal._normalize_natural_ask_scope(
        ["in", "my", "docs", "what", "is", "x"],
        None,
        "/tmp/db",
    )
    assert err is None
    assert in_silo == "docs-1234"
    assert query == ["what", "is", "x"]


def test_in_my_filler_followed_by_only_scope_complains_missing_question(monkeypatch):
    """`pal ask in my docs` — scope but no question — surfaces the missing-question hint."""
    _stub_state(monkeypatch, slug_for={"docs": "docs-1234"})
    in_silo, query, err = pal._normalize_natural_ask_scope(
        ["in", "my", "docs"], None, "/tmp/db"
    )
    assert in_silo is None
    assert err is not None
    assert "Missing question after scope" in err


def test_in_with_empty_token_is_malformed(monkeypatch):
    _stub_state(monkeypatch)
    in_silo, query, err = pal._normalize_natural_ask_scope(
        ["in", "   ", "what"], None, "/tmp/db"
    )
    assert in_silo is None
    assert err is not None
    assert "Malformed scope shorthand" in err


def test_in_scope_with_double_dash_returns_deterministic_hint(monkeypatch):
    _stub_state(monkeypatch)
    in_silo, query, err = pal._normalize_natural_ask_scope(
        ["in", "marketman--quiet", "show", "structure"], None, "/tmp/db"
    )
    assert in_silo is None
    assert err is not None
    assert "Malformed scope token" in err
    assert "marketman--quiet" in err


def test_in_scope_resolved_but_no_remainder_complains(monkeypatch):
    _stub_state(monkeypatch, slug_for={"docs": "docs-1234"})
    in_silo, query, err = pal._normalize_natural_ask_scope(
        ["in", "docs"], None, "/tmp/db"
    )
    assert in_silo is None
    assert err is not None
    assert "Missing question after scope" in err


def test_unresolvable_scope_returns_unchanged_with_no_error(monkeypatch):
    """When the candidate scope can't be resolved, the parser leaves the
    tokens alone so llmli can fall back to its own routing."""
    _stub_state(
        monkeypatch,
        slug_for={},
        prefix_for={},
        registry={},
    )
    in_silo, query, err = pal._normalize_natural_ask_scope(
        ["in", "ghost", "what", "is", "this"], None, "/tmp/db"
    )
    assert in_silo is None
    assert query == ["in", "ghost", "what", "is", "this"]
    assert err is None


def test_prefix_resolution_used_when_exact_misses(monkeypatch):
    """If `resolve_silo_to_slug` returns None but the normalized prefix
    matches, the parser should prefer the prefix result."""
    _stub_state(
        monkeypatch,
        slug_for={},
        prefix_for={"doc": "docs-1234"},
        registry={"docs-1234": {"display_name": "Docs"}},
    )
    in_silo, query, err = pal._normalize_natural_ask_scope(
        ["in", "doc", "tell", "me"], None, "/tmp/db"
    )
    assert err is None
    assert in_silo == "docs-1234"
    assert query == ["tell", "me"]


def test_display_name_alias_match_resolves(monkeypatch):
    """A display_name like 'Job Related Stuff' should resolve from
    the multi-word natural form 'job-related-stuff'."""
    _stub_state(
        monkeypatch,
        slug_for={},
        prefix_for={},
        registry={
            "job-related-stuff-abcd1234": {"display_name": "Job Related Stuff"}
        },
    )
    in_silo, query, err = pal._normalize_natural_ask_scope(
        ["in", "Job", "Related", "Stuff", "what", "are", "my", "skills"],
        None,
        "/tmp/db",
    )
    assert err is None
    assert in_silo == "job-related-stuff-abcd1234"
    assert query == ["what", "are", "my", "skills"]
