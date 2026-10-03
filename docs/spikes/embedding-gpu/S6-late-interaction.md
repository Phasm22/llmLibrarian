---
description: Spike prompt S6 — evaluate multi-vector / late-interaction retrieval (ColBERT-style) against the existing cross-encoder reranker at equal GPU budget. Highest upside and highest risk in the program.
type: plan
updated: 2026-09-20
---

# S6 — Late interaction: does multi-vector retrieval earn its cost?

> Paste everything below into a fresh agent session. Budget: 1–2 weeks.
> Requires: S0 complete, S5 verdict available as the single-vector baseline.

---

## Context

You are working in `llmLibrarian` (this repo): a local document index over a
Chroma vector store, queried through MCP tools or `llmli ask`. Read
[CLAUDE.md](../../../CLAUDE.md) and [docs/TECH.md](../../TECH.md). Read
[PROTOCOL.md](PROTOCOL.md); it binds you.

Retrieval today compresses a whole chunk into a single 768-dimensional vector,
searches by vector similarity fused with keyword results
([src/query/retrieval.py](../../../src/query/retrieval.py)), diversifies and
dedups, and optionally reranks the top 40 with a cross-encoder — a model that
reads the query and the chunk together and scores them
([src/reranker.py](../../../src/reranker.py)). That reranker is the only part of
the query path that does substantial compute per query.

## Your question

**Would keeping one vector per token, and scoring queries against all of them,
retrieve better than one vector per chunk — by enough to justify the storage and
the query-time compute?**

This is the most speculative spike in the program and the one with the largest
possible upside. It is also the one most likely to end in "no". Both outcomes
are worth the time, because right now nobody knows which it is.

## The idea, and what makes it hard

Late interaction keeps a small vector per token instead of collapsing the chunk
to one vector, and scores a query by matching each query token against its best
match in the document and summing. It preserves detail that single-vector
embeddings lose, and it tends to help most on exactly the queries this system
handles badly: specific terms, names, code identifiers, numbers in documents.

The costs are real and must be measured, not assumed:

- **Storage** grows by roughly the token count per chunk, before compression.
  Against a 364 MB index, naive storage could be catastrophic. Compression of
  these vectors (aggressive quantization, centroid-based encoding) is standard
  practice and is part of what you must evaluate.
- **Chroma does not natively store or search multi-vector documents.** You will
  have to design around that: a separate store, or an encoding into the existing
  one, or a second index consulted only for reranking.
- **Query-time compute** is the point where the GPU actually earns its place in
  the query path — which makes this spike relevant to S3 and S4's server design.

## The shape that probably works, and should be tested first

Do not try to replace the first-stage retriever. Test late interaction as a
**reranking stage**: dense retrieval fetches candidates as it does today, then
multi-vector scoring reorders them. This confines storage to what you choose to
keep, avoids rebuilding the search index, and slots into the existing pipeline
where the cross-encoder already sits.

Then, only if that wins, ask whether full multi-vector first-stage retrieval is
worth it.

## What to measure

1. **Quality against the right baseline.** Not against raw dense retrieval —
   against dense retrieval *plus the existing cross-encoder reranker*, which is
   the real system. Use S5's winning single-vector configuration.
2. **Equal-budget comparison.** Give each approach the same query-time
   milliseconds and see which spends them better. A cross-encoder over 40
   candidates and multi-vector scoring over 200 may cost the same; the fair
   question is which wins at a fixed latency.
3. **Per-intent and per-shape results.** Expect the gains, if any, to be
   concentrated — code identifiers, names, exact figures in tax documents. If
   the win is real but confined to one intent, the recommendation might be to
   route only that intent through it, which is far cheaper than adopting it
   wholesale. This system already routes by intent, so that is a natural fit.
4. **Storage, at compression settings you have verified preserve quality.**
   Report index size at current corpus and at 10x.
5. **Indexing cost.** Multi-vector representations make ingest more expensive
   too. Measure it, and feed the number back to S1's simulator.

## Adversarial mandate

- The null hypothesis is that the existing cross-encoder already captures most
  of the available gain at lower complexity, and that late interaction adds
  storage and a parallel index for a point of nDCG. Try hard to confirm that.
- Watch for the comparison being rigged by compression. It is easy to show a
  gain with uncompressed vectors and quietly lose it at the compression ratio
  that makes storage tolerable. Report quality *at the storage budget you would
  actually ship*.
- Check whether a much simpler intervention gets the same benefit: better
  hybrid-search weighting, a smarter keyword component, query expansion, or
  simply retrieving more candidates before reranking. If tuning the existing
  fusion gets halfway there for a day of work, that belongs in the verdict.
- Be honest about the maintenance surface. A second index type means a second
  thing to repair, rehydrate, and keep consistent — this codebase already has
  `repair`, `rehydrate` and a repair ladder for the one it has. Adding a
  parallel store means adding all of that again.
- If the result is positive, immediately ask what it costs the MCP path
  specifically. MCP returns chunks to a host model; a reranking gain that does
  not change which chunks get returned changes nothing for the user.

## Deliverables

1. A working multi-vector reranking MVP under `spikes/late-interaction/`,
   including the storage design you chose and why.
2. Equal-latency-budget comparison against dense-plus-cross-encoder.
3. Per-intent breakdown, with an explicit answer on whether selective routing
   is the right shape.
4. Storage and indexing cost at shipping-quality compression, at 1x and 10x
   corpus.
5. A recommendation, including the option "route only intent X through this".
6. `S6-VERDICT.md` per PROTOCOL.md §7.

## Kill conditions

- No quality gain over dense-plus-cross-encoder at equal latency: close the
  branch, and record the measurement so it does not get re-proposed.
- Gain exists but requires storage growth the project will not accept even
  compressed: report the quality-per-megabyte point it occupies and let S8
  decide against it with that number in hand.
