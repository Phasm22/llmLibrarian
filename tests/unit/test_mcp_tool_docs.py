"""Guard against accidental removal of MCP tool usage boundaries in docstrings."""

from __future__ import annotations

import inspect


def test_key_mcp_tool_docstrings_include_usage_boundaries():
    import mcp_server

    tools = [
        mcp_server.query_personal_knowledge,
        mcp_server.multi_query_knowledge,
        mcp_server.find_files,
        mcp_server.add_silo,
        mcp_server.trigger_reindex,
        mcp_server.repair_silo,
        mcp_server.health,
        mcp_server.session_context,
    ]

    for fn in tools:
        doc = inspect.getdoc(fn) or ""
        assert "Use when:" in doc, f"missing 'Use when:' in {fn.__name__}"
        assert "Do not use when:" in doc, f"missing 'Do not use when:' in {fn.__name__}"
        assert "Pairs with:" in doc, f"missing 'Pairs with:' in {fn.__name__}"
