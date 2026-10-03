# S0 measurement harness

This directory contains the reusable, privacy-preserving measurement layer for
the embedding/GPU spike program. The scripts read production data but never
write to the live Chroma collection.

```bash
# Build aggregate corpus statistics plus gitignored local fixture text.
uv run python -m spikes.harness.corpus

# Harvest the query audit into a gitignored eval set and a public summary.
uv run python -m spikes.harness.eval_set

# Recreate the intentionally synthetic dictionary workload used before S0.
uv run python -m spikes.harness.dictionary_fixture

# Benchmark the current production model on a real fixture.
uv run python -m spikes.harness.benchmark \
  --fixture spikes/private/s0/fixtures/s0-mixed-v1.jsonl \
  --label production-cpu-fp32 --device cpu --batch 32
```

Tracked manifests contain only aggregate counts and SHA-256 digests. Sampled
text, audit queries, cached vectors, and raw power traces live under
`spikes/private/`, which is ignored because it contains personal data.

The benchmark runner discards one model warmup, performs at least five measured
runs, reports median/p5/p95, captures thermal state and GPU residency, and uses
the S0 `powermetrics` wrapper for energy. For a production-like sustained check,
set `--minimum-run-seconds 600 --repeats 5`; this is intentionally expensive.
