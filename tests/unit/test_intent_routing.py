from query.intent import (
    INTENT_AGGREGATE,
    INTENT_CAPABILITIES,
    INTENT_CODE_LANGUAGE,
    INTENT_EVIDENCE_PROFILE,
    INTENT_FIELD_LOOKUP,
    INTENT_FILE_LIST,
    INTENT_STRUCTURE,
    INTENT_MONEY_YEAR_TOTAL,
    INTENT_TAX_QUERY,
    INTENT_PROJECT_COUNT,
    INTENT_LOOKUP,
    INTENT_REFLECT,
    K_AGGREGATE_MAX,
    K_AGGREGATE_MIN,
    K_PROFILE_MAX,
    K_PROFILE_MIN,
    effective_k,
    route_intent,
)


def test_route_intent_defaults_to_lookup_for_empty_query():
    assert route_intent("   ") == INTENT_LOOKUP


def test_route_intent_capabilities_query():
    assert route_intent("what file types can you index?") == INTENT_CAPABILITIES


def test_route_intent_code_language_query():
    assert route_intent("what is my most common programming language") == INTENT_CODE_LANGUAGE


def test_route_intent_year_scoped_code_language_query():
    assert route_intent("what was i coding in 2022") == INTENT_LOOKUP


def test_route_intent_year_scoped_code_language_alt_phrasing():
    assert route_intent("which language did i code in 2022") == INTENT_CODE_LANGUAGE


def test_route_intent_year_scoped_code_language_does_not_capture_project_count():
    q = "how many coding projects did i do in 2022"
    assert route_intent(q) == INTENT_PROJECT_COUNT


def test_route_intent_year_scoped_code_language_does_not_capture_file_list():
    q = "what files are from 2022"
    assert route_intent(q) == INTENT_FILE_LIST


def test_route_intent_reflect_query():
    assert route_intent("reflect on this") == INTENT_REFLECT


def test_route_intent_evidence_profile_query():
    assert route_intent("what do i like about this project?") == INTENT_EVIDENCE_PROFILE


def test_route_intent_aggregate_query():
    assert route_intent("list every source and total docs") == INTENT_AGGREGATE


def test_route_intent_field_lookup_with_total_keyword():
    q = "on 2024 form 1040 what is line 9 total income"
    assert route_intent(q) == INTENT_FIELD_LOOKUP


def test_route_intent_money_year_total_without_line():
    q = "what was my income in 2024"
    assert route_intent(q) == INTENT_MONEY_YEAR_TOTAL


def test_route_intent_money_year_total_make_phrase():
    q = "how much did i make in 2025 at ymca"
    assert route_intent(q) == INTENT_MONEY_YEAR_TOTAL


def test_route_intent_money_year_total_earn_phrase():
    q = "how much did i earn in 2025"
    assert route_intent(q) == INTENT_MONEY_YEAR_TOTAL


def test_route_intent_tax_query_box_lookup():
    q = "box 2 deloitte 2025"
    assert route_intent(q) == INTENT_TAX_QUERY


def test_route_intent_tax_query_taxes_paid_phrase():
    q = "how much did i pay in taxes at deloitte in 2025"
    assert route_intent(q) == INTENT_TAX_QUERY


def test_route_intent_project_count():
    q = "how many coding projects have i done in this folder"
    assert route_intent(q) == INTENT_PROJECT_COUNT


def test_route_intent_file_list_year_query():
    q = "what files are from 2022"
    assert route_intent(q) == INTENT_FILE_LIST


def test_route_intent_file_list_does_not_capture_summary_queries():
    q = "summary of architecture files from 2022"
    assert route_intent(q) == INTENT_LOOKUP


def test_route_intent_structure_outline_query():
    q = "show structure snapshot for this silo"
    assert route_intent(q) == INTENT_STRUCTURE


def test_route_intent_structure_recent_query():
    q = "what changed recently in my docs"
    assert route_intent(q) == INTENT_STRUCTURE


def test_route_intent_structure_inventory_query():
    q = "file type inventory in this folder"
    assert route_intent(q) == INTENT_STRUCTURE


def test_route_intent_structure_extension_count_query():
    q = "how many .docx files are there"
    assert route_intent(q) == INTENT_STRUCTURE


def test_route_intent_neutral_context_preface_does_not_force_structure():
    q = (
        "Context anchors for Demo: anchors: AGENTS.md, README.md; dominant formats: py, md; themes: tests. "
        "What was I building in this silo?"
    )
    assert route_intent(q) == INTENT_LOOKUP


def test_route_intent_fallback_lookup():
    assert route_intent("explain dependency injection") == INTENT_LOOKUP


def test_effective_k_profile_clamps_to_range():
    assert effective_k(INTENT_EVIDENCE_PROFILE, 1) == K_PROFILE_MIN
    assert effective_k(INTENT_EVIDENCE_PROFILE, 10_000) == K_PROFILE_MAX


def test_effective_k_aggregate_clamps_to_range():
    assert effective_k(INTENT_AGGREGATE, 2) == K_AGGREGATE_MIN
    assert effective_k(INTENT_AGGREGATE, 9_999) == K_AGGREGATE_MAX


def test_effective_k_reflect_clamps_to_range():
    assert effective_k(INTENT_REFLECT, 1) == 12
    assert effective_k(INTENT_REFLECT, 100) == 24


def test_effective_k_lookup_passthrough():
    assert effective_k(INTENT_LOOKUP, 17) == 17
