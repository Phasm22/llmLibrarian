---
description: Spike prompt S0 — build the corpus fixtures, retrieval eval set, and benchmark harness that every other embedding spike reports into. Run this first; it blocks all other spikes.
type: plan
updated: 2026-09-20
---

# S0 — Ground truth: fixtures, eval set, harness

Current result: [inconclusive verdict — measurement gate remains open](S0-VERDICT.md).

> Paste everything below into a fresh agent session. Budget: 2–3 days.

---

## Context

You are working in `llmLibrarian` (this repo). It indexes folders the user
chooses into a local Chroma vector store and answers questions from that index
only, through MCP tools or a local Ollama model. Read [CLAUDE.md](../../../CLAUDE.md)
and [docs/TECH.md](../../TECH.md) before starting.

Embeddings today: `all-mpnet-base-v2` (768-dim, 384-token ceiling) via
sentence-transformers, device chosen per call by batch size in
[src/embeddings.py](../../../src/embeddings.py). Chunking is 1000 characters with
150 overlap, capped at 50 chunks per file
([src/constants.py](../../../src/constants.py)). Retrieval returns 12 results by
default; with the cross-encoder reranker on, stage one fetches 40
([src/reranker.py](../../../src/reranker.py)). The live store is ~364 MB.

A program of eight further spikes is about to investigate embedding backends,
GPU utilization, model choice and retrieval architecture. **Every one of them
depends on you.** If your fixtures are unrepresentative or your eval set is
weak, all eight produce confident nonsense.

Read [PROTOCOL.md](PROTOCOL.md) — it binds you too.

## Your question

**What does a measurement of this system have to look like before anyone should
believe it?**

## Why this is not busywork

The benchmark that started this program generated chunks by joining random words
from `/usr/share/dict/words`. Random dictionary words tokenize into unusually
many subword pieces, so nearly every chunk hit the 384-token ceiling. It
reported CPU at 10.7 chunks/s and the GPU at half precision at 33.7 chunks/s —
plausibly a worst case dressed up as typical.

**Your first task is to prove or disprove that.** Measure the real token-length
distribution of the actual index, rerun that exact benchmark against real text,
and report how far off it was. If it turns out to be accurate, that is a finding
too, and it means padding waste is not the story.

## What to build

Under `spikes/harness/`:

### 1. Corpus fixtures

Sample real chunks out of the live index or a copy of the user's real folders,
producing named, versioned, reproducible fixtures. At minimum:

- `s0-mixed-v1` — matches the overall production distribution.
- Per-shape fixtures that isolate what the system actually holds: prose/notes,
  source code, PDFs with extracted layout, transcripts, tax and financial
  documents. These tokenize very differently and may have different optimal
  batching.
- `s0-tail-v1` — the longest 5% of chunks, which dominate padded batch cost.

Record for each: chunk count, token-length histogram, percentage of chunks that
hit the 384-token ceiling, and total tokens. **The token histogram is the single
most reused number in this entire program.** Publish it prominently.

Fixtures must be redistributable within the project — this is the user's
personal data. Prefer a sampling script plus a checked-in manifest of hashes and
statistics over checking in the text itself. If you must materialize text, keep
it out of git and document how to rebuild it.

### 2. Retrieval eval set

This is the hard part and the part that matters most. You need to be able to
answer "did retrieval get better or worse" for any change to the embedding
model, precision, or vector format.

Sources to build it from:

- `~/.pal/logs/query-audit.jsonl` — **real queries the user has actually run**,
  with the per-source-file chunk breakdown each one returned. Read it via
  `pal queries`. This is the most valuable asset available; real query
  distribution beats anything you invent.
- [src/llmli_evals/adversarial.py](../../../src/llmli_evals/adversarial.py) — an
  existing harness that builds a synthetic corpus with contradictions and stale
  documents, then scores factual correctness, abstention behavior and evidence
  grounding. Reuse its structure; do not duplicate it.
- The intent router's categories (`src/query/intent.py`: LOOKUP, AGGREGATE,
  TAX_QUERY, CODE_LANGUAGE, FILE_LIST, TIMELINE, and others). Coverage per
  intent matters, because a model that improves prose lookup may wreck code
  search.

Labeling relevance is the bottleneck. Options, roughly in order of
defensibility: pooled judgments across several retrieval configurations, with an
LLM judge scoring each pooled result and a human spot-check of a sample; known
item search, where you pick a distinctive passage and check whether the query
that should find it does; and click-equivalent signals from the audit log. Pick
deliberately, document the bias each choice introduces, and **measure your own
eval set's reliability** — run the same judgment twice and report agreement. An
eval set nobody has validated is a random number generator with a good
reputation.

Report metrics at the sizes the system actually uses: recall and nDCG at 12
(the default result count) and at 40 (reranker stage one).

### 3. Benchmark runner

One entry point every other spike calls, so results are comparable:

- Takes a fixture, an embedding callable, and a config label.
- Handles warmup, repeats, thermal state capture, variance reporting.
- Emits the PROTOCOL.md results JSON.
- **Measures energy. This is mandatory, and sudo for `powermetrics` has been
  granted by the project owner** — see PROTOCOL.md §2. Joules per million tokens
  is the deciding metric for whether background indexing is acceptable on a
  laptop, and it is the only metric that distinguishes a backend that is fast
  from one that is fast *and* leaves the machine usable.
- Wrap `powermetrics` properly: sample the `cpu_power` block for the duration of
  a run (it carries the CPU, GPU and ANE rails together — see PROTOCOL.md §2 for
  the verified invocation and the sampler-naming trap), attribute the energy to
  the tokens processed in that window, and subtract a measured idle baseline. Get
  this wrapper right once, here, so no other spike has to reinvent it — six
  spikes depend on it and will report incomparable numbers if each rolls its
  own. Validate it: a known-heavy run and a known-idle run should differ the way
  you expect, and the CPU-only configuration should show zero GPU power.
- Has a mode that runs *sustained* for 10+ minutes, to catch throttling.

### 4. A baseline set of results

Run the current production configuration through it and publish the numbers
every later spike will be measured against.

## Adversarial mandate

- Try to make your own eval set produce a wrong answer. Feed it a change you
  know should hurt quality — truncate vectors to 64 dimensions, or swap in a
  deliberately bad model — and confirm it detects the damage. An eval set that
  cannot detect deliberate sabotage cannot detect regressions either.
- Check whether fixture choice alone can flip a conclusion. If code chunks and
  prose chunks rank two backends differently, every later spike needs to report
  per-shape results, and you must say so loudly.
- Look for the case where variance swamps the effect anyone hopes to measure. If
  run-to-run noise on this machine is ±20%, then no spike can honestly claim a
  15% win, and the whole program needs to know that on day one.

## Deliverables

1. `spikes/harness/` — fixtures, eval set, runner.
2. The token-length histogram, published in the verdict.
3. A verdict on the dictionary-words benchmark: how wrong was it, and in which
   direction.
4. Baseline results JSON for the current production config.
5. `S0-VERDICT.md` per PROTOCOL.md §7, including an explicit statement of what
   your eval set can and cannot detect.

## Kill conditions

- If you cannot build an eval set whose sabotage detection works, **stop the
  program** and say so. Every downstream spike that claims a quality-neutral
  speedup would be unfalsifiable.
- If measurement noise on this hardware exceeds the effect sizes the program
  hopes to find, report the minimum detectable effect so later spikes can size
  their claims honestly.
