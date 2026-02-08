"""
Output formatting: path shortening, answer styling, source linkification,
source URL generation, and snippet previews.
"""
import os
import re
from urllib.parse import quote
from pathlib import Path

from constants import SNIPPET_MAX_LEN
from style import bold, dim, code_style, code_block_style, link_style


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


def shorten_path(path: str) -> str:
    """Prefer relative path from cwd, else basename, so output is scannable."""
    if not path:
        return "?"
    p = Path(path)
    try:
        rel = p.resolve().relative_to(Path.cwd().resolve())
        return str(rel)
    except ValueError:
        return p.name if p.name else path


def snippet_preview(text: str, max_len: int = SNIPPET_MAX_LEN) -> str:
    """One-line preview: collapse newlines, strip, truncate."""
    if not text:
        return ""
    one = " ".join((text or "").split())
    one = one.strip()
    if len(one) > max_len:
        one = one[: max_len - 1].rstrip() + "\u2026"
    return one


def source_url(source: str, line: int | None = None, page: int | None = None) -> str:
    """
    Build URL for OSC 8 hyperlink so click opens file (and line when possible).
    Use vscode:// or cursor:// only for code/text extensions (.py, .md, .txt, etc.) so they open in the editor at line.
    For .docx, .pdf, .xlsx, etc. use file:// so the OS opens in the default app (Word, Preview).
    Set LLMLIBRARIAN_EDITOR_SCHEME=vscode|cursor|file (default vscode).
    """
    if not source or source == "?":
        return ""
    try:
        p = Path(source).resolve()
        posix = p.as_posix()
        quoted = quote(posix, safe="/")
        scheme = (os.environ.get("LLMLIBRARIAN_EDITOR_SCHEME") or "vscode").strip().lower()
        use_editor = scheme in ("vscode", "cursor") and _use_editor_link(p)
        if use_editor and line is not None:
            prefix = "cursor://file" if scheme == "cursor" else "vscode://file"
            return f"{prefix}/{quoted}:{line}:1"
        # file:// for everything else: .docx, .pdf, binaries, or when scheme=file
        url = "file://" + quoted if posix.startswith("/") else "file:///" + quoted
        if line is not None:
            url = f"{url}#L{line}"
        elif page is not None:
            url = f"{url}#page={page}"
        return url
    except Exception:
        return ""


def style_answer(answer: str, no_color: bool) -> str:
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


def linkify_sources_in_answer(answer: str, metas: list[dict | None], no_color: bool) -> str:
    """Replace file paths in the answer body with OSC 8 hyperlinks (same as Sources section). Longer path first so 'src/foo.py' is linked before 'foo.py'."""
    seen_sources: set[str] = set()
    variants: list[tuple[str, str]] = []  # (path_variant, url)
    for meta in metas or []:
        source_val = (meta or {}).get("source") or ""
        if not source_val or source_val in seen_sources:
            continue
        seen_sources.add(source_val)
        line = (meta or {}).get("line_start")
        page = (meta or {}).get("page")
        url = source_url(source_val, line=line, page=page)
        if not url:
            continue
        short = shorten_path(source_val)
        name = Path(source_val).name
        added = {p for p, _ in variants}
        for v in (source_val, short, name):
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


def format_source(
    doc: str,
    meta: dict | None,
    distance: float | None,
    no_color: bool = True,
) -> str:
    """Format one source: short path (with file:// hyperlink when TTY), line/page, score, and a one-line snippet."""
    source_val = (meta or {}).get("source") or "?"
    display_path = shorten_path(source_val)
    line = (meta or {}).get("line_start")
    page = (meta or {}).get("page")
    snippet = snippet_preview(doc or "")
    loc = ""
    if page is not None:
        loc = f" (page {page})"
    elif line is not None:
        loc = f" (line {line})"
    score_str = ""
    if distance is not None:
        try:
            score = 1.0 / (1.0 + float(distance))
            score_str = f" \u00b7 {score:.2f}"
        except (TypeError, ValueError):
            pass
    file_url = source_url(source_val, line=line, page=page)
    path_part = link_style(no_color, file_url, display_path)
    meta_part = dim(no_color, f"{loc}{score_str}")
    snippet_part = dim(no_color, snippet)
    return f"  \u2022 {path_part}{meta_part}\n    {snippet_part}"
