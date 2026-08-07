"""Process startup: stdout hygiene, env loading, and DB-path resolution.

Import order matters here. ``silence_stdio_noise()`` must run before anything
that pulls in huggingface/transformers/tqdm, because under the stdio transport
this process speaks JSON-RPC over stdout and a single progress bar corrupts the
stream. This module deliberately imports only the standard library so it is
safe to import first.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Env defaults applied before any library that reads them is imported.
_STDIO_QUIET_ENV = {
    "HF_HUB_DISABLE_PROGRESS_BARS": "1",
    "HF_HUB_VERBOSITY": "error",
    "TRANSFORMERS_VERBOSITY": "error",
    "TRANSFORMERS_NO_ADVISORY_WARNINGS": "1",
    "TOKENIZERS_PARALLELISM": "false",
    "TQDM_DISABLE": "1",
}


def silence_stdio_noise() -> None:
    """Keep third-party libraries off stdout.

    Also defaults LLMLIBRARIAN_EXIT_ON_STALE_GENERATION on: this is a long-lived
    reader, so when another process writes the index the safe move is to exit
    and let the supervisor restart us with fresh ChromaDB state rather than
    query through a cached client whose on-disk segments moved.
    """
    for key, value in _STDIO_QUIET_ENV.items():
        os.environ.setdefault(key, value)
    os.environ.setdefault("LLMLIBRARIAN_EXIT_ON_STALE_GENERATION", "1")


def bootstrap_env(repo_root: Path) -> None:
    """Load .env / XDG config into os.environ. Best-effort by design."""
    try:
        from env_bootstrap import bootstrap_llmlibrarian_env

        bootstrap_llmlibrarian_env(repo_root=repo_root)
    except Exception:
        return


def looks_like_checkout(path: Path) -> bool:
    return (path / "cli.py").exists() and (path / "src").is_dir()


def iter_editable_roots(site_root: Path) -> list[Path]:
    """Source roots named by ``*llmlibrarian*.pth`` files (editable installs)."""
    roots: list[Path] = []
    for pth_path in sorted(site_root.glob("*llmlibrarian*.pth")):
        try:
            for raw_line in pth_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith("import "):
                    continue
                candidate = Path(line).expanduser()
                if candidate.exists():
                    roots.append(candidate.resolve())
        except Exception:
            continue
    return roots


def _db_path_from_desktop_settings() -> str | None:
    """Read db_path from the Claude Desktop extension settings file.

    The .mcpb manifest passes ``${user.db_path}``; when Claude Desktop launches
    us without substituting it, the real value is still on disk here.
    """
    candidates = [
        Path.home()
        / "Library/Application Support/Claude/Claude Extensions Settings"
        / "local.mcpb.tjm4.llmlibrarian.json",
    ]
    for settings_file in candidates:
        if not settings_file.exists():
            continue
        try:
            cfg = json.loads(settings_file.read_text())
            db_path = cfg.get("userConfig", {}).get("db_path", "")
            if db_path and not db_path.startswith("${"):
                return str(Path(db_path).resolve())
        except Exception:
            continue
    return None


def resolve_db_path(script_root: Path) -> str:
    """Resolve LLMLIBRARIAN_DB, tolerating unsubstituted host templates.

    Prefers an existing DB in the active checkout over ``script_root``: when
    mcp_server.py is installed into site-packages, falling back to the script
    root creates a hidden DB inside .venv that is easy to miss and easy to bloat.
    """
    raw = os.environ.get("LLMLIBRARIAN_DB", "")
    if raw and not raw.startswith("${"):
        return str(Path(raw).resolve())

    from_settings = _db_path_from_desktop_settings()
    if from_settings:
        return from_settings

    cwd = Path.cwd()
    editable_roots = iter_editable_roots(script_root)

    existing = [
        cwd / "my_brain_db" if looks_like_checkout(cwd) else None,
        *[r / "my_brain_db" for r in editable_roots if looks_like_checkout(r)],
        script_root / "my_brain_db" if looks_like_checkout(script_root) else None,
        Path.home() / "llmLibrarian" / "my_brain_db",
    ]
    for candidate in existing:
        if candidate is not None and candidate.exists():
            return str(candidate.resolve())

    if looks_like_checkout(cwd.resolve()):
        return str((cwd.resolve() / "my_brain_db").resolve())
    for root in editable_roots:
        if looks_like_checkout(root):
            return str((root / "my_brain_db").resolve())
    return str((script_root / "my_brain_db").resolve())
