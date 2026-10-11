"""Metadata filter where-clauses and tag post-filtering for retrieval."""
from query.meta_filters import (
    apply_tag_postfilter,
    build_metadata_where_parts,
    meta_matches_tags,
    normalize_tag_filters,
)
from query import retrieve_locked


def test_build_metadata_where_parts_ranges_and_equality():
    parts = build_metadata_where_parts(
        category="dinner",
        main_protein="chicken",
        min_servings=2,
        max_servings=6,
        max_total_time=90,
        course="main",
    )
    assert {"category": "dinner"} in parts
    assert {"main_protein": "chicken"} in parts
    assert {"course": "main"} in parts
    assert {"servings": {"$gte": 2}} in parts
    assert {"servings": {"$lte": 6}} in parts
    assert {"total_time": {"$lte": 90}} in parts


def test_tag_membership_uses_flattened_lists():
    assert meta_matches_tags({"tags": ",comfort,sunday,"}, ["sunday"])
    assert meta_matches_tags({"dietary_flags": ",gluten-free,"}, ["gluten-free"])
    assert not meta_matches_tags({"tags": ",comfort,"}, ["sunday"])
    # Prefix safety: "veg" must not match "vegan"
    assert not meta_matches_tags({"tags": ",vegan,"}, ["veg"])


def test_apply_tag_postfilter_keeps_matching_only():
    docs = ["a", "b", "c"]
    metas = [
        {"tags": ",chicken,quick,"},
        {"tags": ",beef,"},
        {"tags": ",chicken,", "dietary_flags": ",dairy-free,"},
    ]
    dists = [0.1, 0.2, 0.3]
    d, m, dist = apply_tag_postfilter(docs, metas, dists, ["chicken"])
    assert d == ["a", "c"]
    assert len(m) == 2
    assert dist == [0.1, 0.3]


def test_normalize_tag_filters_accepts_string_or_list():
    assert normalize_tag_filters("a, B") == ["a", "b"]
    assert normalize_tag_filters(["A", "b"]) == ["a", "b"]
    assert normalize_tag_filters(None) == []


def test_retrieve_where_clause_includes_metadata_filters(monkeypatch, tmp_path):
    db = str(tmp_path / "db")
    (tmp_path / "db").mkdir()
    captured: list[dict] = []

    class _Coll:
        def query(self, **kw: object) -> dict:
            captured.append(dict(kw))
            return {
                "documents": [["Roast chicken"]],
                "metadatas": [[
                    {
                        "source": "/r/chicken.md",
                        "silo": "recipes",
                        "doc_type": "other",
                        "category": "dinner",
                        "main_protein": "chicken",
                        "servings": 4,
                        "total_time": 90,
                        "tags": ",comfort,sunday,",
                    }
                ]],
                "distances": [[0.2]],
                "ids": [["c1"]],
            }

        def get(self, **kw: object) -> dict:
            return {"documents": [], "metadatas": [], "ids": []}

    class _Client:
        def get_or_create_collection(self, **kw: object) -> _Coll:
            return _Coll()

    monkeypatch.setattr(retrieve_locked, "get_embedding_function", lambda **_: None)
    monkeypatch.setattr(
        retrieve_locked,
        "run_hybrid_retrieve",
        lambda **kw: (
            kw["docs_v"],
            kw["metas_v"],
            kw["dists_v"],
            "vector",
        ),
    )

    result = retrieve_locked.execute_retrieve_chroma_phase(
        db=db,
        intent="LOOKUP",
        query="chicken dinner",
        query_for_retrieval="chicken dinner",
        silo_slug="recipes",
        n_stage1=10,
        n_results=5,
        section=None,
        doc_type=None,
        db_path=db,
        get_chroma_client=lambda _db: _Client(),
        category="dinner",
        main_protein="chicken",
        max_servings=6,
        max_total_time=120,
        tags=["comfort"],
    )

    assert captured, "expected a Chroma query"
    where = captured[0]["where"]
    assert where["$and"]
    assert {"silo": "recipes"} in where["$and"]
    assert {"category": "dinner"} in where["$and"]
    assert {"main_protein": "chicken"} in where["$and"]
    assert {"servings": {"$lte": 6}} in where["$and"]
    assert {"total_time": {"$lte": 120}} in where["$and"]

    assert result["chunks"]
    chunk = result["chunks"][0]
    assert chunk["metadata"]["category"] == "dinner"
    assert chunk["metadata"]["main_protein"] == "chicken"
    assert chunk["metadata"]["tags"] == ["comfort", "sunday"]


def test_tag_filter_drops_non_matching_hits(monkeypatch, tmp_path):
    db = str(tmp_path / "db")
    (tmp_path / "db").mkdir()

    class _Coll:
        def query(self, **kw: object) -> dict:
            return {
                "documents": [["beef stew", "chicken soup"]],
                "metadatas": [[
                    {"source": "/r/beef.md", "silo": "recipes", "tags": ",beef,"},
                    {"source": "/r/chicken.md", "silo": "recipes", "tags": ",chicken,"},
                ]],
                "distances": [[0.1, 0.2]],
                "ids": [["b", "c"]],
            }

        def get(self, **kw: object) -> dict:
            return {"documents": [], "metadatas": [], "ids": []}

    class _Client:
        def get_or_create_collection(self, **kw: object) -> _Coll:
            return _Coll()

    monkeypatch.setattr(retrieve_locked, "get_embedding_function", lambda **_: None)
    monkeypatch.setattr(
        retrieve_locked,
        "run_hybrid_retrieve",
        lambda **kw: (kw["docs_v"], kw["metas_v"], kw["dists_v"], "vector"),
    )

    result = retrieve_locked.execute_retrieve_chroma_phase(
        db=db,
        intent="LOOKUP",
        query="dinner",
        query_for_retrieval="dinner",
        silo_slug="recipes",
        n_stage1=10,
        n_results=5,
        section=None,
        doc_type=None,
        db_path=db,
        get_chroma_client=lambda _db: _Client(),
        tags="chicken",
    )
    assert len(result["chunks"]) == 1
    assert result["chunks"][0]["source"].endswith("chicken.md")
