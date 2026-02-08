"""
Intent routing: silent classification of queries to choose retrieval strategy.
No CLI flags — all routing is automatic based on query text.
"""
import re

# Intent constants
INTENT_LOOKUP = "LOOKUP"
INTENT_EVIDENCE_PROFILE = "EVIDENCE_PROFILE"
INTENT_AGGREGATE = "AGGREGATE"
INTENT_REFLECT = "REFLECT"
INTENT_CODE_LANGUAGE = "CODE_LANGUAGE"
INTENT_CAPABILITIES = "CAPABILITIES"

# EVIDENCE_PROFILE / AGGREGATE: wider retrieval (cap). n_results from CLI is still the final context size.
K_PROFILE_MIN = 48
K_PROFILE_MAX = 128
K_AGGREGATE_MIN = 48
K_AGGREGATE_MAX = 128


def route_intent(query: str) -> str:
    """Silent router: LOOKUP | EVIDENCE_PROFILE | AGGREGATE | REFLECT | CODE_LANGUAGE | CAPABILITIES. No user-facing flags."""
    q = query.strip().lower()
    if not q:
        return INTENT_LOOKUP
    # CAPABILITIES: supported file types / formats / what can you index (source of truth, no RAG)
    if re.search(
        r"\bsupported\s+(?:file\s+)?(?:types?|formats?)\b|\bwhat\s+(?:file\s+)?(?:types?|formats?)\b|"
        r"\bwhat\s+can\s+you\s+index\b|\bcapabilities\b|\bwhat\s+formats?\b",
        q,
    ):
        return INTENT_CAPABILITIES
    # CODE_LANGUAGE: most common / primary / dominant coding language (deterministic count by extension)
    if re.search(
        r"\bmost common\s+(?:coding\s+)?(?:programming\s+)?language\b|\bmost used\s+(?:coding\s+)?language\b|"
        r"\b(?:my )?primary\s+(?:coding\s+)?language\b|\bdominant\s+(?:coding\s+)?language\b|"
        r"\bwhat (?:language|lang)\s+(?:do i\s+)?(?:code|program)\s+(?:in\s+)?(?:the\s+)?most\b|"
        r"\bwhat (?:is|'s)\s+my most common\s+(?:coding\s+)?language\b",
        q,
    ):
        return INTENT_CODE_LANGUAGE
    # REFLECT: analyze / reflect on (often short or pasted)
    if re.search(r"\breflect\b|\bsummarize\s+this\b|\banalyze\s+this\b", q) and len(q) < 120:
        return INTENT_REFLECT
    # EVIDENCE_PROFILE: preferences, "what do I like", "what did I say about", "do I mention"
    if re.search(
        r"\bwhat (?:do i|did i) (?:like|love|enjoy|say|think|decide)\b|\bdo i (?:mention|say|like)\b|"
        r"\b(?:my )?(?:preferences?|favorites?|opinions?)\b|\bwhat (?:have i|did i) (?:written|said)\b",
        q,
    ):
        return INTENT_EVIDENCE_PROFILE
    # AGGREGATE: totals, lists across docs
    if re.search(
        r"\ball (?:my )?(?:income|sources|documents|files)\b|\btotal\b|\bsum\s+of\b|\blist\s+(?:every|all)\b|"
        r"\bhow many\b|\bevery\s+(?:source|W2|1099)\b",
        q,
    ):
        return INTENT_AGGREGATE
    return INTENT_LOOKUP


def effective_k(intent: str, n_results: int) -> int:
    """Retrieval K from intent; n_results is the cap for final context."""
    if intent == INTENT_EVIDENCE_PROFILE:
        return min(K_PROFILE_MAX, max(K_PROFILE_MIN, n_results))
    if intent == INTENT_AGGREGATE:
        return min(K_AGGREGATE_MAX, max(K_AGGREGATE_MIN, n_results))
    if intent == INTENT_REFLECT:
        return min(24, max(n_results, 12))
    return n_results
