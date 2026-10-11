---
description: Rules of engagement for every embedding/GPU spike — adversarial stance, benchmark hygiene, results schema, and what counts as a finished verdict. Read before starting any S* spike.
type: rule
updated: 2026-09-20
---

# Spike protocol

This applies to every spike in this directory. A spike that ignores it produces
numbers nobody can compare against anything else, which is worse than no numbers.

## 1. Your stance is adversarial

You are not here to confirm the hypothesis. You are here to **try to kill it**
and report honestly whether it survived.

- A spike that returns "refuted, here is the evidence" is a **success**. It saves
  weeks of building. Say so plainly and do not soften it.
- For every proposal you test, build the **dumbest credible alternative** too and
  benchmark them against each other. If the dumb one is within noise, the
  sophisticated one loses. Complexity has to earn its place with a number.
- Actively look for the measurement that would embarrass you. If you find
  yourself with a clean 4x speedup, your next task is to find the workload where
  it is a slowdown. Report both.
- When you report a win, state what it costs: memory, startup time, a new
  process to supervise, a new failure mode, lines of code, a dependency that
  pins a torch version.

## 2. No number without variance

- Minimum **5 runs** per configuration. Report median plus 5th and 95th
  percentile, never a bare mean.
- **Check for monotonic decay before you take a median.** Plot the runs in
  order. If throughput falls run-over-run, you are measuring a thermal ramp, and
  a median across it measures neither burst nor steady state — it measures
  wherever you happened to stop, and it moves if you change the run count.
  S0 hit exactly this: five MPS runs fell 24.55 → 19.62 chunks/s while GPU
  frequency dropped 666 → 553 MHz at >82% residency, and the median was reported
  as if it were a stable figure.
- When decay is present, report **two** numbers and label them: **burst**
  (first timed pass from a cold, thermally-rested machine) and **sustained**
  (the plateau after a 10-minute conditioning run). They answer different
  product questions — burst governs a single interactive query, sustained
  governs bulk ingest — and quoting one where the other belongs is a reporting
  error.
- Do **not** blanket-discard the first run as warmup. On a thermally-limited
  chip the first run is the least throttled and the most comparable across
  sessions. Discard a warmup pass only for framework/kernel warmup, run it
  untimed and explicitly, and say that you did.
- **Never compare across two changed variables.** Precision, batch size,
  fixture, and thermal state each move throughput substantially on this machine;
  changing two at once and attributing the difference to one of them is the
  single most common error this program has produced so far. If you must compare
  to a historical number, verify its precision and batch size first, or
  re-measure it yourself.
- If p95/p5 spread exceeds 1.5x, you have an uncontrolled variable. Find it
  before reporting. (Observed already: the same MPS config measured 23.1 and
  27.1 chunks/s on consecutive runs.)
- Record machine state with every result: power source (battery vs AC), thermal
  state, what else was running.
- **Energy measurement is authorized and mandatory on macOS.** A scoped sudoers
  rule for `powermetrics` is installed at `/etc/sudoers.d/powermetrics`, so
  `sudo -n powermetrics` runs without a prompt and
  `energy.joules_per_1m_tokens` is a required field on every macOS throughput
  result, not an optional one. Sudo is granted for that one binary and nothing
  else; do not extend it.
- **Use both power samplers, verified on this machine (Mac16,7, M4 Pro):**

  ```
  sudo -n powermetrics --samplers cpu_power,gpu_power -i 1000
  ```

  `cpu_power` emits all three rails in one block — `CPU Power`, `GPU Power`,
  `ANE Power`, and `Combined Power (CPU + GPU + ANE)`. `gpu_power` adds the
  frequency-residency table described in §2a, which is worth capturing on every
  GPU run. Passing
  `--samplers ane_power` on its own prints a **header with no power lines at
  all**, which reads like "the Neural Engine reports nothing on this chip". It
  is a sampler-naming quirk, not a hardware limitation. Add `pmset -g therm`
  for thermal state.
- The `ANE Power` line is the only reliable way to confirm work actually reached
  the Neural Engine rather than silently falling back to GPU or CPU. Any claim
  that a backend used the Neural Engine must be backed by a nonzero reading,
  not by the API having accepted the request.

### 2a. GPU residency separates "slow" from "starved"

The `gpu_power` sampler reports active frequency, residency across every
DVFS/P-state, and idle residency. Capture these on every GPU measurement and
put them in `notes`. They answer a question raw throughput cannot: whether the
GPU is *working hard and still slow*, or *barely being fed*.

Verified idle baseline on this machine: 338 MHz (the lowest P-state), 23.68%
active residency, 76.32% idle, 388 mW.

A run that is supposedly saturating the GPU but shows a low active frequency
and high idle residency is **starved, not compute-bound** — the bottleneck is
upstream (tokenization, Python, host/device transfer, launch overhead), and no
amount of kernel tuning will fix it. This is the single most likely explanation
for two open anomalies in this program: why small batches lose to CPU, and why
throughput *dropped* at batch 128. Check residency before theorizing about
either.
- Anything that will run for minutes in production must be benchmarked for
  **at least 10 minutes sustained**. Apple Silicon throttles; a 30-second
  benchmark measures the burst, not the job.

## 3. Fixtures must be real

- Final numbers come from the S0 corpus fixtures, which are sampled from actual
  indexed silos and preserve real token-length distribution.
- Synthetic input is fine for microbenchmarks. Label it `synthetic: true` in
  results, and never use it for a go/no-go claim.
- **Worked example of getting this wrong:** the benchmark that seeded this
  program built chunks from `/usr/share/dict/words`. Random dictionary words
  tokenize into many rare subword pieces, so nearly every chunk hit the model's
  384-token ceiling. It measured a worst case and presented it as typical. S0's
  first job is to quantify how wrong that was.

## 4. Speed claims without quality claims are void

Any change that alters the numbers a model produces — different precision,
different model, quantized storage, truncated dimensions — must report retrieval
quality from the S0 eval set alongside throughput. A 3x speedup that quietly
costs 8% recall is a regression, and it will not be caught by anything else in
this system.

## 5. Stay out of production paths

- Work under `spikes/` at the repo root, or on a branch. Do not change defaults
  in `src/`.
- A spike may add a code path behind an environment flag that defaults to off,
  when that is the only way to measure the real system. Say so in the verdict.
- Do not modify the live `my_brain_db`. Build throwaway collections; use
  `LLMLIBRARIAN_DB` pointed at a temp path.

## 6. Results schema

Write one JSON file per configuration to `spikes/results/<spike-id>/<run-id>.json`:

```json
{
  "spike": "S3",
  "run_id": "2026-09-21T14:02:11Z-mlx-fp16",
  "hypothesis": "MLX beats torch/MPS by >25% on the same model",
  "verdict": "confirmed | refuted | inconclusive",
  "confidence": "high | medium | low",
  "machine": {"chip": "M4 Pro", "ram_gb": 48, "os": "15.x", "power": "ac",
              "torch": "2.10.0", "sentence_transformers": "5.3.0"},
  "config": {"backend": "mlx", "model": "all-mpnet-base-v2", "precision": "fp16",
             "batch": 64, "synthetic": false, "fixture": "s0-mixed-v1"},
  "throughput": {"unit": "chunks/s", "median": 0, "p5": 0, "p95": 0, "n": 5},
  "latency_ms": {"batch1_median": 0, "batch1_p99": 0},
  "quality": {"eval_set": "s0-eval-v1", "recall_at_12": 0.0, "ndcg_at_12": 0.0,
              "delta_vs_baseline": 0.0},
  "memory": {"rss_peak_mb": 0, "gpu_mb": 0, "released_after_unload": false},
  "energy": {"joules_per_1m_tokens": null},
  "cost": "what this would add to operate: processes, deps, failure modes",
  "notes": "",
  "would_change_my_mind": "the specific observation that would flip this verdict"
}
```

Fields you did not measure are `null`. Never estimate into a numeric field — put
estimates in `notes` and mark them as estimates.

## 7. What a finished verdict looks like

Write `S<n>-VERDICT.md` next to the prompt, with frontmatter (`type: decision`,
`updated:`), containing:

1. **One-sentence answer** to the spike's question. Lead with it.
2. **Verdict and confidence**, and the evidence for each claim. Anything
   inferred rather than measured is labeled inferred.
3. **What you built** — paths under `spikes/`, and whether it is throwaway or
   worth keeping.
4. **What surprised you.** This is often the most valuable section; do not skip
   it because it does not fit the hypothesis.
5. **What you could not answer** and what it would take.
6. **Recommendation** with its cost, or an explicit "do not build this".

## 8. Time-boxing and escalation

Each spike names a rough budget. If you blow past it, or you are blocked more
than two hours on environment setup (a backend that will not install, a missing
driver, permissions), stop and write up the blocker as an interim verdict. A
dependency that takes two hours to install is itself a finding worth recording.

If a spike's result makes a later spike pointless, say so explicitly in the
verdict. Cancelling downstream work is a valid and valuable outcome.
