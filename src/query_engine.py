"""
Archetype-aware query: one collection per archetype, retrieval transparency.
Returns answer + "Answered by: <name>" + Sources (path, snippet, line/page).
Optional ANSI styling when stdout is a TTY (see style.use_color).
"""
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings

from embeddings import get_embedding_function
from load_config import load_config, get_archetype, get_archetype_optional
from reranker import RERANK_STAGE1_N, is_reranker_enabled, rerank as rerank_chunks
from style import bold, dim, path_style, label_style, code_style, code_block_style, link_style

try:
    from ingest import LLMLI_COLLECTION
except ImportError:
    LLMLI_COLLECTION = "llmli"

DB_PATH = "./my_brain_db"
DEFAULT_N_RESULTS = 12
DEFAULT_MODEL = "llama3.1:8b"
SNIPPET_MAX_LEN = 180  # one-line preview

# Intent routing: silent, no CLI flags. Used to choose retrieval K and evidence handling.
INTENT_LOOKUP = "LOOKUP"
INTENT_EVIDENCE_PROFILE = "EVIDENCE_PROFILE"
INTENT_AGGREGATE = "AGGREGATE"
INTENT_REFLECT = "REFLECT"

# EVIDENCE_PROFILE / AGGREGATE: wider retrieval (cap). n_results from CLI is still the final context size.
K_PROFILE_MIN = 48
K_PROFILE_MAX = 128
K_AGGREGATE_MIN = 48
K_AGGREGATE_MAX = 128

# Lexical triggers for "what do I like / what did I say / do I mention" — prefer chunks containing these.
PROFILE_TRIGGERS = re.compile(
    r"\b(I like|I love|I enjoy|I prefer|I think|I believe|my favorite|favorite|into|been into|"
    r"I (?:am |was )?(?:really )?into|I (?:would |will )?(?:choose|pick)|"
    r"said (?:that |about)|wrote (?:that |about)|mentioned|according to (?:my|me)|"
    r"in my (?:view|opinion)|I (?:feel|felt)|I (?:want|wanted)|I (?:decided|chose))\b",
    re.IGNORECASE,
)

# Hybrid search: phrases for Chroma where_document $contains (case-sensitive in Chroma). Used for EVIDENCE_PROFILE.
PROFILE_LEXICAL_PHRASES = [
    "I like", "I love", "I enjoy", "I prefer", "I think", "I believe",
    "my favorite", "favorite", "I said", "I wrote", "I feel", "I want",
    "I decided", "I chose", "in my opinion", "I mentioned",
]
RRF_K = 60  # Reciprocal Rank Fusion constant
MAX_LEXICAL_FOR_RRF = 200  # cap lexical get() results for RRF


def _route_intent(query: str) -> str:
    """Silent router: LOOKUP | EVIDENCE_PROFILE | AGGREGATE | REFLECT. No user-facing flags."""
    q = query.strip().lower()
    if not q:
        return INTENT_LOOKUP
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


def _effective_k(intent: str, n_results: int) -> int:
    """Retrieval K from intent; n_results is the cap for final context."""
    if intent == INTENT_EVIDENCE_PROFILE:
        return min(K_PROFILE_MAX, max(K_PROFILE_MIN, n_results))
    if intent == INTENT_AGGREGATE:
        return min(K_AGGREGATE_MAX, max(K_AGGREGATE_MIN, n_results))
    if intent == INTENT_REFLECT:
        return min(24, max(n_results, 12))
    return n_results


def _write_trace(
    intent: str,
    n_stage1: int,
    n_results: int,
    model: str,
    silo: str | None,
    source_label: str,
    num_docs: int,
    time_ms: float,
    query_len: int,
    hybrid_used: bool = False,
) -> None:
    """Append one JSON-line to LLMLIBRARIAN_TRACE file (if set). No-op if env unset. Does not raise."""
    path = os.environ.get("LLMLIBRARIAN_TRACE")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "intent": intent,
                        "n_stage1": n_stage1,
                        "n_results": n_results,
                        "model": model,
                        "silo": silo,
                        "source_label": source_label,
                        "num_docs": num_docs,
                        "time_ms": round(time_ms, 2),
                        "query_len": query_len,
                        "hybrid": hybrid_used,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass


def _rrf_merge(
    ids_v: list[str],
    docs_v: list[str],
    metas_v: list[dict | None],
    dists_v: list[float | None],
    ids_l: list[str],
    docs_l: list[str],
    metas_l: list[dict | None],
    top_k: int,
    k: int = RRF_K,
) -> tuple[list[str], list[dict | None], list[float | None]]:
    """Merge vector and lexical results by RRF score; return top_k (docs, metas, dists) in merged order."""
    rank_v = {vid: 1.0 / (k + (i + 1)) for i, vid in enumerate(ids_v)}
    rank_l = {lid: 1.0 / (k + (i + 1)) for i, lid in enumerate(ids_l)}
    scores: dict[str, float] = {}
    for vid in ids_v:
        scores[vid] = rank_v.get(vid, 0) + rank_l.get(vid, 0)
    for lid in ids_l:
        if lid not in scores:
            scores[lid] = rank_l.get(lid, 0)
    id_to_doc = dict(zip(ids_v, docs_v))
    id_to_meta = dict(zip(ids_v, metas_v))
    id_to_dist = dict(zip(ids_v, dists_v))
    for i, lid in enumerate(ids_l):
        if lid not in id_to_doc:
            id_to_doc[lid] = docs_l[i] if i < len(docs_l) else ""
            id_to_meta[lid] = metas_l[i] if i < len(metas_l) else None
            id_to_dist[lid] = None
    sorted_ids = sorted(scores.keys(), key=lambda x: -scores[x])[:top_k]
    return (
        [id_to_doc[i] for i in sorted_ids],
        [id_to_meta[i] for i in sorted_ids],
        [id_to_dist[i] for i in sorted_ids],
    )


def _filter_by_triggers(docs: list[str], metas: list[dict | None], dists: list[float | None]) -> tuple[list[str], list[dict | None], list[float | None]]:
    """Prefer chunks that contain profile triggers; keep order but put trigger hits first. If none match, return as-is."""
    with_triggers: list[tuple[str, dict | None, float | None]] = []
    without_triggers: list[tuple[str, dict | None, float | None]] = []
    for i, doc in enumerate(docs):
        meta = metas[i] if i < len(metas) else None
        dist = dists[i] if i < len(dists) else None
        if doc and PROFILE_TRIGGERS.search(doc):
            with_triggers.append((doc, meta, dist))
        else:
            without_triggers.append((doc, meta, dist))
    combined = with_triggers + without_triggers
    return (
        [c[0] for c in combined],
        [c[1] for c in combined],
        [c[2] for c in combined],
    )


def _shorten_path(path: str) -> str:
    """Prefer relative path from cwd, else basename, so output is scannable."""
    if not path:
        return "?"
    p = Path(path)
    try:
        rel = p.resolve().relative_to(Path.cwd().resolve())
        return str(rel)
    except ValueError:
        return p.name or path


def _style_answer(answer: str, no_color: bool) -> str:
    """Apply TTY styles: **bold**, *italic*, `code`, fenced code blocks (dim). Respects style.use_color when no_color is False."""
    from style import use_color
    if no_color or not use_color(no_color):
        return answer
    # Fenced code blocks first (opening 2+ backticks; closing 2+ backticks so ```...`` or ```...``` both match)
    def block_repl(m: re.Match) -> str:
        open_fence, lang, body, close_fence = m.group(1), m.group(2) or "", m.group(3), m.group(4)
        open_str = open_fence + (f"{lang}\n" if lang else "\n")
        return (
            code_block_style(no_color, open_str)
            + code_block_style(no_color, body.rstrip())
            + code_block_style(no_color, "\n" + close_fence)
        )
    out = re.sub(r"(`{2,})(\w*)\n([\s\S]*?)(`{2,})", block_repl, answer)
    # **bold** (before single * so we don't break)
    out = re.sub(r"\*\*([^*]+)\*\*", lambda m: bold(no_color, m.group(1)), out)
    # *italic* (single asterisk, content not containing *)
    out = re.sub(r"\*([^*]+)\*", lambda m: dim(no_color, m.group(1)), out)
    # Inline `code`
    out = re.sub(r"`([^`]+)`", lambda m: code_style(no_color, m.group(1)), out)
    return out


def _linkify_sources_in_answer(answer: str, metas: list[dict | None], no_color: bool) -> str:
    """Replace file paths in the answer body with OSC 8 hyperlinks (same as Sources section). Longer path first so 'src/foo.py' is linked before 'foo.py'."""
    from style import link_style
    seen_sources: set[str] = set()
    variants: list[tuple[str, str]] = []  # (path_variant, url)
    for meta in metas or []:
        source = (meta or {}).get("source") or ""
        if not source or source in seen_sources:
            continue
        seen_sources.add(source)
        line = (meta or {}).get("line_start")
        page = (meta or {}).get("page")
        url = _source_url(source, line=line, page=page)
        if not url:
            continue
        short = _shorten_path(source)
        name = Path(source).name
        added = {p for p, _ in variants}
        for v in (source, short, name):
            if v and v not in added:
                variants.append((v, url))
                added.add(v)
    # Sort by length descending so "src/query_engine.py" is replaced before "query_engine.py"
    variants.sort(key=lambda p: -len(p[0]))
    out = answer
    for path_var, url in variants:
        # Replace path occurrences (allow at word/path boundaries). Use lambda so replacement isn't parsed as re template (ANSI has \033).
        pattern = r"(?<![/\w])" + re.escape(path_var) + r"(?![/\w])"
        repl = link_style(no_color, url, path_var)
        out = re.sub(pattern, lambda _: repl, out)
    return out


def _snippet_preview(text: str, max_len: int = SNIPPET_MAX_LEN) -> str:
    """One-line preview: collapse newlines, strip, truncate."""
    if not text:
        return ""
    one = " ".join((text or "").split())
    one = one.strip()
    if len(one) > max_len:
        one = one[: max_len - 1].rstrip() + "…"
    return one


# Extensions we open in the editor (vscode/cursor) with line number; everything else uses file:// so the OS opens in the default app (Word, Preview, etc.).
_EDITOR_EXTENSIONS = frozenset(
    {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
     ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".sh", ".bash", ".zsh",
     ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".java", ".kt", ".rb", ".php",
     ".sql", ".html", ".css", ".scss", ".xml", ".rss", ".svg", ".csv", ".log"}
)


def _use_editor_link(path: Path) -> bool:
    """True if this file type should open in the editor (vscode/cursor) with line number; else use file://."""
    return path.suffix.lower() in _EDITOR_EXTENSIONS


def _source_url(source: str, line: int | None = None, page: int | None = None) -> str:
    """
    Build URL for OSC 8 hyperlink so click opens file (and line when possible).
    Use vscode:// or cursor:// only for code/text extensions (.py, .md, .txt, etc.) so they open in the editor at line.
    For .docx, .pdf, .xlsx, etc. use file:// so the OS opens in the default app (Word, Preview).
    Set LLMLIBRARIAN_EDITOR_SCHEME=vscode|cursor|file (default vscode).
    """
    import os
    if not source or source == "?":
        return ""
    try:
        p = Path(source).resolve()
        posix = p.as_posix()
        scheme = (os.environ.get("LLMLIBRARIAN_EDITOR_SCHEME") or "vscode").strip().lower()
        use_editor = scheme in ("vscode", "cursor") and _use_editor_link(p)
        if use_editor and line is not None:
            prefix = "cursor://file" if scheme == "cursor" else "vscode://file"
            return f"{prefix}/{posix}:{line}:1"
        # file:// for everything else: .docx, .pdf, binaries, or when scheme=file
        url = "file://" + posix if posix.startswith("/") else "file:///" + posix
        if line is not None:
            url = f"{url}#L{line}"
        elif page is not None:
            url = f"{url}#page={page}"
        return url
    except Exception:
        return ""


def _diversify_by_source(
    docs: list[str],
    metas: list[dict | None],
    dists: list[float | None],
    top_k: int,
    max_per_source: int = 2,
) -> tuple[list[str], list[dict | None], list[float | None]]:
    """Keep best chunks by distance but cap at max_per_source per unique source path so one big file doesn't dominate."""
    if not docs or max_per_source < 1:
        return docs[:top_k], (metas or [])[:top_k], (dists or [])[:top_k]
    out_docs: list[str] = []
    out_metas: list[dict | None] = []
    out_dists: list[float | None] = []
    count_per_source: dict[str, int] = {}
    for i, doc in enumerate(docs):
        if len(out_docs) >= top_k:
            break
        meta = metas[i] if i < len(metas) else None
        source = (meta or {}).get("source") or ""
        if not source:
            source = f"__unknown_{i}"
        n = count_per_source.get(source, 0)
        if n >= max_per_source:
            continue
        count_per_source[source] = n + 1
        out_docs.append(doc)
        out_metas.append(meta)
        out_dists.append(dists[i] if i < len(dists) else None)
    return out_docs, out_metas, out_dists


def _format_source(
    doc: str,
    meta: dict | None,
    distance: float | None,
    no_color: bool = True,
) -> str:
    """Format one source: short path (with file:// hyperlink when TTY), line/page, score, and a one-line snippet."""
    source = (meta or {}).get("source") or "?"
    display_path = _shorten_path(source)
    line = (meta or {}).get("line_start")
    page = (meta or {}).get("page")
    snippet = _snippet_preview(doc or "")
    loc = ""
    if page is not None:
        loc = f" (page {page})"
    elif line is not None:
        loc = f" (line {line})"
    score_str = ""
    if distance is not None:
        try:
            score = 1.0 / (1.0 + float(distance))
            score_str = f" · {score:.2f}"
        except (TypeError, ValueError):
            pass
    file_url = _source_url(source, line=line, page=page)
    path_part = link_style(no_color, file_url, display_path)
    meta_part = dim(no_color, f"{loc}{score_str}")
    snippet_part = dim(no_color, snippet)
    return f"  • {path_part}{meta_part}\n    {snippet_part}"


def run_ask(
    archetype_id: str | None,
    query: str,
    config_path: str | Path | None = None,
    n_results: int = DEFAULT_N_RESULTS,
    model: str = DEFAULT_MODEL,
    no_color: bool = False,
    use_reranker: bool | None = None,
    silo: str | None = None,
    db_path: str | Path | None = None,
) -> str:
    """Query archetype's collection, or unified llmli collection if archetype_id is None (optional silo filter)."""
    if use_reranker is None:
        use_reranker = is_reranker_enabled()

    db = str(db_path or DB_PATH)
    use_unified = archetype_id is None

    if use_unified:
        collection_name = LLMLI_COLLECTION
        # Optional: use archetype prompt when asking --in <silo> and that silo id exists in archetypes (prompt-only; no separate collection)
        try:
            config = load_config(config_path)
            arch = get_archetype_optional(config, silo) if silo else None
        except Exception:
            arch = None
        if arch and arch.get("prompt"):
            base_prompt = arch.get("prompt") or "Answer only from the provided context. Be concise."
            system_prompt = (
                base_prompt
                + "\n\nAddress the user as 'you'. If the context does not contain the answer, state that clearly but remain helpful."
            )
            source_label = arch.get("name") or silo or "llmli"
        else:
            system_prompt = (
                "Answer only from the provided context. Be concise. "
                "Address the user as 'you'. If the context does not contain the answer, state that clearly but remain helpful."
            )
            source_label = silo or "llmli"
    else:
        config = load_config(config_path)
        arch = get_archetype(config, archetype_id)
        collection_name = arch["collection"]
        base_prompt = arch.get("prompt") or "Answer only from the provided context. Be concise."
        system_prompt = (
            base_prompt
            + "\n\nAddress the user as 'you' and 'your' (e.g. 'Your 1099', not 'Tandon's 1099'). "
            "If the context does not contain the answer, state that clearly but remain helpful."
        )
        source_label = arch.get("name") or archetype_id

    # Silent intent routing: choose retrieval K and evidence handling (no new CLI flags)
    t0 = time.perf_counter()
    intent = _route_intent(query)
    n_effective = _effective_k(intent, n_results)
    if intent in (INTENT_EVIDENCE_PROFILE, INTENT_AGGREGATE):
        n_stage1 = max(n_effective, RERANK_STAGE1_N if use_reranker else 60)
    elif intent == INTENT_REFLECT:
        n_stage1 = n_effective
    else:
        n_stage1 = RERANK_STAGE1_N if use_reranker else min(100, max(n_results * 5, 60))

    hybrid_used = False

    # Intent-specific prompt suffixes (silent)
    if intent == INTENT_EVIDENCE_PROFILE:
        system_prompt = (
            system_prompt.rstrip()
            + "\n\nBase your answer only on direct quotes from the context. Cite each quote; do not paraphrase preferences or opinions."
        )
    elif intent == INTENT_AGGREGATE:
        system_prompt = (
            system_prompt.rstrip()
            + "\n\nIf the question asks for a list or total across documents, list each item and cite its source."
        )
    elif intent == INTENT_REFLECT:
        system_prompt = (
            system_prompt.rstrip()
            + "\n\nThe user is asking for reflection or analysis; base your answer on the provided context."
        )

    ef = get_embedding_function()
    client = chromadb.PersistentClient(path=db, settings=Settings(anonymized_telemetry=False))
    collection = client.get_or_create_collection(
        name=collection_name,
        embedding_function=ef,
    )

    include_ids = intent == INTENT_EVIDENCE_PROFILE and use_unified and silo
    query_kw: dict = {
        "query_texts": [query],
        "n_results": n_stage1,
        "include": ["documents", "metadatas", "distances"],
    }
    if use_unified and silo:
        query_kw["where"] = {"silo": silo}
    results = collection.query(**query_kw)
    docs = (results.get("documents") or [[]])[0] or []
    metas = (results.get("metadatas") or [[]])[0] or []
    dists = (results.get("distances") or [[]])[0] or []
    # Chroma always returns "ids"; we need them for RRF when doing hybrid
    ids_v = (results.get("ids") or [[]])[0] or [] if include_ids else []

    # EVIDENCE_PROFILE + unified + silo: hybrid search (vector + Chroma where_document, RRF merge)
    if include_ids and ids_v and len(ids_v) == len(docs):
        where_doc = {"$or": [{"$contains": p} for p in PROFILE_LEXICAL_PHRASES]}
        get_kw: dict = {"where_document": where_doc, "include": ["documents", "metadatas"]}
        get_kw["where"] = {"silo": silo}
        try:
            lex = collection.get(**get_kw)
            ids_l = (lex.get("ids") or [])[:MAX_LEXICAL_FOR_RRF]
            docs_l = (lex.get("documents") or [])[:MAX_LEXICAL_FOR_RRF]
            metas_l = (lex.get("metadatas") or [])[:MAX_LEXICAL_FOR_RRF]
            if ids_l:
                docs, metas, dists = _rrf_merge(
                    ids_v, docs, metas, dists,
                    ids_l, docs_l, metas_l,
                    top_k=n_stage1,
                )
                hybrid_used = True
        except Exception:
            pass
    # EVIDENCE_PROFILE fallback: in-app reorder by trigger regex (no hybrid)
    if intent == INTENT_EVIDENCE_PROFILE and docs and not hybrid_used:
        docs, metas, dists = _filter_by_triggers(docs, metas, dists)

    if use_reranker and docs:
        docs, metas, dists = rerank_chunks(query, docs, metas, dists, top_k=n_results, force=True)
    else:
        # Heuristic: when query asks about "stack" or "project", prefer README/overview chunks
        q_lower = query.lower()
        prefer_readme = "stack" in q_lower or "project" in q_lower

        def _rerank_key(item: tuple) -> tuple:
            doc, meta, dist = item
            source = ((meta or {}).get("source") or "").lower()
            is_readme = prefer_readme and "readme" in source
            is_local = (meta or {}).get("is_local", 1)
            return (0 if is_readme else 1, 1 - is_local, dist if dist is not None else 0)
        combined = list(zip(docs, metas, dists))
        combined.sort(key=_rerank_key)
        docs = [c[0] for c in combined]
        metas = [c[1] for c in combined]
        dists = [c[2] for c in combined]

    # Diversify by source: cap chunks per file so one huge file (e.g. Closed Traffic.txt) doesn't crowd out others
    docs, metas, dists = _diversify_by_source(docs, metas, dists, n_results, max_per_source=2)

    if not docs:
        if use_unified:
            return f"No indexed content for {source_label}. Run: llmli add <path>"
        return f"No indexed content for {source_label}. Run: index --archetype {archetype_id}"

    # Prefix each chunk with source path (and line) so the model can say where something is defined without guessing
    def _context_block(doc: str, meta: dict | None) -> str:
        src = (meta or {}).get("source") or "?"
        short = _shorten_path(src)
        line = (meta or {}).get("line_start")
        page = (meta or {}).get("page")
        loc = f" (line {line})" if line is not None else f" (page {page})" if page is not None else ""
        header = f"[{short}{loc}]"
        return f"{header}\n{(doc or '')[:1000]}"
    context = "\n---\n".join(_context_block(docs[i], metas[i] if i < len(metas) else None) for i, d in enumerate(docs) if d)
    user_prompt = f"Using ONLY the following context, answer: {query}\n\nContext:\n{context}"

    import ollama
    response = ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        keep_alive=0,
    )
    answer = (response.get("message") or {}).get("content") or ""
    answer = _style_answer(answer.strip(), no_color)
    answer = _linkify_sources_in_answer(answer, metas, no_color)

    out = [
        answer,
        "",
        dim(no_color, "---"),
        label_style(no_color, f"Answered by: {source_label}"),
        "",
        bold(no_color, "Sources:"),
    ]
    for i, doc in enumerate(docs):
        meta = metas[i] if i < len(metas) else None
        dist = dists[i] if i < len(dists) else None
        out.append(_format_source(doc, meta, dist, no_color=no_color))

    time_ms = (time.perf_counter() - t0) * 1000
    _write_trace(
        intent=intent,
        n_stage1=n_stage1,
        n_results=n_results,
        model=model,
        silo=silo,
        source_label=source_label,
        num_docs=len(docs),
        time_ms=time_ms,
        query_len=len(query),
        hybrid_used=hybrid_used,
    )
    return "\n".join(out)


def main() -> None:
    """CLI entry: librarian.py <archetype_id> <query> (used by cli.py ask)."""
    import sys
    if len(sys.argv) < 3:
        print("Usage: python librarian.py <archetype_id> <query>")
        sys.exit(1)
    archetype_id = sys.argv[1]
    query = " ".join(sys.argv[2:])
    print(run_ask(archetype_id, query))


if __name__ == "__main__":
    main()
