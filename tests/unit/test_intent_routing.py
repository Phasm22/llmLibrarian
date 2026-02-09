from query.intent import (
    INTENT_AGGREGATE,
    INTENT_CAPABILITIES,
    INTENT_CODE_LANGUAGE,
    INTENT_EVIDENCE_PROFILE,
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


def test_route_intent_reflect_query():
    assert route_intent("reflect on this") == INTENT_REFLECT


def test_route_intent_evidence_profile_query():
    assert route_intent("what do i like about this project?") == INTENT_EVIDENCE_PROFILE


def test_route_intent_aggregate_query():
    assert route_intent("list every source and total docs") == INTENT_AGGREGATE


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
