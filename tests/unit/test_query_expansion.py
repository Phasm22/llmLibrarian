from query.expansion import expand_query


def test_expand_query_appends_known_synonyms():
    expanded = expand_query("income by phone")
    assert expanded.startswith("income by phone ")
    assert "wages" in expanded
    assert "gross income" in expanded
    assert "mobile" in expanded


def test_expand_query_returns_original_when_no_synonyms():
    assert expand_query("project timeline") == "project timeline"
