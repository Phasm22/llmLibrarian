---
description: S0 verdict — representative fixtures and a reusable harness exist, but energy attribution and fine-grained retrieval judgments are not yet trustworthy enough to unblock the full program.
type: decision
updated: 2026-09-20
---

# S0 verdict — the old speedup was overstated; the gate remains open

**One-sentence answer:** representative timing is now believable enough to
reject the old 3.15× GPU claim, but S0 is **inconclusive (medium confidence)**
because whole-chip energy cannot be attributed on the busy host and the eval
set only proves sensitivity to large retrieval regressions.

Do not treat this file as approval to run S5/S6 or to make energy claims. S1 may
use the timing fixtures for exploratory critical-path work, but all downstream
go/no-go decisions remain gated on the two repairs in [Recommendation](#recommendation).

## Verdict and confidence

| Claim | Verdict | Confidence | Evidence |
|---|---|---:|---|
| The production corpus usually hits the 384-token ceiling | Refuted | High | 1,620 of 9,550 eligible chunks (16.96%) reach it; median is 296 tokens. |
| The reported 33.7 chunks/s MPS baseline represents this corpus | Refuted | Medium | Real mixed text measured 21.64 chunks/s median; the earlier number is 55.8% higher. |
| The reported 10.7 chunks/s CPU baseline represents this corpus | Confirmed for this machine state | Medium | Real mixed text measured 10.70 chunks/s median. |
| S0 eval detects deliberate sabotage | Confirmed | Medium | 64-d truncation and random retrieval both produce material recall/nDCG losses. |
| S0 eval can resolve small model-quality differences | Not established | High | Labels are retrieval-output proxies and known-item recall@12 is only 0.44. |
| Net joules can be compared on the current host | Refuted for this run | High | Measured idle package power exceeded every work-run mean; CPU-only runs also contained unrelated GPU activity. |

The current real-text speedup is **2.02×** (21.64 / 10.70), not 3.15×
(33.7 / 10.7). The old speedup ratio was therefore **55.7% too high relative
to the measured ratio**. Both configurations met the protocol's variance
threshold: CPU p95/p5 was 1.22 and MPS p95/p5 was 1.24.

These are timing findings, not a production recommendation. The five MPS runs
slowed monotonically from 24.55 to 19.62 chunks/s while mean GPU frequency fell
from 666 to 553 MHz. GPU active residency stayed above 82%, so this does not
look like simple upstream starvation. A continuous ten-minute run is still
required before S2 or S3 may use a sustained-throughput number.

## The corpus measurement

The snapshot read 9,550 chunks from the live HTTP Chroma service without
writing to it. It excludes `/spikes/` so building this experiment cannot change
its own source distribution. Rebuild details and content digests are in
[`fixtures-manifest.json`](../../../spikes/harness/fixtures-manifest.json); all
sampled text stays under gitignored `spikes/private/s0/`.

### Full-corpus token histogram

| Untruncated MPNet tokens | Chunks | Share |
|---:|---:|---:|
| 0–32 | 211 | 2.21% |
| 33–64 | 68 | 0.71% |
| 65–96 | 136 | 1.42% |
| 97–128 | 136 | 1.42% |
| 129–192 | 211 | 2.21% |
| 193–256 | 1,925 | 20.16% |
| 257–320 | 3,285 | 34.40% |
| 321–383 | 1,958 | 20.50% |
| 384+ | 1,620 | 16.96% |

The corpus contains 3,242,776 untruncated tokens and 2,769,758 actual model
input tokens after the 384-token ceiling. Median is 296, p95 is 553.5, and p99
is 1,789.5 tokens. The extremely long tail is real extracted-document content,
not the typical workload.

### Shape differences

| Fixture | Chunks | Median | p95 | At 384+ |
|---|---:|---:|---:|---:|
| mixed | 512 | 288 | 468.9 | 14.26% |
| prose/notes | 512 | 262 | 345.4 | 1.76% |
| source code | 512 | 318 | 411.0 | 12.70% |
| PDF layout | 512 | 393.5 | 733.8 | 55.08% |
| tax/financial | 512 | 331 | 2,273.0 | 39.45% |
| transcripts | 91 | 271 | 373.0 | 3.30% |
| longest 5% tail | 478 | 846.5 | 3,454.3 | 100% |

Fixture choice can plainly change padding cost. Every later throughput spike
must report mixed, PDF, tax/financial, and at least one uncapped shape; a single
mixed average will hide the operationally important difference.

## The dictionary benchmark was not reproducible as described

No source for the original benchmark exists in the repository, so the word
count, character count, seed, batch construction, precision, and warmup policy
cannot be recovered. Calling a new workload “the exact benchmark” would be
false.

The closest reproducible reconstruction joins seeded random entries from
`/usr/share/dict/words` into the product's 1,000-character chunk size. Across
512 chunks it has median 294 tokens, p95 306.4, and **zero** chunks at the
384-token ceiling. That directly refutes the claim that random dictionary
words plus a 1,000-character chunk necessarily create an all-capped workload.
Its manifest is
[`dictionary-manifest.json`](../../../spikes/harness/dictionary-manifest.json).

What can be said quantitatively is narrower:

- the old CPU number (10.7 chunks/s) matches the current representative result
  (10.70) to the reported precision;
- the old MPS number (33.7) is 55.8% above the current representative result
  (21.64);
- consequently the claimed CPU→MPS multiplier falls from 3.15× to 2.02×.

The earlier MPS result was materially optimistic, but the evidence does **not**
support blaming dictionary tokenization. Its exact generator would have to be
recovered to identify the cause.

## Retrieval eval and sabotage

[`eval-manifest.json`](../../../spikes/harness/eval-manifest.json) describes 121
private cases: 71 unique query/silo pairs harvested from 109 audit events and
50 deterministic known-item queries. Repeated historical queries had mean
source-set Jaccard agreement 0.872 across 49 adjacent event pairs.

Only 113 cases resolve against the current silo roster. Results are exact
cosine retrieval over the production vectors:

| Representation | Recall@12 | nDCG@12 | Recall@40 | nDCG@40 |
|---|---:|---:|---:|---:|
| full 768-d | 0.555 | 0.431 | 0.666 | 0.457 |
| first 64 dimensions, renormalized | 0.494 | 0.364 | 0.584 | 0.387 |
| deterministic random ranking | 0.271 | 0.157 | 0.323 | 0.177 |

The eval detects the mandated sabotage: 64-d truncation costs 11.0% relative
recall@12 and 15.4% relative nDCG@12; random retrieval is worse again. This is
enough to catch a large regression.

It is not independent ground truth. The audit records what the old retriever
returned, not a click, accepted answer, or human relevance judgment, so it
advantages the baseline. The query distribution is also 119 LOOKUP versus two
EVIDENCE_PROFILE cases, with no meaningful coverage of the other router
families. Known-item recall@12 is only 0.44. This set cannot yet adjudicate a
2–5 point model difference, aggregate behavior, tax correctness, or reranker
quality.

## Energy validation failed safely

The wrapper correctly parses the CPU/GPU/ANE rails and GPU residency from the
verified `cpu_power,gpu_power` sampler. MPS work raised mean GPU power to
3.48–4.36 W and held active residency at 82–85%; ANE remained approximately
zero, as expected.

Net energy subtraction was invalid in every run. The pre-run idle mean was
13.1 W for MPS and 12.6 W for CPU, higher than each measured work mean. A
separate long-running Python agent was consuming roughly 14% CPU, and CPU-only
runs showed unrelated GPU activity (one window averaged 2.98 W GPU and 40%
active residency). The required “CPU-only shows zero GPU work” validation
therefore failed.

The runner emits `joules_per_1m_tokens: null` in this condition. It retains
gross upper bounds (1,489 J/M input tokens MPS; 3,214 CPU) for diagnosis, but
they are **not attributable energy numbers** and must not support a decision.

## What was built

- [`spikes/harness/`](../../../spikes/harness/) — fixture builder, dictionary
  reconstruction, eval builder/scorer, benchmark runner, and shared
  `powermetrics` parser. Worth keeping.
- `spikes/private/s0/` — 8.4 MB of sampled personal text and private queries.
  Throwaway/rebuildable and gitignored.
- [`spikes/results/S0/`](../../../spikes/results/S0/) — baseline and sabotage
  JSON. Worth keeping as provenance; invalid energy fields are explicitly null.
- [`test_spike_harness.py`](../../../tests/unit/test_spike_harness.py) — shape,
  histogram, digest, and power-parser checks. Worth keeping.

No production source path, live collection, registry, or database was mutated.
The fixture builder used the already-running Chroma HTTP service read-only, so
there was no embedded second-writer risk and no reindex was necessary.

## What surprised me

The dictionary premise failed before the benchmark did: at the real 1,000-
character chunk size, dictionary text and mixed real text have nearly identical
median token counts. The meaningful distribution split is document shape,
especially PDF and tax extraction.

The second surprise was directional MPS decay inside only two minutes despite
no macOS thermal warning. A five-run percentile summary alone makes the run
look controlled; the ordered trace shows a trend that the summary conceals.

The third was that `powermetrics` can identify the target GPU rail correctly
while whole-package baseline subtraction is still unusable. Instrument access
is solved; experimental isolation is not.

## What remains unanswered

1. Net joules per million tokens under a quiet, controlled host state.
2. Ten-minute sustained MPS throughput and whether the falling frequency
   stabilizes or continues.
3. Independent relevance labels with repeated-judge agreement and human spot
   checks, especially outside LOOKUP.
4. Why the uncommitted original dictionary benchmark produced 33.7 chunks/s
   and allegedly capped nearly every input.
5. Whether code and prose actually reverse backend rankings; only shape
   distributions, not per-shape backend timings, were completed here.

## Recommendation

Keep the harness, but **do not close S0 yet**.

1. Rerun energy and sustained timing in a quiet measurement window: stop other
   agent/model workloads, confirm a five-minute stable idle rail, require CPU-
   only GPU power to remain at baseline, then collect one continuous ten-minute
   MPS trace plus five timed windows. Cost: about one hour and temporary loss of
   interactive local-model/agent work.
2. Pool results from full MPNet, 64-d truncation, and one deliberately different
   model; judge the pool twice with blinded ordering, human-check at least 30
   stratified query/result pairs, and require agreement before using small
   deltas. Cost: roughly half a day of labeling plus private local inference.

S0 becomes sufficient when net energy is positive and repeatable, the sustained
trace has no unexplained trend, and the judged eval both catches sabotage and
shows measured repeat agreement. I would change this verdict sooner if the
original benchmark source is recovered and proves a different, production-
representative construction.
