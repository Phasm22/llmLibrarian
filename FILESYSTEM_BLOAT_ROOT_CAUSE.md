# Filesystem Bloat Root Cause Analysis

**Date:** 2026-04-18 5:35 PM  
**Status:** CRITICAL — Multiple watch daemons contending for shared ChromaDB

## The Problem

Four `pal.py pull --watch` processes are running simultaneously, all trying to write to the same ChromaDB database:

```
PID  CPU%  MEM        Command
1243 100%  1.4 GB     pal.py pull /Users/tjm4/Desktop/old-school --watch --interval 60
1212 0%    0.5 GB     pal.py pull /Users/tjm4/journalLinker --watch --interval 60
1236 0%    0.5 GB     pal.py pull /Users/tjm4/Documents/hot_seat --watch --interval 60
1217 0%    0.5 GB     pal.py pull /Users/tjm4/Desktop/ContextSort/decisions --watch --interval 60
```

All four hold locks on `.llmli_chroma.flock`, creating:
- **Lock contention:** Writes block on each other
- **Silent failures:** Lock timeouts don't abort, they silently retry/defer
- **Disk accumulation:** Partial writes, temp files, uncommitted state pile up
- **System lockup:** Eventually kernel can't allocate more file descriptors

## Why The Fixes Didn't Work

1. **Apr 4 fix (72d9e1d):** Removed `clear_system_cache()` to prevent SIGSEGV
   - Fixed SIGSEGV *within a single process*, but didn't address **multi-process contention**

2. **Apr 4 fix (e0f637d):** Single ChromaDB client + thread-safe singleton
   - Thread-safe *within one Python process*, but **multiple processes bypass this entirely**
   - Each `pal.py pull` instance is a separate process with its own Python interpreter
   - The singleton pattern only applies to threads within that process

## Why This Happens Silently

The ChromaDB lock file (`.llmli_chroma.flock`) exists, but:
- Multiple processes can each hold the lock during their window
- Lock contention just causes slow operations, not immediate errors
- Writes may partially succeed, leaving DB in inconsistent state
- `llmli_last_failures.json` stays empty because individual operations don't fail—they just hang
- By the time you notice the system is slow, disk is already full

## Temporary Workaround (Immediate)

Stop the watch daemons:
```bash
killall -9 -p pal  # Force kill all pal.py processes
rm -f /Users/tjm4/llmLibrarian/my_brain_db/.llmli_chroma.flock  # Clear stale lock
```

Then verify:
```bash
llmli ls  # Should work instantly now
```

## Permanent Fix Required

Choose ONE of:

### Option A: Single Watch Daemon (Recommended for now)
- Merge all `pal.py pull --watch` calls into a single daemon that pulls multiple folders
- This prevents multi-process contention entirely
- Example: `pal pull --watch --folders /path1 /path2 /path3 --interval 60`

### Option B: Process-Level Lock Serialization
- Add a cross-process lock in `src/chroma_client.py` using `fcntl.flock()` on a file
- Serialize all ChromaDB access across all Python processes
- Slower (writes block on reads), but safe

### Option C: MCP-Only Architecture
- Don't let pal.py import/use ChromaDB directly
- All access goes through MCP server (single point of entry, no multi-process issues)
- Requires larger refactor: pal.py → mcp calls instead of direct imports

## What's Accumulating on Disk

With multiple processes contending:
1. Partial writes to `chroma.sqlite3` (corruption that repair fixes)
2. Stale embeddings never committed (orphaned in buffer)
3. Lock holders not releasing file handles properly
4. Temporary work files not cleaned up between retries

Each watch interval (60s) can accumulate garbage until disk fills or system runs out of file descriptors.

## Next Steps

1. Kill watch daemons immediately (they're tearing up your SSD)
2. Choose fix (A/B/C) based on usage pattern
3. Add monitoring to catch this: 
   - Alert if >1 process holds `.llmli_chroma.flock`
   - Alert if ChromaDB write hangs >5s
