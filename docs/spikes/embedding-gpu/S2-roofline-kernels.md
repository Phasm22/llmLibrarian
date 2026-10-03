---
description: Spike prompt S2 — establish how much of the M4 Pro's compute and memory bandwidth the embedder actually reaches, and how much is lost to padding, precision and launch overhead. Run after S1 says embedding matters.
type: plan
updated: 2026-09-20
---

# S2 — Roofline: how much of the chip are we using?

> Paste everything below into a fresh agent session. Budget: 4–5 days.
> Requires: S0 complete. S1 must not have cancelled this spike.

---

## Context

You are working in `llmLibrarian` (this repo): a local document index with a
Chroma vector store, queried through MCP tools or a local Ollama model. Read
[CLAUDE.md](../../../CLAUDE.md) and [docs/TECH.md](../../TECH.md) first. Read
[PROTOCOL.md](PROTOCOL.md); it binds you.

Embeddings run `all-mpnet-base-v2` — a BERT-family encoder, 768 dimensions, 12
layers, 384-token maximum — through sentence-transformers on torch, in full
precision. Device is chosen per call by batch size
([src/embeddings.py](../../../src/embeddings.py)). Hardware is an Apple M4 Pro
with 48 GB of unified memory.

Known measurements (worst-case synthetic input, superseded by S0's fixtures):
CPU 10.7 chunks/s, GPU full precision 26.3, GPU half precision 33.7, GPU half
precision at batch 128 **slower** at 23–27. That last result is the interesting
one — throughput went *down* when the batch got bigger, and nobody knows why.

## Your question

**What fraction of this chip's theoretical capability does the embedder reach,
and what is taking the rest?**

This spike is not about swapping libraries — that is S3. It is about
understanding the machine well enough to know what "fast" would even mean here.

## Establish the roofline first

Before optimizing anything, work out the ceiling:

- Peak FLOPs and peak memory bandwidth of the M4 Pro GPU, and of the CPU cores.
  Verify with microbenchmarks rather than trusting spec sheets — run a large
  matrix multiply and a streaming copy and see what you actually get.
- Compute the arithmetic intensity of one forward pass of this model at the real
  token-length distribution from S0. Is this workload compute-bound or
  memory-bandwidth-bound? **Answer this explicitly.** Everything downstream
  depends on it: if it is bandwidth-bound, lower precision and smaller models
  help and better kernels do not; if it is compute-bound, the reverse.
- State what fraction of the roofline the current implementation achieves. If it
  is already at 70%, this spike ends early and says so.

## Lines of attack

Investigate each, measure each, and report the ones that fail:

### 1. Padding waste

Batches are padded to the longest sequence in them. With real chunks spanning
roughly 30 to 384 tokens, a naively-batched run may spend a large share of its
compute on padding tokens. Quantify it exactly: total tokens processed versus
useful tokens, at the S0 distribution.

Then attack it. Length-bucketed batching (sentence-transformers already sorts
internally — verify whether it actually helps at these sizes, and whether it
survives the 256-chunk Chroma batch boundary). Token-budget batching, where a
batch is "as many sequences as fit in N tokens" rather than a fixed count.
Sequence packing, where multiple short chunks are concatenated into one padded
slot with an attention mask that keeps them from seeing each other — this
eliminates padding entirely but requires attention that supports variable-length
segments, which may or may not exist on Apple's backend. Find out.

### 2. Why bigger batches got slower

**Start with GPU frequency residency** (PROTOCOL.md §2a), because it partitions
the hypothesis space in one measurement. If the GPU sits at a low P-state with
high idle residency during the slow run, it is being starved by something
upstream and the cause is not in the kernels at all. If it is pinned at a high
frequency with low idle residency and still slower, the cause is genuinely on
the device.

Chase the batch-128 anomaly to a root cause. Candidates: memory pressure and
unified-memory paging, a kernel that falls off a fast path at some size, the
sort-then-batch interaction, thermal throttling during a longer run, or
measurement error. Do not accept "it's just noise" without demonstrating it.
Produce a throughput-versus-batch-size curve with variance bands, at several
sequence lengths, and explain its shape.

### 3. Precision

Full, half (fp16), and brain-float (bf16) precision, and where available, 8-bit.
Report throughput and — mandatory, per PROTOCOL.md §4 — retrieval quality from
the S0 eval set, plus numerical drift against a full-precision reference
(cosine similarity of the resulting vectors, and rank correlation of retrieval
results). Half precision has a narrow exponent range; check for overflow on
outlier inputs rather than assuming it is fine.

### 4. Graph-level optimization

Whatever torch offers on this backend: compilation, static shapes, fused
attention, avoiding host/device round trips, avoiding CPU-side tokenization
stalling the GPU. Support for these on Apple's MPS backend is uneven and
version-dependent — establishing what actually works on torch 2.10 is itself a
deliverable. Measure the tokenizer separately; if tokenization is single-threaded
Python and the GPU is waiting on it, that is the whole game.

### 5. Small-batch latency

The current code switches to CPU below 24 items because of per-call GPU
overhead. Find out what that overhead is made of and whether it is removable —
warm buffers, persistent graphs, avoiding re-entering the framework. The watch
daemon lives in this regime, so a fix here matters more than bulk throughput.
If the 24 threshold can be driven to zero, a whole branch of configuration logic
disappears.

### 6. Sustained versus burst

Run for 20+ minutes and report the throughput curve as the machine heats. Report
on battery and on AC. A 3x burst win that decays to 1.2x sustained is a
different product decision, especially for a background daemon on a laptop.

## Adversarial mandate

- Try to show the current implementation is close enough to the roofline that
  tuning is pointless. That would be an excellent result — it would redirect the
  program to S5 and S6, where the upside is quality rather than speed.
- For every optimization that wins, find its failure case: the input shape,
  batch size, or corpus where it loses. Report the crossover.
- Be suspicious of any speedup above 2x from a configuration flag. Verify the
  output vectors are still correct — compare against a full-precision reference
  before believing the number. Fast wrong answers are easy.
- Check that optimizations survive contact with the real pipeline. A win in a
  tight benchmark loop that vanishes when the embedder is called through Chroma
  with 256-chunk batches is not a win.

## Deliverables

1. Roofline analysis with the explicit compute-bound / bandwidth-bound verdict.
2. Padding waste quantified at the real token distribution, and the best
   mitigation measured.
3. Root cause of the batch-128 slowdown.
4. A tuned configuration: precision, batch policy, threading — with a patch
   under `spikes/` behind an off-by-default flag, and both throughput and
   quality numbers.
5. Small-batch latency floor, and whether the CPU/GPU threshold can be removed.
6. Sustained-throughput and energy curves.
7. `S2-VERDICT.md` per PROTOCOL.md §7, stating what fraction of peak is now
   reached and what the remaining gap is made of.

## Kill conditions

- Already within 20% of achievable roofline: report it, recommend skipping
  kernel work in S3, and push the program toward S5/S6.
- Every optimization that works requires a torch version or backend feature that
  is unstable on this platform: say so, and hand the problem to S3 as evidence
  that the runtime itself needs replacing.
