---
description: Spike prompt S8 — take every spike verdict, attack the combined conclusions, and produce the architecture decision record and build plan. Run last, after the other spikes have landed verdicts.
type: plan
updated: 2026-09-20
---

# S8 — Synthesis: what do we actually build?

> Paste everything below into a fresh agent session. Budget: 3–5 days.
> Requires: verdicts from S0–S7, or explicit notes on which were cancelled and
> why.

---

## Context

You are working in `llmLibrarian` (this repo): a local document index over a
Chroma vector store, queried through MCP tools or `llmli ask` with a local
Ollama model. Read [CLAUDE.md](../../../CLAUDE.md) and
[docs/TECH.md](../../TECH.md). Read [PROTOCOL.md](PROTOCOL.md); it binds you
too, especially the adversarial stance.

Seven spikes have run, investigating: measurement ground truth (S0), where time
actually goes (S1), hardware utilization (S2), embedding runtimes and
heterogeneous compute (S3), a dedicated embedding service (S4), model and vector
representation (S5), multi-vector retrieval (S6), and memory residency (S7).
Their prompts and verdicts are in this directory; their results are under
`spikes/results/`.

## Your question

**Given everything measured, what should be built, in what order, and what
should be explicitly abandoned?**

## Your first job is to distrust the verdicts

Do not simply aggregate. Each spike ran with an incentive to find something, and
each measured its own piece in isolation. Before recommending anything:

- **Check for contradictions.** Where two spikes measured overlapping things,
  do the numbers agree? If S1's profile says embedding is 40% of ingest and
  S4's server claims a 2x end-to-end ingest win, one of them is wrong. Find out
  which.
- **Check that wins compose.** Speedups measured independently rarely add. S2's
  kernel tuning, S3's backend and S4's batching may be three descriptions of the
  same win. If the pipeline simulator from S1 survived validation, use it to
  estimate the combined effect, and state its uncertainty. Then verify the
  combination on real hardware rather than trusting the estimate — the single
  most likely error in this whole program is stacking overlapping wins.
- **Re-derive the bottleneck after each proposed change.** A change that makes
  embedding 3x faster simply relocates the constraint. Name where it lands, and
  check whether the next change in your plan addresses that or something now
  irrelevant.
- **Check the quality claims survived contact.** Any recommendation combining a
  new model, lower precision and quantized storage has stacked three sources of
  quality loss that were each measured alone. Run the combination through S0's
  eval set before recommending it. Non-negotiable.
- **Audit against S0's stated limits.** S0 was required to say what its eval set
  and fixtures cannot detect. Any conclusion resting on an undetectable
  difference must be marked as such.

## Account for complexity honestly

This is the part the project owner specifically wanted decided with evidence
rather than instinct. For each recommendation, produce a concrete ledger:

- New processes to install, start, supervise, and diagnose — and what `pal`
  surface they add, next to the existing `pal chroma` and `pal mcp` commands.
- New dependencies, their size, and what they pin.
- New failure modes, and what the degraded path looks like for a user who does
  not know the system's internals.
- New state to repair. This codebase already has `repair`, `repair-ladder` and
  `rehydrate` for the state it has; anything added needs the equivalent.
- Documentation and first-run setup burden. The product is a tool someone
  installs on their own machine; setup friction is a real cost.
- What a full reindex costs, measured — the owner considers this cheap, so
  record the actual number rather than arguing about it.

Then set that ledger against the measured benefit, per recommendation. Some will
obviously fail this test. Say so bluntly.

## Produce three plans, not one

1. **The free wins.** Changes that are strictly better with no new architecture
   — configuration, precision, batch policy, removing a workaround S7 showed is
   obsolete. These should ship regardless of everything else.
2. **The considered build.** The one or two structural changes whose measured
   benefit clears their complexity ledger. With an order, a rough size, and the
   metric that confirms each landed.
3. **The abandoned.** Everything investigated and rejected, each with the number
   that killed it. Write this section carefully — its purpose is to stop these
   ideas being re-proposed in six months, and it is worth as much as the other
   two.

## Deliverables

1. An architecture decision record in `docs/` following this project's
   conventions, recording the decision, alternatives and consequences.
2. The three plans above.
3. A contradiction report: where spike results disagreed, and how you resolved
   it.
4. A combined-configuration measurement — the recommended stack, end to end, on
   real fixtures, with quality from S0's eval set. Not an estimate.
5. Updates to [README.md](README.md) in this directory marking every spike
   closed with a one-line outcome.
6. A short honest statement of what the program did not find out, and whether
   any of it is worth another spike.

## Adversarial mandate

- The most valuable outcome available to you is discovering that the headline
  recommendation is not supported by the evidence once the numbers are combined.
  Look for that first.
- Consider the null plan seriously: ship the free wins, change nothing
  structural, and spend the effort on retrieval quality instead. If the measured
  ingest and query times are already acceptable for a personal tool on a fast
  laptop, then a faster embedder is engineering for its own sake. Make the case
  for this plan properly before rejecting it.
- Sanity-check against the user-visible experience. If bulk ingest drops from 20
  minutes to 7 but happens twice a month, while a watch-daemon update stays at
  4 seconds and happens two hundred times a day, the program optimized the wrong
  thing. Convert every win into "how often does a human notice this".
