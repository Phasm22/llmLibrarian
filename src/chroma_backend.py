"""Injectable Chroma client factory (optional ``get_chroma_client`` on run_add / run_ask)."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ChromaClientFactory(Protocol):
    """Given a resolved DB path string, return a Chroma persistent client (or test fake)."""

    def __call__(self, db_path: str, /) -> Any:  # pragma: no cover - typing only
        ...
