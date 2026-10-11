---
description: Spike prompt S3 — benchmark every viable embedding runtime on Apple Silicon and the Linux box, and test whether the Neural Engine can embed while the GPU serves Ollama. Run after S1 says embedding matters.
type: plan
updated: 2026-09-20
---

# S3 — Backend shootout, and heterogeneous compute

> Paste everything below into a fresh agent session. Budget: 5–7 days.
> Requires: S0 complete. S1 must not have cancelled this spike. Coordinate with
> S2 — use its tuned torch configuration as the baseline, not the untuned one.

---

## Context

You are working in `llmLibrarian` (this repo): a local document index over a
Chroma vector store, queried through MCP tools, or through `llmli ask` which
retrieves and then generates with a **local Ollama model on the same machine**.
Read [CLAUDE.md](../../../CLAUDE.md) and [docs/TECH.md](../../TECH.md). Read
[PROTOCOL.md](PROTOCOL.md); it binds you.

Embeddings run `all-mpnet-base-v2` through sentence-transformers on torch
([src/embeddings.py](../../../src/embeddings.py)). Images run CLIP through
open_clip ([src/image_embeddings.py](../../../src/image_embeddings.py)), pinned
to CPU after a measurement showed Apple's GPU backend pinned ~1.6 GB
permanently while buying nothing — 127 ms per image on GPU versus 124 on CPU,
because image decode dominated. That comment is a good model for the kind of
finding this spike should produce.

Hardware: Apple M4 Pro, 48 GB unified memory. Plus a Linux desktop running the
`pc-stacks` host runtime (see [AGENTS.md](../../../AGENTS.md)) whose **GPU is
unknown** — establishing what it has is part of your job.

## Your question

**Which runtime should compute embeddings on each machine — and can we use more
than one compute unit at the same time?**

## Part 1: the shootout

Benchmark every viable runtime on the same model, same S0 fixtures, same
harness. Candidates to investigate, verifying availability and maturity for
yourself rather than trusting any of these descriptions:

- **torch on MPS**, tuned per S2. The baseline everything must beat.
- **MLX** — Apple's own array framework, designed around unified memory. Check
  what embedding-model support exists and whether this model or an equivalent
  can be converted.
- **Core ML** — the route to Apple's **Neural Engine**, a separate accelerator
  from the GPU. Conversion from a BERT-family encoder is the risk; fixed input
  shapes may be required, which interacts badly with variable chunk lengths.
  Core ML will silently run on GPU or CPU when an operation is unsupported, so
  **every Neural Engine claim in this spike must be proven with a nonzero
  `ane_power` reading** from S0's energy wrapper (sudo for `powermetrics` is
  granted — see PROTOCOL.md §2). A "Neural Engine" result that is really the GPU
  wearing a hat would corrupt the most important finding in this spike.
- **ONNX Runtime** with the CoreML execution provider. Note that Chroma's own
  default embedding path already uses this (`LLMLIBRARIAN_EMBEDDING=default`,
  MiniLM, 384-dim) — so part of this is already in the codebase, unmeasured.
- **llama.cpp / GGUF embedding models** — Metal backend, aggressive
  quantization, and a server mode that would dovetail with S4.
- Anything else you find that is credible. Justify inclusions and exclusions.

On the Linux box: first determine what GPU exists. Then, if it is NVIDIA,
evaluate the server-grade options (Hugging Face's text-embeddings-inference,
ONNX Runtime with TensorRT, vLLM-style batching) and report what a second node
would contribute. If it has no usable GPU, say so and close that branch.

For each backend report: throughput on real fixtures, batch-1 latency, memory
resident **and whether it is released on unload**, energy per million tokens,
sustained versus burst, model load time, conversion effort, dependency weight,
and numerical agreement with the torch full-precision reference. A backend that
produces subtly different vectors is not a drop-in replacement — quantify the
drift and run the S0 eval set.

Dependency weight is a real criterion, not a footnote. A backend that pins a
torch version, adds a gigabyte of wheels, or breaks on OS updates costs the
project something even if it wins on speed.

## Part 2: the interesting part — heterogeneous compute

This is where the genuine unknowns are, and where a result would be worth more
than any single-backend speedup.

### Can embedding and generation run at once?

`llmli ask` retrieves and then generates with Ollama, which wants the GPU. The
watch daemon may be indexing at the same time. Today both contend for one
device, and nobody has measured what that does.

Test it: run a sustained embedding load and a sustained Ollama generation
simultaneously. Measure the throughput and latency of both, against each running
alone. Then test the interesting configuration — **embeddings on the Neural
Engine, generation on the GPU** — and see whether the interference disappears.
If it does, that is a structural argument for a Core ML path regardless of
whether it wins a solo benchmark, and it changes what S4 should build.

### Can one batch be split across units?

CPU, GPU and Neural Engine can in principle all run at once on unified memory.
Test a work-splitting scheme that dispatches portions of a batch to each,
proportional to measured rates. Report aggregate throughput against
best-single-unit, and the overhead of coordination. This may well be a
negative result — memory bandwidth is shared, so three units may simply contend
for the same resource. Find out which it is, and say so.

### Does the small-batch regime have a different winner?

The watch daemon embeds one file at a time. The Neural Engine is often better at
low-latency small work and much better on energy, which is exactly the daemon's
profile. Check whether the right answer is **different backends for different
workloads**, routed by batch size — a generalization of the CPU/GPU threshold
already in the code. If so, S4's server design has to accommodate it.

## Adversarial mandate

- Assume every backend's published benchmarks are marketing until you reproduce
  them on the real fixtures. Report reproduction failures; they are findings.
- The most likely true outcome is that tuned torch wins on effort-adjusted
  terms and nothing justifies a rewrite. Try to reach that conclusion honestly
  before trying to beat it.
- Weight conversion friction properly. A backend needing a hand-maintained model
  conversion step blocks every future model change — which directly undermines
  S5. Say so if it applies.
- For the heterogeneous work, the null hypothesis is that shared memory
  bandwidth means running two units at once gains nothing. Test it first.
- Check that a backend which looks good in isolation still looks good inside a
  long-lived process. S7 is investigating memory residency; share findings both
  ways.

## Deliverables

1. Backend matrix across all metrics, on real fixtures, with variance.
2. The Linux box's actual hardware and what it would contribute.
3. Contention measurements: embedding versus Ollama, alone and concurrent, on
   the same unit and on different units.
4. A verdict on batch-size-routed backend selection.
5. A recommended backend per machine and per workload, with the cost of each.
6. `S3-VERDICT.md` per PROTOCOL.md §7.

## Kill conditions

- No backend beats tuned torch by more than 25% on real fixtures, and the
  heterogeneous results are null: recommend staying on torch, and hand S4 a
  simpler problem.
- Core ML conversion of this model class proves impractical: close the Neural
  Engine branch explicitly, with the specific blocker, so nobody reopens it
  casually.
