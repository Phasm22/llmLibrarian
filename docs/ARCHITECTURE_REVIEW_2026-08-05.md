# Architecture review note

Status: needs review

Quick pass across:

- `README.md`
- `docs/GUIDE.md`
- `docs/CHROMA_AND_STACK.md`
- `docs/orchestration-matrix.md`
- `src/chroma_client.py`
- `src/chroma_lock.py`
- `mcp_server.py`
- `scripts/run_mcp_http.sh`
- `.mcp.json`

This is not a bug report. It is a short "look again" note for the design and runtime model.

## Main review flags

1. Chroma is dictating the architecture.

   The repo now carries embedded mode, HTTP server mode, generation markers, preflight checks, retry logic, and multiple lock layers to keep Chroma stable. That is functional, but it means the storage choice is driving the shape of the system instead of sitting behind a small abstraction.

   Review question: should this standardize on one operating mode, or should Chroma be treated as replaceable infrastructure?

2. The MCP topology is still split.

   The repo has both stdio and HTTP-era guidance, and `.mcp.json` still points at a local Python command while the docs emphasize a persistent HTTP MCP service. That makes it easy to accidentally run more than one MCP process against the same DB path.

   Review question: should stdio be removed, or at least isolated so the supported deployment model is unambiguous?

3. `mcp_server.py` is carrying too many responsibilities.

   It owns auth, health, query routing, ingest, repair, background jobs, and process locking. The API works, but the server is now part transport, part worker, and part recovery console.

   Review question: should write actions move to a separate job layer or a smaller service boundary?

4. The write path is convention-heavy.

   `add_silo` and `trigger_reindex` return immediately and require callers to poll for progress. `confirm=true` is a soft guard, not a true permission boundary. That is acceptable for a local power-user tool, but it is not a strong remote-write model.

   Review question: should remote MCP write tools get a stronger approval model and clearer job status?

5. Locking is functional but complex.

   The system uses flock, in-process mutexes, generation files, heartbeat probes, and retry backoff. Each piece solves a real failure mode, but together they create a lot of state to reason about.

   Review question: can a simpler concurrency model replace some of this compensation logic?

## Bottom line

This looks stable enough for a single-user local system, but the design has accumulated a lot of compensating machinery around Chroma and HTTP MCP. That is the part that should be reviewed next.
