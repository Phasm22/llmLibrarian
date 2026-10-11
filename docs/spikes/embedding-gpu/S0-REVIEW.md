---
description: Adversarial review of the S0 verdict — corpus findings stand, but the headline "55.8% overstated" claim compares three changed variables at once and the dictionary refutation tested the wrong workload. Read alongside S0-VERDICT.md.
type: decision
updated: 2026-09-20
---

# Review of S0-VERDICT.md

Written by the program lead, not by the S0 agent. [S0-VERDICT.md](S0-VERDICT.md)
is left unedited so the audit trail stays intact. Per
[PROTOCOL.md](PROTOCOL.md) §1, verdicts get attacked too.

**Summary: the corpus work is excellent and stands. Two of the three headline
claims do not.**

## What stands, and is valuable

- **The token histogram.** 9,550 chunks, median 296 tokens, only 16.96% at the
  384 ceiling. This is the program's most reused number and it was measured
  properly.
- **The per-shape spread**, which is a genuinely important finding: PDF layout
  chunks cap at 55.08% and tax/financial at 39.45%, against 1.76% for prose.
  The instruction that every later spike must report multiple shapes is correct
  and should be enforced.
- **Refusing to publish an energy number** when the idle baseline was invalid.
  Exactly right. A fabricated joules figure would have poisoned S3.
- **The thermal decay observation** — five MPS runs falling monotonically from
  24.55 to 19.62 chunks/s while GPU frequency dropped 666 → 553 MHz at >82%
  residency. This is the most useful new fact in the verdict, and it has
  consequences the verdict did not draw out (see below).

## Correction 1: the speedup comparison changed three variables at once

The verdict compares its measured **21.64 chunks/s** against the prompt's
**33.7** and concludes the old number was "55.8% too high". But:

| | Original benchmark | S0 measurement |
|---|---|---|
| Precision | **fp16** (33.7) — fp32 was **26.3** | fp32 |
| Batch size | 64 | 32 |
| Thermal state | single burst pass | median of 5 consecutive, decaying |
| Fixture | synthetic, 74% capped | real, 14% capped |

Compared like-for-like — fp32 against fp32, burst against burst — it is
**26.3 vs 24.55**, about 7% apart. The 55.8% gap is mostly a precision
difference being attributed to fixture realism.

The prompt is partly at fault: it quoted the fp16 headline (33.7) next to the
fp32 CPU number (10.7) to form the 3.15× claim, which was itself a sloppy
comparison. That error is now corrected in both directions.

**The consequence matters more than the correction.** Half precision was never
measured on real text. Its ~1.28× contribution (26.3 → 33.7) is still entirely
unverified at the real token distribution, and it is a live proposal in S2. The
honest current state is: **fp32 real-text acceleration is 2.02×, and fp16 on
real text is unmeasured.** Not "the speedup was overstated".

## Correction 2: the dictionary refutation tested a different workload

The verdict states the original benchmark could not be found. It existed in a
session scratchpad rather than the repo, and is now preserved verbatim with
provenance notes at
[`spikes/harness/reference/bench_original_2026-09-20.py`](../../../spikes/harness/reference/bench_original_2026-09-20.py).

The reconstruction joined dictionary words into the product's **1,000-character**
chunk size. The original joined **30–380 words**, which is a median of **2,160
characters** — more than twice as long. Measured token distributions:

| Generator | Median tokens | At 384+ |
|---|---:|---:|
| Original (30–380 words) | 676 | **74.2%** |
| S0 reconstruction (1,000 chars) | 294 | **0%** |

So the original claim — that the synthetic benchmark was dominated by capped
chunks — was substantially correct. The refutation measured a workload less than
half as long and found, unsurprisingly, no capping.

The verdict's own caution ("calling a new workload *the exact benchmark* would
be false") was the right instinct, applied to the wrong conclusion: it correctly
declined to claim reproduction, then drew a strong refutation from the
non-reproduction anyway. With the source recovered this is now settled, and the
reconstruction should be retired rather than kept as a fixture.

## Correction 3: the CPU coincidence deserves suspicion, not confirmation

The verdict marks the 10.7 CPU figure "confirmed", having measured 10.697 — to
three significant figures, across a different fixture (294 vs 676 median
tokens), a different batch size, and a different thermal regime.

Agreement that exact across that many changed variables is more likely to be
telling us something than to be luck. A plausible reading: the CPU path is not
token-count-sensitive in the way the GPU path is, because it is bound by
something invariant — thread count, memory bandwidth, or a fixed per-call
overhead. If true, that is a real finding about the CPU path and it belongs in
S2's roofline analysis. It should be chased, not filed as a confirmation.

## The protocol bug this exposes

[PROTOCOL.md](PROTOCOL.md) §2 says: five runs, discard the first as warmup,
report the median. On this machine the MPS runs decayed monotonically. Under a
monotonic decay, a median of five consecutive runs measures neither burst nor
steady state — it measures the middle of a thermal ramp, and the answer shifts
with how many runs you happened to do. Worse, the rule discards the *first* run
as "warmup" when on a thermally-limited chip the first run is the least
throttled and the most comparable across sessions.

This would have silently corrupted every throughput comparison in the program.
It is now fixed in §2. S0's own baseline numbers should be re-run under the
corrected rule before S2 or S3 use them.

## Standing of the gate

S0's own conclusion — inconclusive, gate open — is correct and I am not
overriding it. Two repairs remain outstanding: a quiet ten-minute energy run,
and relevance labels that can resolve small quality differences (known-item
recall@12 of 0.44 is too weak for S5's model comparisons).

Adjusting what that gate blocks:

- **S1 may start now.** It profiles where wall-clock time goes and builds the
  simulator. It needs the fixtures, which are sound, and needs neither energy
  attribution nor fine-grained relevance labels.
- **S5 and S6 stay blocked.** Both are decided on small quality differences,
  which S0 has explicitly stated it cannot yet resolve.
- **S2 and S3 stay blocked on energy**, and must re-run the baselines under the
  corrected thermal rule before quoting any throughput number.
