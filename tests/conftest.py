import os
import sys
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

# Avoid color/style differences in test output formatting.
os.environ.setdefault("LLMLIBRARIAN_EDITOR_SCHEME", "file")


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

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("query", kwargs))
        return self.query_result

    def get(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get", kwargs))
        return self.get_result

    def add(self, **kwargs: Any) -> None:
        self.calls.append(("add", kwargs))

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
        return state["response"]

    monkeypatch.setitem(sys.modules, "ollama", SimpleNamespace(chat=_chat))
    return state
