"""
Lightweight query expansion for common domain terms.
Keeps the original query intact and appends a small synonym set for recall.
"""
import re

SYNONYMS: dict[str, list[str]] = {
    "income": ["wages", "salary", "compensation", "earnings", "gross income"],
    "address": ["street", "mailing address", "residence"],
    "phone": ["telephone", "cell", "mobile", "contact number"],
}


def expand_query(query: str) -> str:
    """Append simple domain synonyms. Returns original query when no expansions match."""
    raw = (query or "").strip()
    if not raw:
        return ""
    extras: list[str] = []
    seen = set()
    for token in raw.lower().split():
        for synonym in SYNONYMS.get(token, []):
            s = synonym.strip()
            if not s:
                continue
            if s in seen:
                continue
            seen.add(s)
            extras.append(s)
    if not extras:
        return raw
    return f"{raw} {' '.join(extras)}"


def decompose_temporal_query(query: str) -> list[str] | None:
    """
    Decompose temporal comparison queries into sequential sub-queries.
    Returns None when decomposition not applicable.

    Patterns detected:
    - Year range: "from 2023 to 2024" → ["X in 2023", "X in 2024"]
    - Year vs: "2022 vs 2023" → ["X in 2022", "X in 2023"]
    - Month range: "between January and March 2024" → ["X in January 2024", "X in March 2024"]
    - Multi-part: "What is X and how does Y work" → ["What is X", "how does Y work"]
    """
    q = (query or "").strip()
    if not q:
        return None

    q_lower = q.lower()

    # Pattern 1: Year range (from YYYY to YYYY)
    year_range_match = re.search(r'\bfrom\s+(20\d{2})\s+to\s+(20\d{2})\b', q_lower)
    if year_range_match:
        year1, year2 = year_range_match.group(1), year_range_match.group(2)
        # Extract the subject (everything before "from")
        subject_match = re.search(r'(.+?)\s+from\s+', q_lower)
        if subject_match:
            subject = q[:subject_match.end(1)].strip()
            return [f"{subject} in {year1}", f"{subject} in {year2}"]
        else:
            # Fallback: generic temporal queries
            return [f"in {year1}", f"in {year2}"]

    # Pattern 2: Year comparison (YYYY vs/versus YYYY)
    year_vs_match = re.search(r'\b(20\d{2})\s+(?:vs|versus)\s+(20\d{2})\b', q_lower)
    if year_vs_match:
        year1, year2 = year_vs_match.group(1), year_vs_match.group(2)
        # Extract subject before year
        subject_match = re.search(r'(.+?)\s+' + re.escape(year1), q_lower)
        if subject_match:
            subject = q[:subject_match.end(1)].strip()
            return [f"{subject} in {year1}", f"{subject} in {year2}"]
        else:
            return [f"in {year1}", f"in {year2}"]

    # Pattern 3: Month range (between Month and Month YYYY)
    month_range_match = re.search(
        r'\bbetween\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+and\s+(january|february|march|april|may|june|july|august|september|october|november|december)(?:\s+(20\d{2}))?\b',
        q_lower
    )
    if month_range_match:
        month1, month2 = month_range_match.group(1), month_range_match.group(2)
        year = month_range_match.group(3) or ""
        # Extract subject
        subject_match = re.search(r'(.+?)\s+between\s+', q_lower)
        if subject_match:
            subject = q[:subject_match.end(1)].strip()
            year_suffix = f" {year}" if year else ""
            return [f"{subject} in {month1.capitalize()}{year_suffix}", f"{subject} in {month2.capitalize()}{year_suffix}"]
        else:
            year_suffix = f" {year}" if year else ""
            return [f"in {month1.capitalize()}{year_suffix}", f"in {month2.capitalize()}{year_suffix}"]

    # Pattern 4: Multi-part questions (What is X and how does Y work)
    # Look for " and " that separates two question-like clauses
    and_split = re.split(r'\s+and\s+', q, maxsplit=1)
    if len(and_split) == 2:
        part1, part2 = and_split
        # Check if both parts look like questions (have question words or verbs)
        has_q_word_1 = bool(re.search(r'\b(what|how|why|when|where|who|which)\b', part1.lower()))
        has_q_word_2 = bool(re.search(r'\b(what|how|why|when|where|who|which|does|do|did|is|are|was|were)\b', part2.lower()))
        if has_q_word_1 and has_q_word_2:
            return [part1.strip(), part2.strip()]

    # No decomposition applicable
    return None
