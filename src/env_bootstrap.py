"""Bootstrap process environment for llmLibrarian.

Goal: stop treating a repo-local `.env` as the default secret store.

Precedence (first match wins; never overwrites existing os.environ values):
1) `LLMLIBRARIAN_ENV_FILE` if set **and the path exists** (explicit path to a KEY=VAL file)
2) `XDG_CONFIG_HOME/llmLibrarian/.llmlibrarian.env` (fallback: `~/.config/llmLibrarian/.llmlibrarian.env`)
3) Legacy user config: `XDG_CONFIG_HOME/llmLibrarian/llmlibrarian.env`
4) Optional legacy: repo-root `.env` ONLY if `LLMLIBRARIAN_DOTENV=1`

This intentionally does **not** depend on importing the rest of llmLibrarian.
"""

from __future__ import annotations

import os
from pathlib import Path


def _parse_dotenv_line(raw_line: str) -> tuple[str, str] | None:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    if "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    if not key or key in os.environ:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return key, value


def load_key_value_file(path: Path) -> int:
    """Load KEY=VAL pairs into os.environ. Returns number of vars set."""
    if not path.exists():
        return 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0

    set_count = 0
    for raw in lines:
        parsed = _parse_dotenv_line(raw)
        if not parsed:
            continue
        k, v = parsed
        os.environ[k] = v
        set_count += 1
    return set_count


def bootstrap_llmlibrarian_env(*, repo_root: Path) -> None:
    """Populate os.environ from safer defaults; idempotent."""
    if os.environ.get("LLMLIBRARIAN_ENV_BOOTSTRAPPED") == "1":
        return

    explicit = (os.environ.get("LLMLIBRARIAN_ENV_FILE") or "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if p.exists():
            load_key_value_file(p)
            os.environ["LLMLIBRARIAN_ENV_BOOTSTRAPPED"] = "1"
            return

    xdg = (os.environ.get("XDG_CONFIG_HOME") or "").strip()
    cfg_home = Path(xdg).expanduser() if xdg else (Path.home() / ".config")
    config_dir = cfg_home / "llmLibrarian"
    for user_env in (
        config_dir / ".llmlibrarian.env",
        config_dir / "llmlibrarian.env",
    ):
        user_env = user_env.expanduser()
        if user_env.exists():
            load_key_value_file(user_env)
            os.environ["LLMLIBRARIAN_ENV_BOOTSTRAPPED"] = "1"
            return

    dotenv_flag = (os.environ.get("LLMLIBRARIAN_DOTENV") or "").strip().lower()
    if dotenv_flag in {"1", "true", "yes", "on"}:
        legacy = (repo_root / ".env").expanduser()
        load_key_value_file(legacy)

    os.environ["LLMLIBRARIAN_ENV_BOOTSTRAPPED"] = "1"
