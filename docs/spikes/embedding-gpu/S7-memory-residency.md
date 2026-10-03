---
description: Spike prompt S7 — soak-test GPU and host memory behavior in the long-lived MCP server and watch daemon, and determine whether a GPU-backed daemon can survive a day on a laptop. Informs S4's lifecycle design.
type: plan
updated: 2026-09-20
---

# S7 — Memory residency: can a GPU-backed daemon live for a day?

> Paste everything below into a fresh agent session. Budget: 3–5 days, plus
> unattended soak time. Requires: S0 complete.

---

## Context

You are working in `llmLibrarian` (this repo): a local document index over a
Chroma vector store, with a long-lived MCP server and a `pal` watch daemon that
re-indexes changed files continuously. Read [CLAUDE.md](../../../CLAUDE.md),
[docs/TECH.md](../../TECH.md), and
[docs/CHROMA_AND_STACK.md](../../CHROMA_AND_STACK.md). Read
[PROTOCOL.md](PROTOCOL.md); it binds you.

The codebase carries scar tissue from exactly this problem, and the comments are
your starting evidence:

- [src/embeddings.py](../../../src/embeddings.py) caches embedding functions
  because without it "every ingest / update_file / remove_file call in a
  long-lived MCP server instantiates a fresh SentenceTransformer (~500 MB of
  torch tensors), fragments the glibc heap, and drives anon RSS into the tens of
  GB over hours."
- [src/image_embeddings.py](../../../src/image_embeddings.py) pins CLIP to CPU
  because on Apple's GPU backend it held "~1.6 GB of GPU memory in the
  long-lived MCP server (the MPS heap is not returned even after unloading)".
- There is an environment flag, `LLMLIBRARIAN_EXIT_ON_STALE_GENERATION`, whose
  job is to restart the MCP embedded reader — a workaround whose necessity
  should itself be re-examined.

Both fixes were reactions to real damage. Neither was followed by a systematic
measurement, and both constrain what S4 can design.

## Your question

**What does memory actually do in these processes over a day of realistic use,
and what lifecycle policy keeps a GPU-backed service healthy on a personal
machine?**

## Why this decides more than it looks like

Every other spike in this program assumes a model can be held resident on an
accelerator. If GPU memory is never released and a second model cannot be
loaded, then S3's multi-backend routing, S4's shared multi-model service, S5's
larger models, and S6's parallel index are all constrained by the same wall. You
are measuring that wall.

This is also the difference between a tool that runs on a laptop alongside
everything else the user is doing, and one that quietly makes the machine
unusable. A background indexer holding several gigabytes of GPU memory while
someone is in a video call is a product failure regardless of its throughput.

## What to measure

### 1. Characterize the existing leak behavior properly

Reproduce both documented problems deliberately, in a controlled way, and
quantify them: growth rate per operation, whether it plateaus, what exactly is
retained. Distinguish clearly between host memory (RSS, and how much is
allocator fragmentation versus live objects), unified/GPU memory as the OS
accounts for it, and what the framework thinks it is holding. These three
numbers disagree, and knowing how they disagree is the finding.

Determine whether the "MPS heap is not returned" claim is precisely true, or
whether it is returned under some condition nobody found — a cache-empty call, a
particular unload order, process-level pressure. This matters enormously for
whether idle unloading is a viable policy.

### 2. Realistic soaks

Run at least 24 hours, unattended, under a load profile that resembles actual
use rather than a stress loop: a watch daemon reacting to real file changes, MCP
queries at irregular intervals, an occasional bulk ingest, long idle periods.
Sample memory continuously and plot it. Idle periods are important — many leaks
only show as a staircase that never comes back down between bursts.

Run the same soak against both the current architecture and, if S4 has produced
one, the server architecture. The server hypothesis is that one process holding
one model is strictly better than three processes each holding their own;
verify that rather than assuming it.

### 3. Concurrency and contention for memory

This machine has 48 GB of unified memory shared between CPU and GPU. Ollama
loads multi-gigabyte generation models into the same pool. Measure what happens
when an embedding model, a reranker, a CLIP model and an Ollama model all want
residency at once. Find the point where the system starts swapping or where
Apple's memory pressure handling begins evicting, and report how the system
behaves at that boundary — degraded, or broken.

### 4. Lifecycle policies, tested

Propose and measure concrete policies: load on demand versus at startup; unload
after idle (and whether that actually frees anything, per §1); periodic
restart; restart on a memory threshold; keeping a small model resident and a
large one on demand. Report the cost of each in first-query latency after idle,
which is what a user actually feels.

## Adversarial mandate

- Try to prove the existing workarounds are unnecessary — that the CPU pinning
  for CLIP and the exit-on-stale-generation flag are no longer needed on current
  torch and OS versions. If they can be removed, that is a direct simplification
  and a real deliverable.
- Try equally hard to prove they are insufficient, by finding a usage pattern
  that still blows up memory despite them. Long-running personal tools fail on
  the path nobody tested.
- Be suspicious of a soak that looks clean. Verify your load generator actually
  exercised the paths you think it did — count operations, not just elapsed
  time. A flat memory graph from a daemon that was idle all night proves
  nothing.
- Check behavior on battery and under memory pressure from other applications,
  not just on an otherwise idle machine. That is the real deployment condition.
- Consider the failure mode where the *fix* is the problem: a periodic restart
  that drops a warm model may turn a memory problem into a latency problem the
  user notices more.

## Deliverables

1. Precise characterization of host and accelerator memory behavior per
   operation type, with the three accounting views reconciled.
2. A definitive answer on whether accelerator memory can be reclaimed, and under
   what conditions.
3. 24-hour soak plots for current architecture, and for the server architecture
   if it exists.
4. The residency budget: what can be held simultaneously on a 48 GB machine
   alongside a local generation model, and what happens past that.
5. A recommended lifecycle policy with its measured latency cost, written so S4
   can implement it directly.
6. A verdict on whether the existing CPU pinning and restart workarounds can be
   removed.
7. `S7-VERDICT.md` per PROTOCOL.md §7.

## Kill conditions

- If accelerator memory genuinely cannot be reclaimed without killing the
  process, say so plainly: it makes periodic restart the only viable policy, and
  S4 must design for a service that expects to be recycled.
- If no leak reproduces under realistic load on current versions, recommend
  removing the workarounds and re-measuring the resulting simplification.
