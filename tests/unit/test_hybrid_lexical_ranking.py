"""Lexical leg of hybrid retrieval ranks by BM25, not storage order.

Acceptance tests from docs/RCA_HYBRID_LEXICAL_RANKING_RECIPES_SILO.md. The fake
collection returns rows in insertion order, which is what Chroma's filtered
``get`` does — so any test here that passes by accident of order would have
passed before the fix too.
"""
import logging
import re

from query.retrieval import (
    MAX_LEXICAL_FOR_RRF,
    extract_direct_lexical_terms,
    lexical_term_pattern,
    rank_lexical_candidates,
    run_hybrid_retrieve,
)


class FakeCollection:
    """Insertion-ordered stand-in for a Chroma collection: $regex, ids=, where={"silo": ...}."""

    def __init__(self, rows: list[tuple[str, str, dict]]):
        self.rows = rows
        self.get_calls: list[dict] = []

    def count(self) -> int:
        return len(self.rows)

    def get(self, ids=None, where=None, where_document=None, include=None, **_kw):
        self.get_calls.append({"ids": ids, "where": where, "where_document": where_document})
        out = self.rows
        if where and "silo" in where:
            out = [r for r in out if r[2].get("silo") == where["silo"]]
        if where_document is not None:
            rx = re.compile(where_document["$regex"])
            out = [r for r in out if rx.search(r[1])]
        if ids is not None:
            wanted = set(ids)
            out = [r for r in out if r[0] in wanted]
        res: dict = {"ids": [r[0] for r in out]}
        if include and "documents" in include:
            res["documents"] = [r[1] for r in out]
        if include and "metadatas" in include:
            res["metadatas"] = [dict(r[2]) for r in out]
        return res


def _filler(n: int, start: int = 0) -> list[tuple[str, str, dict]]:
    return [(f"f{i}", f"filler text about baking bread number {i}", {"source": "filler.txt"}) for i in range(start, start + n)]


def _hybrid(col, query, ids_v, *, top_k=10, diagnostics=None):
    by_id = {r[0]: r for r in col.rows}
    return run_hybrid_retrieve(
        ids_v=ids_v,
        docs_v=[by_id[i][1] for i in ids_v],
        metas_v=[dict(by_id[i][2]) for i in ids_v],
        dists_v=[0.1 * (n + 1) for n in range(len(ids_v))],
        query_text=query,
        collection=col,
        where_filter=None,
        top_k=top_k,
        diagnostics=diagnostics,
    )


def test_early_stored_common_word_chunk_does_not_outrank_vector_top():
    # "Contents" is stored first and contains only the common word "chicken".
    rows = [("toc", "Table of contents: chicken, soups, breads", {"source": "nelson.pdf"})]
    rows += [(f"c{i}", f"chicken dish {i} with rice", {"source": "nelson.pdf"}) for i in range(8)]
    rows += _filler(60)
    rows.append(("fric", "Chicken Fricassee. Cut up a fowl; fricassee in cream.", {"source": "boston.txt"}))
    col = FakeCollection(rows)

    _docs, metas, _d, method = _hybrid(col, "Chicken Fricassee", ["fric", "f1", "f2"])

    assert method == "hybrid"
    assert metas[0]["source"] == "boston.txt"
    assert metas[0]["_signals"]["vector_rank"] == 1
    assert metas[0]["_signals"]["lexical_rank"] == 1
    toc = next(m for m in metas if m["_signals"]["vector_rank"] is None and m["source"] == "nelson.pdf")
    assert toc["_signals"]["lexical_rank"] > 1


def test_lowercase_term_matches_title_case_and_all_caps():
    rows = _filler(60) + [
        ("t", "Lefse\nRoll thin and bake on a griddle.", {"source": "a"}),
        ("u", "COOKIES, BARS, & LEFSE", {"source": "b"}),
    ]
    col = FakeCollection(rows)

    ids_l, _docs, _metas, diag = rank_lexical_candidates(col, ["lefse"], None)

    assert set(ids_l) == {"t", "u"}
    assert diag["terms_used"] == ["lefse"]
    assert re.search(lexical_term_pattern("lefse"), "LEFSE")


def test_best_match_retained_when_stored_last_beyond_cap():
    weak = [(f"w{i}", f"a julep is mentioned once here {i} " + "padding " * 40, {"source": "w"}) for i in range(MAX_LEXICAL_FOR_RRF + 50)]
    best = ("best", "Mint Julep. Mint julep, Kentucky style: crush mint, add julep sugar.", {"source": "bullock"})
    col = FakeCollection(_filler(2000) + weak + [best])

    ids_l, _docs, _metas, diag = rank_lexical_candidates(col, ["mint", "julep", "kentucky"], None)

    assert ids_l[0] == "best"
    assert len(ids_l) == MAX_LEXICAL_FOR_RRF
    assert diag["candidates"] > MAX_LEXICAL_FOR_RRF


def test_common_words_only_query_is_vector_only_and_says_so():
    rows = [(f"r{i}", f"stir the cup of flour {i}", {"source": "x"}) for i in range(100)]
    col = FakeCollection(rows)
    diag: dict = {}

    _docs, metas, _d, method = _hybrid(col, "What can I make with one cup and flour?", ["r0", "r1"], diagnostics=diag)

    assert method == "vector_only"
    assert all(m["_signals"]["lexical_rank"] is None for m in metas)
    assert diag["status"] == "no_discriminative_terms"
    assert set(diag["terms_dropped"]) == {"cup", "flour"}
    assert all(v.startswith("too_common") for v in diag["terms_dropped"].values())


def test_stopwords_cover_common_function_words():
    terms = extract_direct_lexical_terms("weeknight dinner with ground beef and spinach")
    assert terms == ["weeknight", "dinner", "ground", "beef", "spinach"]
    # Quoted phrases are lowercased too, and repeats collapse across case.
    assert extract_direct_lexical_terms('"Mint Julep" MINT julep') == ["mint julep", "mint", "julep"]


def test_lexical_exception_is_logged_and_falls_back(caplog):
    class Broken(FakeCollection):
        def get(self, **kw):
            raise RuntimeError("regex unsupported")

    col = Broken([("a", "lefse", {"source": "a"})])
    diag: dict = {}
    with caplog.at_level(logging.WARNING, logger="query.retrieval"):
        _docs, metas, _d, method = _hybrid(col, "lefse", ["a"], diagnostics=diag)

    assert method == "vector_only"
    assert metas[0]["_signals"] == {"vector_rank": 1, "lexical_rank": None, "rrf_score": None}
    assert diag["status"] == "error"
    assert "RuntimeError: regex unsupported" in caplog.text


def test_where_filter_scopes_df_and_candidates():
    rows = [(f"a{i}", "lefse recipe", {"silo": "other"}) for i in range(100)]
    rows += _filler(60) + [("mine", "Lefse from grandma", {"silo": "recipes"})]
    for r in rows:
        r[2].setdefault("silo", "recipes")
    col = FakeCollection(rows)

    ids_l, *_rest, diag = rank_lexical_candidates(col, ["lefse"], {"silo": "recipes"})

    assert ids_l == ["mine"]
    assert diag["scope_size"] == 61
    assert all(c["where"] == {"silo": "recipes"} for c in col.get_calls if c["ids"] is None)
