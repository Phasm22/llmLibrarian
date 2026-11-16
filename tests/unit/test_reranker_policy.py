"""Reranker helper contract: decision policy, deterministic order, honest fallback."""
from __future__ import annotations

import pytest

import reranker


class _FakeModel:
    def __init__(self, scores):
        self.scores = scores
        self.calls = 0

    def predict(self, pairs):
        self.calls += 1
        return [self.scores[p[1]] for p in pairs]


@pytest.fixture
def fake_model(monkeypatch):
    model = _FakeModel({})
    monkeypatch.setattr(reranker, "_get_model", lambda _name, _device: model)
    return model


def test_rerank_decision_policy(monkeypatch):
    assert reranker.rerank_decision("anything", False) == "disabled"
    assert reranker.rerank_decision("taxes in 2023", True) == "year_skipped"
    assert reranker.rerank_decision("how do I prune tomatoes", True) == "eligible"
    monkeypatch.setenv("LLMLIBRARIAN_RERANK", "0")
    assert reranker.rerank_decision("how do I prune tomatoes", None) == "disabled"


def test_rerank_order_is_stable_on_ties(fake_model):
    fake_model.scores = {"a": 1.0, "b": 2.0, "c": 1.0, "d": 2.0}
    order, diag = reranker.rerank_order("q", ["a", "b", "c", "d"])
    assert order == [1, 3, 0, 2]
    assert diag["status"] == "applied"
    assert diag["candidates"] == 4


def test_rerank_keeps_distances_with_their_chunk(fake_model):
    fake_model.scores = {"a": 0.1, "b": 0.9, "c": 0.5}
    docs, metas, dists = reranker.rerank(
        "q", ["a", "b", "c"], [{"i": 0}, {"i": 1}, {"i": 2}], [0.2, 0.4, 0.3], top_k=None
    )
    assert docs == ["b", "c", "a"]
    assert [m["i"] for m in metas] == [1, 2, 0]
    assert dists == [0.4, 0.3, 0.2]


def test_rerank_failure_keeps_incoming_order_and_reports(monkeypatch):
    def _boom(_name, _device):
        raise RuntimeError("no model")

    monkeypatch.setattr(reranker, "_get_model", _boom)
    order, diag = reranker.rerank_order("q", ["a", "b"])
    assert order is None
    assert diag["status"] == "failed"
    assert "RuntimeError" in diag["error"]
    docs, _metas, dists = reranker.rerank("q", ["a", "b", "c"], [None] * 3, [0.1, 0.2, 0.3], top_k=2)
    assert docs == ["a", "b"]
    assert dists == [0.1, 0.2]


@pytest.mark.parametrize("transport", ["http", "streamable-http", "sse"])
def test_http_defaults_off_and_forces_cpu(monkeypatch, transport):
    import mcp_server
    monkeypatch.delenv("LLMLIBRARIAN_RERANK", raising=False)
    monkeypatch.setenv("LLMLIBRARIAN_RERANK_DEVICE", "mps")
    mcp_server._configure_reranker_for_transport(transport)
    assert reranker.is_reranker_enabled() is False
    assert reranker._get_reranker_device() == "cpu"
    monkeypatch.setenv("LLMLIBRARIAN_RERANK", "1")
    mcp_server._configure_reranker_for_transport(transport)
    assert reranker.rerank_decision("tomato soup", True) == "eligible"
    assert reranker._get_reranker_device() == "cpu"


def test_stdio_preserves_operator_rerank_policy(monkeypatch):
    import mcp_server
    monkeypatch.setenv("LLMLIBRARIAN_RERANK", "1")
    monkeypatch.setenv("LLMLIBRARIAN_RERANK_DEVICE", "mps")
    mcp_server._configure_reranker_for_transport("stdio")
    assert reranker._get_reranker_device() == "mps"
