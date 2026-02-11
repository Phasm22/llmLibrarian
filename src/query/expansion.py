"""
Lightweight query expansion for common domain terms.
Keeps the original query intact and appends a small synonym set for recall.
"""

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
