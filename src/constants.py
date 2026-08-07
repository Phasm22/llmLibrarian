"""
Centralized constants for llmLibrarian. Shared across ingest, query, and CLI modules.
"""
import os
from pathlib import Path

_TRUTHY = {"1", "true", "yes", "on"}


def env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable.

    One accepted spelling set across the codebase: ``1/true/yes/on`` (case- and
    whitespace-insensitive). Anything else set is false; unset yields ``default``.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
        return default
    return raw.lower() in _TRUTHY


def mcp_auth_token() -> str:
    """Bearer token for the llmLibrarian MCP HTTP server.

    ``LLMLIBRARIAN_MCP_AUTH_TOKEN`` is the name the server, the run script, and
    the env template all use; ``LLMLIBRARIAN_MCP_BEARER_TOKEN`` is the older
    client-side spelling still written by ``pal``. Read both so a token set
    under either name authenticates -- a client that misses the token gets a
    401 it cannot distinguish from "server down".
    """
    for name in ("LLMLIBRARIAN_MCP_AUTH_TOKEN", "LLMLIBRARIAN_MCP_BEARER_TOKEN"):
        tok = os.environ.get(name, "").strip()
        if tok:
            return tok
    return ""


def pid_is_running(pid: int) -> bool:
    """True if ``pid`` names a live process.

    A pid we lack permission to signal is still running, so PermissionError
    counts as alive.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _looks_like_checkout(path: Path) -> bool:
    return (path / "cli.py").exists() and (path / "src").is_dir()


def _iter_editable_roots(site_root: Path) -> list[Path]:
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


def _default_db_path() -> str:
    env_db = os.environ.get("LLMLIBRARIAN_DB", "").strip()
    if env_db:
        return str(Path(env_db).expanduser().resolve())

    cwd = Path.cwd().resolve()
    if _looks_like_checkout(cwd):
        return str((cwd / "my_brain_db").resolve())

    source_root = Path(__file__).resolve().parent.parent
    if _looks_like_checkout(source_root):
        return str((source_root / "my_brain_db").resolve())

    for editable_root in _iter_editable_roots(source_root):
        if _looks_like_checkout(editable_root):
            return str((editable_root / "my_brain_db").resolve())

    return str((source_root / "my_brain_db").resolve())


# Storage
DB_PATH = _default_db_path()
LLMLI_COLLECTION = "llmli"
LLMLI_IMAGE_COLLECTION = "llmli_image"

# Chunking
CHUNK_SIZE = 1000
# ~15% of CHUNK_SIZE; reduces boundary splits vs 10% (100) for sentence/code continuity.
CHUNK_OVERLAP = 150

# Ingestion
ADD_BATCH_SIZE = 256
MAX_WORKERS = 8

# Query defaults
DEFAULT_N_RESULTS = 12
DEFAULT_MODEL = "llama3.1:8b"
# Keep snippets short enough for terminal readability while still showing enough context.
SNIPPET_MAX_LEN = 180
# Limit per-source chunks to avoid a single file dominating answers.
MAX_CHUNKS_PER_FILE = 50
# Distance threshold for default relevance filtering.
# Balanced to avoid noisy hallucination while still allowing useful low-confidence
# responses for moderately related scoped queries.
DEFAULT_RELEVANCE_MAX_DISTANCE = 0.9
