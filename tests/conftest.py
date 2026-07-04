import os
import sys
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# --- Environment isolation (must run before pal/cli/mcp_server imports) ---
# pal bootstraps ~/.config/llmLibrarian/.llmlibrarian.env into os.environ at
# import time. On an operator machine that sets LLMLIBRARIAN_CHROMA_HOST,
# which silently flips get_client() into HTTP mode and makes "unit" tests
# write into the production Chroma server. Block the bootstrap and scrub any
# host-specific vars so tests only ever touch per-test tmp paths.
os.environ["LLMLIBRARIAN_ENV_BOOTSTRAPPED"] = "1"
for _key in (
    "LLMLIBRARIAN_DB",
    "LLMLIBRARIAN_CHROMA_HOST",
    "LLMLIBRARIAN_CHROMA_PORT",
    "LLMLIBRARIAN_CHROMA_SSL",
    "LLMLIBRARIAN_MCP_URL",
    "LLMLIBRARIAN_MCP_PATH",
    "LLMLIBRARIAN_EMBEDDING_MODEL",
    "LLMLIBRARIAN_EMBEDDING_DEVICE",
):
    os.environ.pop(_key, None)
# ONNX MiniLM path: keeps the torch/sentence-transformers stack out of test
# runs entirely (CI installs without torch); override explicitly if a test
# needs the sentence-transformer path.
os.environ.setdefault("LLMLIBRARIAN_EMBEDDING", "default")

# Deterministic terminal rendering: rich/typer help output and answer
# wrapping (_answer_wrap_width) depend on detected terminal width and TTY
# color support, which differ on CI runners. Pin width 88 (the suite's
# phrasing asserts expect the resulting 86-column answer wrap) and disable
# color so substring asserts see plain text.
os.environ["COLUMNS"] = "88"
os.environ["NO_COLOR"] = "1"
os.environ.pop("FORCE_COLOR", None)

# Ensure tests target the workspace module, not an installed site-packages copy.
_pal_path = (ROOT / "pal.py").resolve()
_pal_spec = importlib.util.spec_from_file_location("pal", _pal_path)
if _pal_spec is not None and _pal_spec.loader is not None:
    _pal_module = importlib.util.module_from_spec(_pal_spec)
    sys.modules["pal"] = _pal_module
    _pal_spec.loader.exec_module(_pal_module)

_cli_path = (ROOT / "cli.py").resolve()
_cli_spec = importlib.util.spec_from_file_location("cli", _cli_path)
if _cli_spec is not None and _cli_spec.loader is not None:
    _cli_module = importlib.util.module_from_spec(_cli_spec)
    sys.modules["cli"] = _cli_module
    _cli_spec.loader.exec_module(_cli_module)

_mcp_path = (ROOT / "mcp_server.py").resolve()
_mcp_spec = importlib.util.spec_from_file_location("mcp_server", _mcp_path)
if _mcp_spec is not None and _mcp_spec.loader is not None:
    _mcp_module = importlib.util.module_from_spec(_mcp_spec)
    sys.modules["mcp_server"] = _mcp_module
    _mcp_spec.loader.exec_module(_mcp_module)

# Avoid color/style differences in test output formatting.
os.environ.setdefault("LLMLIBRARIAN_EDITOR_SCHEME", "file")

# Never let tests rewrite or restart the operator's real watch daemon units
# (pal._sync_daemon_if_installed runs after successful pulls).
os.environ["PAL_SUPPRESS_DAEMON_SYNC"] = "1"


@pytest.fixture(autouse=True)
def _reset_embedding_function_cache() -> Any:
    """Clear the process-wide embedding-function cache between tests so cases
    that swap chromadb fakes or env vars see a fresh EF."""
    from embeddings import _reset_ef_cache_for_tests

    _reset_ef_cache_for_tests()
    yield
    _reset_ef_cache_for_tests()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Consistent temp DB path fixture for tests that need a Chroma path."""
    return tmp_path / "db"


class _MockCollection:
    def __init__(self) -> None:
        self.query_result: dict[str, Any] = {
            "documents": [[]],
            "metadatas": [[]],
            "distances": [[]],
            "ids": [[]],
        }
        self.get_result: dict[str, Any] = {"ids": [], "documents": [], "metadatas": []}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._added_ids: set[str] = set()

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("query", kwargs))
        return self.query_result

    def get(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get", kwargs))
        ids = kwargs.get("ids")
        if ids is not None:
            # Post-add write verification path: acknowledge ids we've seen added.
            found = [i for i in ids if i in self._added_ids]
            return {"ids": found, "documents": [None] * len(found), "metadatas": [None] * len(found)}
        return self.get_result

    def add(self, **kwargs: Any) -> None:
        self.calls.append(("add", kwargs))
        self._added_ids.update(kwargs.get("ids") or [])

    def delete(self, **kwargs: Any) -> None:
        self.calls.append(("delete", kwargs))


@pytest.fixture
def mock_collection() -> _MockCollection:
    """Collection stand-in with configurable query/get responses."""
    return _MockCollection()


@pytest.fixture
def mock_ollama(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch `ollama.chat` and capture request payloads for assertions."""
    state: dict[str, Any] = {
        "calls": [],
        "response": {"message": {"content": "ok"}},
    }

    def _chat(**kwargs: Any) -> dict[str, Any]:
        state["calls"].append(kwargs)
        response = state["response"]
        if isinstance(response, list):
            if not response:
                raise AssertionError("mock_ollama response queue is empty")
            return response.pop(0)
        return response

    monkeypatch.setitem(sys.modules, "ollama", SimpleNamespace(chat=_chat))
    return state
