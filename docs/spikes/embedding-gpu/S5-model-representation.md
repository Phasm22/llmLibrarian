---
description: Spike prompt S5 — find the embedding model and vector representation that maximize retrieval quality per millisecond and per megabyte. Full reindex is explicitly cheap, so dimension and model changes are all in scope.
type: plan
updated: 2026-09-20
---

# S5 — Model and representation: quality per millisecond, quality per megabyte

> Paste everything below into a fresh agent session. Budget: 1–2 weeks.
> Requires: S0 complete (the eval set is the whole basis of this spike).
> Can run in parallel with S1–S4; it does not depend on any hardware finding.

---

## Context

You are working in `llmLibrarian` (this repo): a local document index over a
Chroma vector store, queried through MCP tools or `llmli ask` with a local
Ollama model. Read [CLAUDE.md](../../../CLAUDE.md) and
[docs/TECH.md](../../TECH.md). Read [PROTOCOL.md](PROTOCOL.md); it binds you.

Current setup: `all-mpnet-base-v2`, 768 dimensions, 384-token maximum, released
in 2021. Chunks are 1000 characters with 150 overlap, capped at 50 per file
([src/constants.py](../../../src/constants.py)). Retrieval is hybrid with
reciprocal rank fusion ([src/query/retrieval.py](../../../src/query/retrieval.py)),
followed by diversification and dedup, with an optional cross-encoder reranker
over 40 candidates ([src/reranker.py](../../../src/reranker.py)). Queries are
routed by intent — lookup, aggregate, tax, code, file-list, timeline and others
([src/query/intent.py](../../../src/query/intent.py)). The live store is ~364 MB.

There is a guard that refuses a model whose vector dimension differs from what a
collection already holds ([src/embeddings.py](../../../src/embeddings.py),
`validate_embedding_dimension`), because changing dimensions requires
reindexing everything.

**The project owner has stated that a full reindex is cheap and not a
constraint.** Do not let that guard narrow your search. Treat model choice,
dimension, and storage format as fully open.

## Your question

**What model and vector representation give the best retrieval quality for the
latency and storage this system can afford?**

Two frontiers, not one number: quality per millisecond, and quality per
megabyte. Report both, and report where the current configuration sits on each.

## Dimensions of the search

### 1. Models

Survey what is actually good now — the field moved a great deal since 2021.
Verify current standings yourself from retrieval benchmarks and from your own
measurements; do not trust any list, including this one. Families worth
examining: modern small encoders in the 100–600M parameter range, embedding
models distilled from recent LLMs, and multi-capability models that emit dense,
sparse and multi-vector representations at once. Note for each: dimension,
maximum sequence length, license, whether it needs instruction prefixes for
queries versus documents (getting this wrong silently costs a lot of quality),
and multilingual coverage if the corpus needs it.

Sequence length deserves special attention. The current model caps at 384
tokens, which is part of why chunks are 1000 characters. A model with a longer
context changes the chunking strategy — which means chunk size is a variable in
this spike, not a constant. Test whether larger chunks with a longer-context
model beat more smaller chunks.

### 2. Dimension and storage format

- **Matryoshka-style truncation**, where a model is trained so that the first N
  dimensions are usable alone. If a 1024-dim model truncated to 256 keeps most
  of its quality, storage and search cost drop sharply.
- **Quantization of stored vectors**: 8-bit, and binary. Binary vectors with a
  Hamming-distance first pass and full-precision rescoring of the top candidates
  is a well-established pattern; test whether Chroma's query path can express
  it, and what it costs to bolt on if not.
- Measure the full tradeoff surface: index size, query latency, memory, recall.
  Include what happens at 10x the current corpus, since storage decisions are
  the hardest to reverse.

### 3. Fit to this corpus specifically

The index holds a genuinely mixed bag: prose notes, source code, PDFs, tax
documents, transcripts. General benchmark leaderboards will not tell you which
model handles *this* mixture. Report per-shape and per-intent results from S0's
eval set. A model that wins overall while losing badly on code or on tax lookups
may be the wrong choice, because those are routed intents with specific
extraction logic behind them ([src/tax/](../../../src/tax/)).

Also check the image side. CLIP handles images today
([src/image_embeddings.py](../../../src/image_embeddings.py)) in a separate
collection. Ask whether a newer multimodal embedder would let text and image
search share one space, and what that would buy — it would change how
`ask_image` and photo queries work.

### 4. Interaction with what is already there

Quality changes are not additive. Measure the candidate models **with the
existing reranker on and off**, and with hybrid fusion on and off. A strong
reranker may erase the difference between two embedders — in which case the
cheapest embedder wins and this spike's answer is "keep mpnet, spend the budget
elsewhere". Test for that explicitly.

## Adversarial mandate

- **The hypothesis to attack is that a newer model helps at all.** Run
  mpnet-plus-reranker against each candidate at an equal total latency budget.
  If the old model in a good pipeline matches a new model in a naive one, that
  is the finding.
- Beware benchmark contamination. Public leaderboards are heavily optimized
  against; a model tuned for them may underperform on a personal document
  corpus that looks nothing like the benchmark data. Your S0 eval set is the
  only honest arbiter you have — and S0 is required to have stated its own
  limits, so quote them.
- Check that quality gains survive the full pipeline, not just raw top-k. This
  system diversifies, dedups, and feeds chunks to a generating model. A model
  that retrieves subtly redundant chunks may score well and produce worse
  answers.
- Test the regression direction. For every candidate that wins overall, find the
  queries it makes worse and characterize them. Real users notice the query that
  used to work and now does not, far more than a percentage point of average
  gain.
- Account for the operational cost: model size, load time, memory, whether it
  runs acceptably on the backends S3 is evaluating, and whether it needs a
  conversion step that blocks future changes.

## Deliverables

1. A quality-per-millisecond frontier and a quality-per-megabyte frontier, with
   the current configuration marked on both.
2. Per-shape and per-intent breakdown for the top candidates.
3. A recommendation for model, dimension and storage format, with the reindex
   cost stated as a measured duration.
4. A chunking recommendation if the winning model's context length changes it.
5. The list of queries that get worse under the recommendation.
6. `S5-VERDICT.md` per PROTOCOL.md §7.

## Kill conditions

- No candidate beats mpnet-plus-reranker at equal latency on the real eval set:
  recommend keeping the current model, and say what that implies for S6.
- The eval set cannot distinguish the top candidates (differences inside its
  noise band): report the minimum detectable difference and recommend choosing
  on cost and operational simplicity instead of quality.
