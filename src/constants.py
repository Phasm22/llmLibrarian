"""
Centralized constants for llmLibrarian. Shared across ingest, query, and CLI modules.
"""
import os
from pathlib import Path


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
# Markdown files at or under this size (chars, after frontmatter strip) become one chunk.
# Override with LLMLIBRARIAN_MD_SINGLE_CHUNK_MAX. Larger .md splits on headings first.
MD_SINGLE_CHUNK_MAX = 4000

# Ingestion
ADD_BATCH_SIZE = 256
MAX_WORKERS = 8

# Query defaults
DEFAULT_N_RESULTS = 12
DEFAULT_MODEL = "llama3.2:latest"


def query_keep_alive() -> str | int:
    """Ollama residency for query-time generation; 0 preserves unload-after-use."""
    value = os.environ.get("LLMLIBRARIAN_KEEP_ALIVE", "").strip()
    return value or 0


# Keep snippets short enough for terminal readability while still showing enough context.
SNIPPET_MAX_LEN = 180
# Limit per-source chunks to avoid a single file dominating answers.
MAX_CHUNKS_PER_FILE = 50
# Distance threshold for default relevance filtering.
# Balanced to avoid noisy hallucination while still allowing useful low-confidence
# responses for moderately related scoped queries.
DEFAULT_RELEVANCE_MAX_DISTANCE = 0.9
