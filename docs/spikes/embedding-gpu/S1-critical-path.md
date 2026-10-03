---
description: Spike prompt S1 — profile the real ingest and query paths and build a pipeline simulator, to decide whether embedding compute is on the critical path at all. Can cancel S2/S3/S4.
type: plan
updated: 2026-09-20
---

# S1 — Critical path: is embedding even the bottleneck?

> Paste everything below into a fresh agent session. Budget: 3–4 days.
> Requires: S0 fixtures and harness. **Cleared to start** although S0 is still
> inconclusive: this spike needs neither energy attribution nor fine-grained
> relevance labels. Read [S0-VERDICT.md](S0-VERDICT.md) and
> [S0-REVIEW.md](S0-REVIEW.md) first. Do not reuse S0's throughput numbers:
> take your own under PROTOCOL.md §2's burst/sustained rule.

---

## Context

You are working in `llmLibrarian` (this repo). It indexes user-chosen folders
into a local Chroma vector store and answers questions from that index only, via
MCP tools or a local Ollama model. Read [CLAUDE.md](../../../CLAUDE.md) and
[docs/TECH.md](../../TECH.md) first. Read [PROTOCOL.md](PROTOCOL.md); it binds you.

Ingest today: collect → chunk → extract → embed → add to Chroma, with embedding
called inside the Chroma write loop
([src/ingest/\_\_init\_\_.py](../../../src/ingest/__init__.py), around the
`add_chunks_in_batches` path, batch size 256, up to 8 workers). Embedding device
is picked per call by batch size in [src/embeddings.py](../../../src/embeddings.py);
for ingests of 400+ files it is deliberately pinned to CPU, because Apple's MPS
backend is not thread-safe and the parallel workers matter more.

A program of further spikes wants to make embedding faster: better kernels, a
different runtime, a dedicated GPU-owning process. **Your job is to find out
whether any of that would show up in wall-clock time at all.**

## Your question

**Where does time actually go, and what is the ceiling on any embedding-side
improvement?**

## Hypothesis to attack

> Embedding compute is a large enough share of ingest wall-clock time that
> making it 3x faster is worth restructuring the pipeline for.

Attack it. The obvious failure mode for this whole program is that PDF
extraction, OCR (there is a Swift vision OCR path,
[src/vision_ocr.swift](../../../src/vision_ocr.swift)), file walking, chunking,
and Chroma's own indexing dominate, and the GPU work is rounding error.

## What to measure

### 1. Stage-resolved profiles of real ingests

Instrument the real pipeline — not a reproduction of it — and profile at least
these shapes, because they will not behave alike:

- Many small text/markdown files (thousands of notes).
- Few large PDFs requiring extraction, ideally some requiring OCR.
- A source code tree.
- An image folder (the CLIP path in
  [src/image_embeddings.py](../../../src/image_embeddings.py), where a comment
  records that image decode dominates and the GPU bought nothing).
- A mixed real silo, as the user actually runs it.

For each: wall clock per stage, CPU utilization per stage, GPU utilization,
memory, and how much of the time any given resource sits idle. Capture a
flamegraph or equivalent, not just totals.

Then compute the **Amdahl ceiling**: if embedding took zero time, how much
faster is each ingest shape? That single number decides the fate of S2, S3 and
S4.

### 2. The incremental path, which may matter more

Bulk ingest is a rare event. The `pal` watch daemon re-indexing one changed file
happens constantly, and it lands in the small-batch regime where the current
code chooses CPU. Measure end-to-end latency from file save to the new content
being retrievable, broken down by stage — including process startup and model
load, which may dwarf the actual compute.

If a one-file update spends 4 seconds loading a model to do 40 ms of work, that
is a far more interesting finding than anything about kernels, and it points
straight at S4.

### 3. The query path

Measure MCP query latency end to end: embed the query (batch of one), Chroma
search, dedup/diversify, optional cross-encoder rerank over 40 candidates,
serialize. Find where the time is. Do this under two conditions — idle, and
while a bulk ingest is running — because contention between the two is a real
user-visible failure mode and nobody has measured it.

### 4. Chroma's own contribution

Chroma 1.x is not process-safe for multiple embedded clients on one path; the
production setup is one `chroma run` server with HTTP clients (see
[docs/CHROMA_AND_STACK.md](../../CHROMA_AND_STACK.md)). Measure what the HTTP
hop, serialization, and HNSW index build cost as a share of ingest. If Chroma's
write path is the wall, embedding speed is irrelevant and this program should
pivot to storage.

## What to build

**A discrete-event simulator of the pipeline**, under `spikes/sim/`.

Model each stage as a service with a measured rate distribution and a worker
count, with queues between them. Validate it: it must predict the wall clock of
real ingests you did not use to fit it, within 15%. State that validation error
honestly — an unvalidated simulator is a fantasy generator.

Then use it to answer questions that would otherwise require building things:

- What is the speedup from overlapping stages, holding per-stage rates constant?
- What is the speedup from a 3x faster embedder, with and without overlap?
- Where does the bottleneck move once embedding is fast? It always moves
  somewhere. Name where.
- How many extraction workers can one embedder feed before it starves?
- At what corpus size does each conclusion change?

This simulator is the most reusable artifact in the program. S4 in particular
should be able to test its design in simulation before writing a line of server
code, and S8 should be able to ask it what a proposed architecture is worth.

## Adversarial mandate

- Your default expectation should be that the GPU program is mostly dead. Try
  hard to demonstrate it. If extraction and Chroma writes are 80% of wall clock,
  say so in the first paragraph of your verdict and recommend cancelling S2 and
  S3.
- Conversely, if embedding *is* dominant, find the workload where it is not, and
  bound the claim.
- Check whether the current CPU pinning for large ingests is actually a
  pessimization or was a reasonable call. Measure 8 CPU workers against 1 GPU
  stream on the same real corpus. The quick benchmark suggested CPU loses badly,
  but it never tested the parallel path — that is exactly the comparison nobody
  has run.
- Question the 8-worker and 256-batch constants. They look like round numbers,
  not measured optima.

## Deliverables

1. Stage-resolved profiles for all five ingest shapes, in the results schema.
2. Amdahl ceiling per shape — the headline number.
3. Incremental (single-file) and query-path latency breakdowns.
4. `spikes/sim/` with stated validation error.
5. `S1-VERDICT.md` per PROTOCOL.md §7, opening with an explicit
   **continue / cancel** recommendation for S2, S3 and S4.

## Kill conditions

- **Embedding under 30% of ingest wall clock on the realistic mixed shape:**
  recommend cancelling S2 and S3. S4 may still survive on the strength of the
  incremental-path and query-contention findings — evaluate it separately.
- **Simulator cannot be validated within 15%:** report it as unusable rather
  than shipping a model later spikes would trust wrongly.
