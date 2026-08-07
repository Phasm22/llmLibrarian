"""Process-level concerns for the llmLibrarian MCP server.

``mcp_server.py`` owns the tool surface -- the FastMCP instance and every
``@mcp.tool()``. Everything a tool does not need to know about lives here:

- ``bootstrap``   -- stdout hygiene, env loading, DB-path resolution
- ``runtime``     -- single-instance PID lock, signals, auth, process visibility
- ``jobs``        -- the Chroma mutex and the background ingest runner
- ``diagnostics`` -- health summaries and the operator guidance derived from them

Nothing in this package imports ``mcp_server``; the dependency runs one way.
"""
