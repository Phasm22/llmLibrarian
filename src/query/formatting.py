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

_CWD_RESOLVED = Path.cwd().resolve()


def _use_editor_link(path: Path) -> bool:
    """True if this file type should open in the editor (vscode/cursor) with line number; else use file://."""
    return path.suffix.lower() in _EDITOR_EXTENSIONS


def shorten_path(path: str) -> str:
    """Prefer relative path from cwd, else basename, so output is scannable."""
    if not path:
        return "?"
    p = Path(path)
    try:
        rel = p.resolve().relative_to(_CWD_RESOLVED)
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
    Set LLMLIBRARIAN_EDITOR_SCHEME=vscode|cursor|file (default file).
    """
    if not source or source == "?":
        return ""
    try:
        p = Path(source).resolve()
        posix = p.as_posix()
        quoted = quote(posix, safe="/")
        scheme = (os.environ.get("LLMLIBRARIAN_EDITOR_SCHEME") or "file").strip().lower()
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


_HEADER_META_PATTERN = re.compile(
    r'(?:"|\'|`)?file=(?P<path>[^"\',\n]+?)'
    r'(?:\s+\(line\s+(?P<line>\d+)\)|\s+\(page\s+(?P<page>\d+)\))?'
    r'\s+mtime=[^\s"\']+'
    r'\s+silo=[^\s"\']+'
    r'\s+doc_type=[^\s"\']+'
    r'(?:"|\'|`)?',
    re.IGNORECASE,
)
_FILE_ONLY_PATTERN = re.compile(
    r'(?:"|\'|`)?file=(?P<path>[^"\',\n]+?)'
    r'(?:\s+\(line\s+(?P<line>\d+)\)|\s+\(page\s+(?P<page>\d+)\))?'
    r'(?:"|\'|`)?',
    re.IGNORECASE,
)


def sanitize_answer_metadata_artifacts(answer: str) -> str:
    """
    Remove leaked internal context header artifacts from model output.

    Converts fragments like:
      file=TD-resume.docx (line 24) mtime=2025-08-12 silo=... doc_type=other
    into:
      TD-resume.docx (line 24)
    """
    if not answer:
        return ""

    def _repl(m: re.Match) -> str:
        raw_path = (m.group("path") or "").strip()
        display = shorten_path(raw_path)
        line = m.group("line")
        page = m.group("page")
        if line:
            return f"{display} (line {line})"
        if page:
            return f"{display} (page {page})"
        return display

    out = _HEADER_META_PATTERN.sub(_repl, answer)
    out = _FILE_ONLY_PATTERN.sub(_repl, out)
    return out


_THIRD_PERSON_USER_PATTERNS = (
    (re.compile(r"\b(?:the|this|a|an)\s+patient['’]s\b", re.IGNORECASE), "your"),
    (re.compile(r"\b(?:the|this|a|an)\s+user['’]s\b", re.IGNORECASE), "your"),
    (
        re.compile(
            r"\b(?:[Tt]he|[Tt]his|[Aa]|[Aa]n)\s+patient\s+named\s+"
            r"[A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,3}\b",
        ),
        "you",
    ),
    (
        re.compile(
            r"\b(?:[Tt]he|[Tt]his|[Aa]|[Aa]n)\s+user\s+named\s+"
            r"[A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,3}\b",
        ),
        "you",
    ),
    (re.compile(r"\b(?:the|this|a|an)\s+patient\b", re.IGNORECASE), "you"),
    (re.compile(r"\b(?:the|this|a|an)\s+user\b", re.IGNORECASE), "you"),
)


def normalize_answer_direct_address(answer: str) -> str:
    """
    Normalize common third-person user references to second-person.

    Conservative lexical rewrite only; avoids broader grammatical mutation.
    """
    if not answer:
        return ""
    out = answer

    def _repl(m: re.Match, replacement: str) -> str:
        idx = m.start()
        prefix = m.string[:idx].rstrip()
        sentence_start = (idx == 0) or prefix.endswith((".", "!", "?", "\n"))
        return replacement.capitalize() if sentence_start else replacement

    for pattern, replacement in _THIRD_PERSON_USER_PATTERNS:
        out = pattern.sub(lambda m: _repl(m, replacement), out)
    return out


_UNCERTAINTY_LEAD_PATTERNS = (
    re.compile(r"^\s*based on the provided context,\s*", re.IGNORECASE),
    re.compile(r"^\s*it appears that\s*", re.IGNORECASE),
)
_UNCERTAINTY_SENTENCE_PATTERN = re.compile(
    r"\b("
    r"it appears that|"
    r"it is difficult to|"
    r"without (?:more|additional) (?:information|context)|"
    r"cannot determine|"
    r"unable to determine|"
    r"not explicitly (?:mentioned|shown)|"
    r"likely|possibly|unclear|uncertain"
    r")\b",
    re.IGNORECASE,
)


def normalize_uncertainty_tone(answer: str, has_confidence_banner: bool, strict: bool) -> str:
    """
    Keep uncertainty signaling concise when confidence banner is already shown.
    """
    if not answer:
        return ""
    if not has_confidence_banner or strict:
        return answer

    out = answer.strip()
    for pattern in _UNCERTAINTY_LEAD_PATTERNS:
        out = pattern.sub("", out, count=1)

    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", out) if s.strip()]
    if not sentences:
        return out

    kept: list[str] = []
    caveat: str | None = None
    for sent in sentences:
        if _UNCERTAINTY_SENTENCE_PATTERN.search(sent):
            if caveat is not None:
                continue
            sent = re.sub(r"^\s*based on the provided context,\s*", "", sent, flags=re.IGNORECASE)
            sent = re.sub(r"^\s*it appears that\s*", "", sent, flags=re.IGNORECASE)
            caveat = sent.strip()
            continue
        kept.append(sent)

    joined = " ".join(kept).strip()
    if caveat:
        if joined:
            return f"{joined}\n\nCaveat: {caveat}".strip()
        return f"Caveat: {caveat}"
    return joined or out


_OWNERSHIP_CONFLICT_REWRITES = (
    (
        re.compile(r"\b(?:it|this|that)\s+(?:also\s+)?(?:appears|seems)\s+to\s+be\s+written\s+by\s+someone\s+else\b", re.IGNORECASE),
        "ownership is uncertain",
    ),
    (
        re.compile(r"\bsuggest(?:s|ing)\s+they\s+were\s+written\s+by\s+someone\s+else\b", re.IGNORECASE),
        "ownership is uncertain",
    ),
    (
        re.compile(r"\bwritten\s+by\s+someone\s+else\b", re.IGNORECASE),
        "ownership is uncertain",
    ),
)


def normalize_ownership_claims(answer: str) -> str:
    """
    Reduce obvious ownership-claim contradictions to one consistent uncertainty label.
    """
    if not answer:
        return ""
    out = answer
    lower = out.lower()
    has_authored_claim = bool(
        re.search(r"\b(authored|written by you|your authored|likely authored)\b", lower)
    )
    has_other_claim = bool(
        re.search(r"\b(someone else|written by someone else)\b", lower)
    )
    if not (has_authored_claim and has_other_claim):
        return out
    for pattern, replacement in _OWNERSHIP_CONFLICT_REWRITES:
        out = pattern.sub(replacement, out)
    return out


def normalize_sentence_start(answer: str) -> str:
    """
    Capitalize first alphabetical character in the answer body.
    """
    if not answer:
        return ""
    chars = list(answer)
    for i, ch in enumerate(chars):
        if ch.isalpha():
            chars[i] = ch.upper()
            break
    return "".join(chars)


def normalize_inline_numbered_lists(answer: str) -> str:
    """
    Reflow single-line numbered lists like "1. ... 2. ..." into multi-line lists.
    """
    if not answer:
        return ""
    out_lines: list[str] = []
    for raw in answer.splitlines():
        markers = list(re.finditer(r"(?<!\w)(\d+)\.\s+", raw))
        if len(markers) < 2:
            out_lines.append(raw)
            continue
        prefix = raw[: markers[0].start()].strip()
        if prefix:
            out_lines.append(prefix)
        for idx, match in enumerate(markers):
            start = match.start()
            end = markers[idx + 1].start() if (idx + 1) < len(markers) else len(raw)
            item = raw[start:end].strip()
            if item:
                out_lines.append(item)
    return "\n".join(out_lines)


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
    include_snippet: bool = True,
    no_color: bool = True,
) -> str:
    """Format one source: short path (with file:// hyperlink when TTY), line/page, score, and a one-line snippet."""
    source_val = (meta or {}).get("source") or "?"
    display_path = shorten_path(source_val)
    line = (meta or {}).get("line_start")
    page = (meta or {}).get("page")
    snippet = snippet_preview(doc or "") if include_snippet else ""
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
    if snippet:
        snippet_part = dim(no_color, snippet)
        return f"  \u2022 {path_part}{meta_part}\n    {snippet_part}"
    return f"  \u2022 {path_part}{meta_part}"
