"""
ANSI styling for CLI output. Respects TTY and no_color flag.
"""
import sys
from typing import Callable

def use_color(no_color: bool = False) -> bool:
    if no_color:
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False

def _wrap(code: str, no_color: bool) -> Callable[[bool, str], str]:
    def fn(no_c: bool, text: str) -> str:
        if no_c or no_color:
            return text
        return f"\033[{code}m{text}\033[0m" if text else text
    return fn

def bold(no_color: bool, text: str) -> str:
    return _wrap("1", no_color)(no_color, text)

def dim(no_color: bool, text: str) -> str:
    return _wrap("2", no_color)(no_color, text)

def path_style(no_color: bool, text: str) -> str:
    if no_color:
        return text
    return f"\033[36m{text}\033[0m"  # cyan


def link_style(no_color: bool, url: str, label: str) -> str:
    """Wrap label in an OSC 8 hyperlink when TTY and not no_color; otherwise return path_style(label)."""
    if no_color or not url or url == "?":
        return path_style(no_color, label)
    try:
        if not sys.stdout.isatty():
            return path_style(True, label)
    except Exception:
        return path_style(True, label)
    # OSC 8: ]8;;URL\ BEL link_text ]8;;\ BEL
    return f"\033]8;;{url}\033\\{path_style(False, label)}\033]8;;\033\\"

def label_style(no_color: bool, text: str) -> str:
    if no_color:
        return text
    return f"\033[1;36m{text}\033[0m"  # bold cyan

def success_style(no_color: bool, text: str) -> str:
    if no_color:
        return text
    return f"\033[32m{text}\033[0m"  # green

def warn_style(no_color: bool, text: str) -> str:
    if no_color:
        return text
    return f"\033[33m{text}\033[0m"  # yellow

def code_style(no_color: bool, text: str) -> str:
    if no_color:
        return text
    return f"\033[2;37m{text}\033[0m"  # dim white

def code_block_style(no_color: bool, text: str) -> str:
    if no_color:
        return text
    return f"\033[2;37m{text}\033[0m"  # dim white
