# Implementation Status and Fit-Gap Assessment Report
Date: 2026-02-11
Source roadmap: `docs/ROADMAP.md`

## Executive Summary
- Total assessed items: 15 (all roadmap sub-items across sections 1–4)
- `Implemented`: 10
- `Partially Implemented`: 4
- `Not Implemented`: 0
- `Superseded/Changed`: 1

Top 3 risks:
- Retrieval quality still lacks BM25 fallback for weak semantic matches and semantic boundary chunking.
- Citation trust is still footer-based; sentence-level grounding/validation is not implemented.
- Generic field extraction remains domain-specific (tax-form shaped).

Top 3 quick wins:
- Add BM25 lexical fallback with weak-distance trigger for general retrieval paths.
- Add semantic chunking boundary detection for code/JSON/Markdown.
- Add citation validator/post-check to catch unsupported inline claims.

## Method
Status was assigned by inspecting code paths, current CLI behavior, and test coverage:
- Retrieval/guardrails: `src/query/*`
- Ingest/chunking/capabilities: `src/ingest.py`
- CLI/workflows: `pal.py`, `cli.py`
- Domain packs/config: `archetypes.yaml`, `src/load_config.py`, `src/query/core.py`
- Tests: `tests/unit/*`, `tests/integration/*`

Rubrics used:
- `Implemented`: behavior exists and has test coverage.
- `Partially Implemented`: core behavior exists, but roadmap intent is not fully met.
- `Not Implemented`: no meaningful implementation found.
- `Superseded/Changed`: roadmap intent replaced by a different accepted design.

## Item-by-Item Matrix
| Roadmap ID | Feature | Implementation Status | Evidence | Fit | Gap | Impact | Effort to close | Recommended next action |
|---|---|---|---|---|---|---|---|---|
| `1a` | BM25 lexical fallback on weak vector similarity | Partially Implemented | `src/query/retrieval.py:56` (`rrf_merge`), `src/query/core.py:349` (lexical + RRF path for `EVIDENCE_PROFILE`) | Moderate | No BM25 implementation and no weak-distance trigger branch for general retrieval. | High | M | Add `_bm25_search()` + weak-distance fallback gate for all relevant intents. |
| `1b` | Per-intent diversity caps | Implemented | `src/query/retrieval.py:30` (`DIVERSITY_CAPS`, `max_chunks_for_intent`), `src/query/core.py:448` (intent-based cap), `tests/unit/test_retrieval_pipeline.py:51` | Strong | None observed for roadmap scope. | Medium | - | Monitor answer quality by intent and tune cap values as needed. |
| `1c` | Query expansion for domain synonyms | Implemented | `src/query/expansion.py:1`, `src/query/core.py:205`, `tests/unit/test_query_expansion.py:1`, `tests/unit/test_run_ask_orchestration.py:63` | Strong | Current synonym dictionary is intentionally small and static. | Medium | S | Expand synonym catalog incrementally from observed misses. |
| `1d` | Semantic chunking with boundaries | Partially Implemented | `src/ingest.py:268` (`chunk_text` is line-aware) | Moderate | No boundary-pattern-aware split policy (`def/class/headings/JSON`). | High | M | Add boundary-aware chunker and regression tests for code/JSON/Markdown boundaries. |
| `2a` | Source-grounded inline citations (`[N]`) | Partially Implemented | `src/query/core.py:534` (sources section output), `src/query/formatting.py` (source formatting/linkification) | Weak | No sentence-level citation enforcement/validation; no citation post-check pipeline. | High | M | Add citation prompt contract + `validate_citations()` and fail-safe rewrite/warn path. |
| `2b` | Confidence signaling banner | Implemented | `src/query/core.py:87` (`_confidence_signal`), `src/query/core.py:552` (warning injection), `tests/unit/test_run_ask_orchestration.py:134` | Strong | Confidence policy is heuristic and may need tuning by corpus type. | Medium | S | Add per-intent thresholds if false positives show up in real usage. |
| `2c` | Generic structured field extraction | Partially Implemented | `src/query/guardrails.py:16` (`parse_field_lookup_request`), `src/query/guardrails.py:83` (`_extract_exact_line_value`) | Moderate | Extraction exists but is tax/form-line oriented, not generic labeled-field extraction. | Medium | M | Add `extract_field_value(chunks, field_pattern)` and generic lookup route. |
| `3a` | `pal ask --show-sources` opt-in verbosity | Superseded/Changed | `src/query/core.py:534` always appends sources; `pal.py:951` ask options omit `--show-sources` | Moderate | Roadmap wanted default answer-only + opt-in sources; current product returns sources by default. | Low | S | Keep current design or add `--hide-sources` if output noise becomes a user pain. |
| `3b` | `pal diff <silo>` | Implemented | `pal.py:1009` (`diff` command), `pal.py:1015` (manifest-vs-fs compare) | Strong | Diff currently uses default include/exclude rules rather than per-silo overrides. | Medium | S | Add optional per-silo include/exclude overrides if needed. |
| `3c` | `pal status` one-line health summary | Implemented | `pal.py:1092` (`status` command) | Strong | None observed for roadmap scope. | Medium | - | Keep output compact and stable for scripting. |
| `3d` | `pal ask --quiet` scripting mode | Implemented | `pal.py:947`, `cli.py:106`, `cli.py:325`, `src/query/core.py:121`, `src/query/core.py:552`, `tests/unit/test_run_ask_orchestration.py:112` | Strong | Guardrail responses are reduced to primary message in quiet mode; this is intentional. | Medium | S | Add docs examples for shell pipelines. |
| `4a` | Legal domain pack | Implemented | `archetypes.yaml:92` | Strong | Pack is config-only; no domain-specific parser/guardrail extensions yet. | Low-Med | S | Add domain-specific extraction helpers only if users request them. |
| `4b` | Medical/health domain pack | Implemented | `archetypes.yaml:107` | Strong | Safety policy is prompt-level only (no hard validator layer yet). | Low-Med | S | Add optional hard safety post-checks for high-stakes workflows. |
| `4c` | Code review domain pack | Implemented | `archetypes.yaml:121` (`codebase`) | Strong | Coexists with existing `palindrome` pack; overlap may need cleanup later. | Low-Med | S | Decide whether to keep both packs or deprecate one alias. |
| `4d` | Per-silo prompt override via `pal pull --prompt` | Implemented | `pal.py` (`pull --prompt`, `--clear-prompt` and validation), `src/state.py` (`prompt_override` helpers + preserved registry metadata), `src/query/core.py` (override/archetype/default precedence with hashed-slug and display-name fallback), `tests/unit/test_pal_pull.py`, `tests/unit/test_state_registry.py`, `tests/unit/test_run_ask_orchestration.py` | Strong | None observed for roadmap scope. | Medium | - | Monitor prompt override usage and add `pal inspect` surfacing if users need easier visibility. |

## Priority-Order Fit-Gap (Roadmap vs. Current State)
Roadmap priority list currently front-loads items that are still open, but ordering is inconsistent with implementation reality and stated impact:

- Misorder 1: per-intent caps are ranked first while BM25 fallback remains only partial.
- Misorder 2: optional domain-pack work is now complete while higher-impact retrieval trust items remain open.
- Misorder 3: trust-critical inline citation work remains partial despite many workflow items being closed.

Suggested interpretation (without rewriting roadmap):
- Treat BM25 fallback plus citation/confidence improvements as trust/retrieval critical path.
- Treat semantic chunking + generic structured extraction as the next implementation wave.
- Treat domain-pack expansion as an optional track after core trust/retrieval closure.

## Appendix: Verification Snapshot
Commands run during this assessment and observed outcomes:

```bash
# Roadmap and implementation inspection
sed -n '1,260p' docs/ROADMAP.md
sed -n '1,260p' src/query/retrieval.py
sed -n '1,260p' src/query/core.py
sed -n '1,280p' src/ingest.py
sed -n '1,260p' src/query/guardrails.py
sed -n '860,1060p' pal.py
sed -n '1,220p' archetypes.yaml
rg -n "bm25|rrf|divers|expand_query|citation|confidence|show-sources|--quiet|diff|status|prompt" src pal.py cli.py archetypes.yaml tests -S

# Test baseline snapshot
uv run pytest -q
# Result: 167 passed in 1.90s
```

Coverage quality notes:
- Strong coverage exists for current retrieval pipeline primitives (`tests/unit/test_retrieval_pipeline.py`) and pal workflow guardrails (`tests/unit/test_pal_*`).
- New coverage now asserts query expansion, intent cap behavior, quiet output, and confidence signaling.
