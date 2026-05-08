from __future__ import annotations

from query.ask.orchestrator import _apply_context_budget


def test_apply_context_budget_keeps_high_ranked_chunks_first(monkeypatch):
    monkeypatch.setattr("query.ask.orchestrator.qc.context_block", lambda doc, _meta, _show: doc)
    docs, metas, dists, exhausted = _apply_context_budget(
        ["A" * 30, "B" * 30, "C" * 30],
        [{}, {}, {}],
        [0.1, 0.2, 0.3],
        budget_tokens=20,  # 80 chars budget
        use_unified=False,
        silo=None,
    )
    assert exhausted is False
    assert docs == ["A" * 30, "B" * 30]
    assert len(metas) == 2
    assert len(dists) == 2


def test_apply_context_budget_reports_exhausted_when_nothing_fits(monkeypatch):
    monkeypatch.setattr("query.ask.orchestrator.qc.context_block", lambda doc, _meta, _show: doc)
    docs, metas, dists, exhausted = _apply_context_budget(
        ["X" * 120],
        [{}],
        [0.1],
        budget_tokens=10,  # 40 chars budget
        use_unified=False,
        silo=None,
    )
    assert exhausted is True
    assert docs == []
    assert metas == []
    assert dists == []
