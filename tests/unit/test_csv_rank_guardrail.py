from query.guardrails import parse_csv_rank_request, run_csv_rank_lookup_guardrail


class _DummyCollection:
    def __init__(self, docs, metas):
        self._docs = docs
        self._metas = metas

    def get(self, **_kwargs):
        return {"documents": self._docs, "metadatas": self._metas}


def test_parse_csv_rank_request_extracts_rank_and_year():
    req = parse_csv_rank_request("what restaurant was ranked number 1 in 2020")
    assert req == {"rank": "1", "year": "2020"}


def test_run_csv_rank_lookup_guardrail_returns_deterministic_answer():
    coll = _DummyCollection(
        docs=[
            'CSV row 1: Rank=1 | Restaurant=Carmine\'s (Times Square) | Sales=39080335',
            'CSV row 2: Rank=2 | Restaurant=The Boathouse Orlando | Sales=35218364',
        ],
        metas=[
            {"source": "/tmp/2020/Independence100.csv"},
            {"source": "/tmp/2020/Independence100.csv"},
        ],
    )
    out = run_csv_rank_lookup_guardrail(
        collection=coll,
        use_unified=True,
        silo="restaurant",
        subscope_where=None,
        query="what restaurant was ranked number 1 in 2020",
        source_label="restaurant",
        no_color=True,
    )
    assert out is not None
    assert "Rank 1: Carmine's (Times Square)" in out["response"]
    assert out["guardrail_reason"] == "csv_rank_lookup"


def test_run_csv_rank_lookup_guardrail_returns_none_when_no_match():
    coll = _DummyCollection(
        docs=['CSV row 4: Rank=4 | Restaurant=LAVO Italian Restaurant & Nightclub'],
        metas=[{"source": "/tmp/2020/Independence100.csv"}],
    )
    out = run_csv_rank_lookup_guardrail(
        collection=coll,
        use_unified=True,
        silo="restaurant",
        subscope_where=None,
        query="what restaurant was ranked number 1 in 2020",
        source_label="restaurant",
        no_color=True,
    )
    assert out is None
