"""
Intent routing: silent classification of queries to choose retrieval strategy.
No CLI flags — all routing is automatic based on query text.
"""
import re

# Intent constants
INTENT_LOOKUP = "LOOKUP"
INTENT_FIELD_LOOKUP = "FIELD_LOOKUP"
INTENT_MONEY_YEAR_TOTAL = "MONEY_YEAR_TOTAL"
INTENT_TAX_QUERY = "TAX_QUERY"
INTENT_PROJECT_COUNT = "PROJECT_COUNT"
INTENT_EVIDENCE_PROFILE = "EVIDENCE_PROFILE"
INTENT_AGGREGATE = "AGGREGATE"
INTENT_REFLECT = "REFLECT"
INTENT_CODE_LANGUAGE = "CODE_LANGUAGE"
INTENT_CAPABILITIES = "CAPABILITIES"
INTENT_FILE_LIST = "FILE_LIST"
INTENT_STRUCTURE = "STRUCTURE"
INTENT_TIMELINE = "TIMELINE"
INTENT_METADATA_ONLY = "METADATA_ONLY"

# EVIDENCE_PROFILE / AGGREGATE: wider retrieval (cap). n_results from CLI is still the final context size.
K_PROFILE_MIN = 48
K_PROFILE_MAX = 128
K_AGGREGATE_MIN = 48
K_AGGREGATE_MAX = 128


def route_intent(query: str) -> str:
    """Silent router: LOOKUP | FIELD_LOOKUP | MONEY_YEAR_TOTAL | TAX_QUERY | PROJECT_COUNT | EVIDENCE_PROFILE | AGGREGATE | REFLECT | CODE_LANGUAGE | CAPABILITIES | FILE_LIST | STRUCTURE."""
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
    # FILE_LIST: explicit file/document listing by year (deterministic catalog query).
    if (
        re.search(r"\b(files?|documents?|docs?)\b", q)
        and re.search(r"\b(20\d{2})(?!\d)\b", q)
        and re.search(r"\b(list|which|what|show|find|from)\b", q)
        and not re.search(r"\b(summary|overview|analy[sz]e|analysis|architecture|design|why|how)\b", q)
    ):
        return INTENT_FILE_LIST
    # METADATA_ONLY: pure metadata aggregation queries (check before STRUCTURE to avoid conflicts)
    if (
        re.search(
            r'\b(how\s+many|count|total|number\s+of)\b',
            q
        )
        and re.search(
            r'\b(files?|documents?|docs?)\b',
            q
        )
        and re.search(
            r'\b(by\s+(?:year|month|quarter|type|extension|folder))\b',
            q
        )
    ):
        return INTENT_METADATA_ONLY
    # Also catch "file counts", "document types", "extension breakdown"
    if re.search(
        r'\b(file\s+counts?|document\s+(?:types?|counts?)|extension\s+breakdown)\b',
        q
    ):
        return INTENT_METADATA_ONLY
    # TIMELINE: temporal sequence queries (deterministic chronological ordering)
    if (
        re.search(
            r'\b(timeline|chronolog|sequence|history|evolution|progression)\b',
            q
        )
        and re.search(r'\b(20\d{2}|events?|milestones?|changes?|updates?)\b', q)
    ):
        return INTENT_TIMELINE
    # STRUCTURE: deterministic catalog snapshots (outline/recent/inventory).
    if (
        re.search(
            r"\b("
            r"structure|folder\s+outline|outline|directory|layout|snapshot|"
            r"recent\s+(?:changes?|files?)|what\s+changed\s+recently|"
            r"file\s+types?|extensions?|inventory"
            r")\b",
            q,
        )
        and re.search(r"\b(files?|folders?|docs?|documents?|structure|changes?|types?|extensions?|inventory)\b", q)
    ):
        return INTENT_STRUCTURE
    # STRUCTURE ext-count: deterministic inventory math (e.g., "how many .docx files").
    if (
        re.search(r"\.(?:[a-z0-9]{1,8})\b", q)
        and re.search(r"\b(how\s+many|count|number\s+of)\b", q)
        and re.search(r"\b(files?|documents?|docs?)\b", q)
    ):
        return INTENT_STRUCTURE
    # CODE_LANGUAGE (year-scoped): explicit language asks only.
    # Guardrails:
    # - exactly one year token
    # - explicit language signal present (don't steal "what was i coding in 2022")
    # - do not steal project-count or explicit file-list asks
    year_tokens = re.findall(r"\b20\d{2}\b", q)
    asks_project_count = bool(re.search(r"\bhow\s+(many|much)\b", q) and re.search(r"\bprojects?\b", q))
    asks_explicit_file_list = bool(
        re.search(r"\b(files?|documents?|docs?)\b", q)
        and re.search(r"\b(list|which|what|show|find|from)\b", q)
    )
    if (
        len(year_tokens) == 1
        and not asks_project_count
        and not asks_explicit_file_list
        and re.search(
            r"\b(?:language|lang)\b|\bwhich\s+language\b",
            q,
        )
    ):
        return INTENT_CODE_LANGUAGE

    # CODE_LANGUAGE: most common / primary / dominant coding language (deterministic count by extension)
    if re.search(
        r"\bmost common\s+(?:coding\s+)?(?:programming\s+)?language\b|\bmost used\s+(?:coding\s+)?language\b|"
        r"\b(?:my )?primary\s+(?:coding\s+)?language\b|\bdominant\s+(?:coding\s+)?language\b|"
        r"\bwhat (?:language|lang)\s+(?:do i\s+)?(?:code|program)\s+(?:in\s+)?(?:the\s+)?most\b|"
        r"\bwhat (?:is|'s)\s+my most common\s+(?:coding\s+)?language\b",
        q,
    ):
        return INTENT_CODE_LANGUAGE
    # FIELD_LOOKUP: tax/form field extraction requests with explicit year + form + line.
    has_year = bool(re.search(r"\b20\d{2}\b", q))
    has_line = bool(re.search(r"\bline\s+\d{1,3}[a-z]?\b", q))
    has_form = bool(
        re.search(r"\bform\s+\d{3,4}(?:-[a-z0-9]+)?\b", q)
        or re.search(r"\b1040(?:-sr)?\b", q)
    )
    if has_year and has_line and has_form:
        return INTENT_FIELD_LOOKUP
    # MONEY_YEAR_TOTAL: year-scoped income question without explicit form/line (e.g., "income in 2024").
    has_income = bool(
        re.search(
            r"\b(income|earnings|wages|agi|adjusted\s+gross|total\s+income)\b",
            q,
        )
    )
    has_make_earn = bool(
        re.search(
            r"\bhow\s+much\b[^\n]{0,80}\b(?:make|made|earn|earned)\b",
            q,
        )
    )
    has_tax_specific = bool(
        re.search(
            r"\b(tax|taxes|withheld|witheld|withholding|federal\s+income\s+tax|federal\s+wages?\s+withh?eld|w-?2|1099|1040|box\s*\d{1,2}|payroll|state\s+tax)\b",
            q,
        )
    )
    has_year_only = bool(re.search(r"\b20\d{2}\b", q))
    lacks_line = not has_line
    lacks_form = not has_form
    if has_year_only and (has_income or has_make_earn) and lacks_line and lacks_form and not has_tax_specific:
        return INTENT_MONEY_YEAR_TOTAL
    if (
        re.search(r"\b(min(?:imum)?|threshold|required|at\s+least)\b", q)
        and re.search(r"\b(file|issue|send|report)\b", q)
        and re.search(r"\b1099\b", q)
    ):
        return INTENT_TAX_QUERY
    # TAX_QUERY: deterministic tax-domain resolver (box lookups, withholding/tax phrasing).
    if has_year_only and re.search(
        r"\b(tax|taxes|withheld|witheld|withholding|federal\s+income\s+tax|federal|w-?2|1099|1040|box\s*\d{1,2}|payroll|state\s+tax)\b",
        q,
    ):
        return INTENT_TAX_QUERY
    # PROJECT_COUNT: how many coding projects in this folder/silo
    if re.search(r"\bhow\s+(many|much)\b", q) and re.search(r"\bprojects?\b", q):
        return INTENT_PROJECT_COUNT
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
