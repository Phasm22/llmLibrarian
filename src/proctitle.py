"""Consistent process titles for the llmLibrarian family.

Format: ``llmLibrarian-<role>[:<detail>...]`` — colon-separated, no spaces,
so one ``pgrep -f llmLibrarian`` finds every process and each segment reads
unambiguously (role, transport/port, silo slug, client app).

setproctitle rewrites the process argv, which is what ps/pgrep/lsof and
terminal tools display. The kernel's p_comm field (top, most menu-bar
monitors) stays "Python" — it is locked by the interpreter binary's own
name and cannot be changed at runtime without shipping a renamed binary.
"""

from __future__ import annotations

import re

_SANITIZE_RE = re.compile(r"[\s/:]+")


def set_process_title(*parts: object) -> None:
    """Set the title to ``llmLibrarian-part[:part...]``; silent no-op without setproctitle.

    Falsy/None parts are dropped; whitespace, slashes, and colons inside a
    part collapse to ``-`` so each colon in the final title is a real
    separator.
    """
    try:
        import setproctitle
    except ImportError:
        return
    clean = [
        _SANITIZE_RE.sub("-", str(p).strip())
        for p in parts
        if p is not None and str(p).strip()
    ]
    if not clean:
        return
    setproctitle.setproctitle("llmLibrarian-" + ":".join(clean))
