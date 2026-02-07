"""
Archetype-aware query: one collection per archetype, retrieval transparency.
Returns answer + "Answered by: <name>" + Sources (path, snippet, line/page).
Optional ANSI styling when stdout is a TTY (see style.use_color).
"""
import re
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings

from embeddings import get_embedding_function
from load_config import load_config, get_archetype
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
    """Apply TTY styles: **bold**, *italic*, `code`, fenced code blocks (dim)."""
    if no_color:
        return answer
    # Fenced code blocks first (so we don't style inside them)
    def block_repl(m: re.Match) -> str:
        lang, body = m.group(1) or "", m.group(2)
        fence = f"```{lang}\n" if lang else "```\n"
        return (
            code_block_style(no_color, fence)
            + code_block_style(no_color, body.rstrip())
            + code_block_style(no_color, "\n```")
        )
    out = re.sub(r"```(\w*)\n([\s\S]*?)```", block_repl, answer)
    # **bold** (before single * so we don't break)
    out = re.sub(r"\*\*([^*]+)\*\*", lambda m: bold(no_color, m.group(1)), out)
    # *italic* (single asterisk, content not containing *)
    out = re.sub(r"\*([^*]+)\*", lambda m: dim(no_color, m.group(1)), out)
    # Inline `code`
    out = re.sub(r"`([^`]+)`", lambda m: code_style(no_color, m.group(1)), out)
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

    ef = get_embedding_function()
    client = chromadb.PersistentClient(path=db, settings=Settings(anonymized_telemetry=False))
    collection = client.get_or_create_collection(
        name=collection_name,
        embedding_function=ef,
    )

    # Stage 1: fetch enough so we can diversify by source (one big file shouldn't dominate)
    n_stage1 = RERANK_STAGE1_N if use_reranker else min(100, max(n_results * 5, 60))
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

    context = "\n---\n".join((d or "")[:1000] for d in docs if d)
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
