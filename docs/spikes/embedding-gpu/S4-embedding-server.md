---
description: Spike prompt S4 — build a dedicated embedding service that owns the GPU, and try to prove a simpler in-process design beats it. This is where "is the complexity worth it" gets answered with numbers.
type: plan
updated: 2026-09-20
---

# S4 — The embedding server, and whether it earns its keep

> Paste everything below into a fresh agent session. Budget: 1–2 weeks.
> Requires: S0 complete. S1's simulator, if it exists, should be used before
> you write server code. S3's backend recommendation, if available.

---

## Context

You are working in `llmLibrarian` (this repo): a local document index over a
Chroma vector store, reached through MCP tools, a `pal` CLI, and a watch daemon
that re-indexes changed files. Read [CLAUDE.md](../../../CLAUDE.md),
[docs/TECH.md](../../TECH.md), and
[docs/CHROMA_AND_STACK.md](../../CHROMA_AND_STACK.md). Read
[PROTOCOL.md](PROTOCOL.md); it binds you.

Today, every process that needs an embedding loads its own model. The MCP
server, the daemon, and each CLI invocation each hold roughly half a gigabyte of
torch tensors. [src/embeddings.py](../../../src/embeddings.py) caches the model
per process specifically because, without it, a long-lived MCP server
"fragments the glibc heap and drives anon RSS into the tens of GB over hours".
Apple's GPU backend is not thread-safe, so bulk ingest pins itself to CPU to
keep 8 workers ([src/embeddings.py](../../../src/embeddings.py),
`ingest_parallel_embedding_device`). GPU memory is not returned on unload
([src/image_embeddings.py](../../../src/image_embeddings.py)).

There is a precedent in this codebase for the fix: Chroma has exactly these
problems, and the answer was **one `chroma run` server, every client over HTTP**.

## Your question

**Should embeddings move to a dedicated process that owns the accelerator — and
is the operational cost worth what it buys?**

The second half is the real assignment. The user explicitly wants the
"is this worth the complexity" judgment made empirically rather than by
intuition. Produce the numbers that decide it.

## Build two things, and make them fight

### A. The server (the sophisticated option)

A long-lived process owning one model on one accelerator, serving every caller.
Design questions to answer by measurement, not by preference:

- **Batching across callers.** Coalesce requests arriving within a short window
  into one batch. What window? Measure the latency/throughput tradeoff rather
  than picking a number.
- **Token-budget batches**, informed by S2's padding findings, rather than fixed
  counts.
- **Priority lanes.** An interactive MCP query must not wait behind a 10,000-chunk
  ingest. Test preemption or a reserved lane, and measure query p99 under a
  sustained ingest flood. **This is the headline metric of the spike.**
- **Transport.** Compare HTTP over loopback, a Unix domain socket, and shared
  memory. At 768 floats per chunk, serialization may dominate the compute you
  are trying to speed up — measure it. Note the existing precedent is HTTP, and
  consistency has value.
- **Backpressure.** What happens when ingest produces work faster than the GPU
  drains it? Unbounded queues turn into memory exhaustion.
- **Lifecycle.** Model load on first use or at startup; idle unload to release
  memory (interacting with the fact that GPU memory does not come back);
  supervision; restart; what a client does when the server is down.
- **Multiple models.** Text and CLIP image embeddings, and possibly the
  cross-encoder reranker, could share one GPU-owning process. Decide whether
  that is one service or several, and measure the memory consequence.

Then measure what it unlocks: with one process owning the accelerator, the
thread-safety constraint disappears, so bulk ingest can use the GPU while many
extraction workers run in parallel. Quantify that against today's CPU-pinned
path on a real large ingest.

### B. The simplest credible alternative (the one that should win)

Do **not** skip this. Build the boring version: keep everything in-process, put
a lock around accelerator access, keep the existing model cache, and fix only
the obvious sins — overlap extraction with embedding, stop pinning bulk ingest
to CPU, tune the batch policy.

If the boring version lands within 15% of the server on the metrics that matter,
**the server loses** and your verdict says so. That is a genuinely good outcome:
it saves a process, a protocol, a supervisor, and a permanent class of bugs.

## Metrics that decide it

Report all of these for both designs and for today's baseline:

1. Query p99 latency, idle and under ingest load.
2. Bulk ingest wall clock on a real large silo.
3. Single-file watch-daemon latency, save to retrievable — **including process
   startup and model load**, which the server design eliminates and which S1
   may have shown to dominate.
4. Total system memory with MCP server, daemon and a CLI invocation all live.
5. Cold-start behavior: first query after boot, and after idle.
6. Failure modes, enumerated and tested: server down, server hung, version skew
   between client and server, two servers started at once, killed mid-batch.
   The existing stack already has PID-file locking for the MCP HTTP path; reuse
   the pattern rather than inventing one.

## Adversarial mandate

- Your job is to kill the server proposal. Look for the workload where a shared
  batching queue makes latency *worse* — a single interactive query that now
  waits for a window to close, or a batch formed with mismatched sequence
  lengths that wastes compute on padding.
- Count the complexity precisely: new lines of code, new process to supervise,
  new install/start/stop/status surface (`pal embed ...` alongside
  `pal chroma ...`), new failure modes, new things to document, new ways a
  first-time setup breaks. Put this in the verdict as a concrete list, not an
  adjective.
- Test the degraded paths as hard as the happy path. A design that is 30%
  faster but fails closed when the server dies is worse than one that is slower
  and always works. Verify the in-process fallback actually works under a real
  failure, not a simulated one.
- Consider whether the same benefit is available for free. If model load
  dominates single-file latency, a persistent daemon that already exists might
  be made to hold the model, with no new process at all. Check that before
  building anything.
- Ask whether this scales down. On a machine with no GPU, does the server still
  make sense, or does it become pure overhead?

## Stretch: the two-node question

If S3 found a usable GPU on the Linux box, test whether the Mac can offload bulk
embedding to it over the network while keeping interactive queries local. Measure
whether network transfer of text in and vectors out leaves any win, and whether
the added failure surface is tolerable. Expect this to lose; say so if it does.

## Deliverables

1. `spikes/embed-server/` — the server MVP.
2. `spikes/embed-inproc/` — the boring alternative.
3. A head-to-head results table across all six metric groups, with variance.
4. An explicit complexity ledger for the server design.
5. A recommendation: build it, build the boring version, or change nothing.
6. `S4-VERDICT.md` per PROTOCOL.md §7.

## Kill conditions

- In-process alternative within 15% on query p99 under load and on ingest wall
  clock: recommend against the server, and hand S8 the boring option.
- Transport overhead eats more than half the compute win: reconsider the whole
  shape — the answer may be a library-level fix, not a service.
