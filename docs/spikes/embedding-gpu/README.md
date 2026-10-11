---
description: Index of the embedding/GPU research spikes — read first to see what each spike asks, what order they run in, and what has already been answered.
type: doc
updated: 2026-09-20
---

# Embedding & GPU research program

Nine spikes investigating how llmLibrarian should compute embeddings, and what
hardware it should use to do it. Each `S*.md` file is a **self-contained prompt**
meant to be pasted into a fresh long-running agent session. They assume no
context from the conversation that produced them.

Every spike shares one rulebook: [PROTOCOL.md](PROTOCOL.md). Read it before
starting any spike. It defines the adversarial stance, the benchmark hygiene
rules, and the results schema all spikes report into.

## Why this exists

Embeddings currently run through sentence-transformers on `all-mpnet-base-v2` in
full precision, with the device chosen per call by batch size
([src/embeddings.py](../../../src/embeddings.py)). Three things about that setup
are suspicious enough to be worth real investigation:

1. Large ingests are deliberately pinned to CPU, because Apple's GPU backend
   (MPS) is not thread-safe and the ingest path wants 8 parallel workers. A quick
   measurement showed CPU at 10.7 chunks/s against 33.7 for the GPU at half
   precision — so the path that matters most may be taking the slowest route.
2. Every process (MCP server, `pal` daemon, CLI) loads its own copy of the model,
   and GPU memory is not returned when it unloads — a finding already recorded in
   [src/image_embeddings.py](../../../src/image_embeddings.py).
3. Embedding is interleaved with Chroma writes, so no stage of the pipeline
   overlaps with any other.

None of that establishes that GPU work is where the wins are. S1 exists
specifically to try to prove it isn't.

## Order and dependencies

```
S0  ground truth + harness ......... blocks everything
     |
S1  critical path + simulator ...... gates S2/S3/S4 (may cancel them)
     |
     +-- S2  roofline & kernels ..... independent after S1
     +-- S3  backend shootout ....... independent after S1
     +-- S4  embedding server ....... wants S1 simulator, can start on S0
     +-- S7  memory forensics ....... independent after S0, informs S4
     |
S5  model & representation ......... needs S0 eval set only; run in parallel early
     |
S6  late interaction ............... needs S5's eval set results as baseline
     |
S8  synthesis & decision ........... needs all of the above
```

S0 first, alone. Then S1 and S5 together — S1 decides whether the
hardware program is worth running at all, while S5 answers a question that stays
valuable either way. Everything else follows.

**Current gate status** (see [S0-REVIEW.md](S0-REVIEW.md)): **S1 is cleared to
start** — it needs only the fixtures, which are sound. **S5 and S6 remain
blocked** on relevance labels able to resolve small quality differences.
**S2 and S3 remain blocked** on working energy attribution, and must re-run the
S0 baselines under the corrected burst/sustained rule in PROTOCOL.md §2 before
quoting any throughput number.

## The spikes

| # | Question | Kill condition |
|---|---|---|
| [S0](S0-ground-truth.md) ([verdict](S0-VERDICT.md), [review](S0-REVIEW.md)) | What is a fixture and a measurement we can trust? | Inconclusive — energy isolation and fine-grained relevance labels not yet sufficient |
| [S1](S1-critical-path.md) | Is embedding even on the critical path? | Embedding <30% of ingest wall clock |
| [S2](S2-roofline-kernels.md) | How much of the chip are we actually using? | Already within 20% of achievable peak |
| [S3](S3-backend-shootout.md) | Which runtime, and can we use ANE and GPU at once? | No backend beats tuned torch by >25% |
| [S4](S4-embedding-server.md) | Is a dedicated embedding process worth its complexity? | A lock in-process gets within 15% |
| [S5](S5-model-representation.md) | What model and vector format maximize quality per ms and per MB? | No model beats mpnet+rerank at equal latency |
| [S6](S6-late-interaction.md) | Does multi-vector retrieval justify its storage and GPU cost? | Gain under cross-encoder rerank at equal budget |
| [S7](S7-memory-residency.md) | Can a GPU-backed daemon survive 24h on a laptop? | No leak; RSS flat over soak |
| [S8](S8-synthesis.md) | Given all results, what do we build? | — (decision) |

## Results

Spikes write machine-readable results to `spikes/results/<spike-id>/<run-id>.json`
and a written verdict alongside their prompt file as `S*-VERDICT.md`. The table
above gets updated as verdicts land. A confirmed or refuted verdict closes a
spike; an inconclusive verdict records evidence and leaves its gate open.

## Hardware in scope

- **macOS dev machine:** Apple M4 Pro, 48 GB unified memory. Primary target.
- **Linux desktop:** runs the `pc-stacks` host runtime (see
  [AGENTS.md](../../../AGENTS.md)). Its GPU is currently **unknown** — S3 must
  establish this before any NVIDIA-specific work is scoped.
