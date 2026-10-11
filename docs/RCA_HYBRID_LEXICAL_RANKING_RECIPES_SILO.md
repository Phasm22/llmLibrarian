---
description: Open when hybrid retrieval ranking looks wrong, or before changing the lexical leg — RCA of the lexical leg feeding storage order into RRF (fixed 2026-10-10 with Option B), with evidence, root causes, the fix, before/after numbers, and open corpus/ingest findings.
type: rca
updated: 2026-10-10
globs:
  - "src/query/retrieval.py"
  - "src/query/retrieve_locked.py"
  - "mcp_server.py"
---

# RCA: hybrid retrieval ranks lexical hits by storage order

**Status: RC1–RC3 and the contributing factors are fixed (Option B, 2026-10-10).
The secondary corpus findings are still open.** See [Resolution](#resolution).

> Handoff for a Claude Code session. Read [CLAUDE.md](../CLAUDE.md) and
> [TECH.md](TECH.md) first. Everything below was observed through the MCP tools
> (`explain_retrieval`, `inspect_silo`, `read_document`) and by reading
> `src/query/retrieval.py` on the MacBook (TJ-MBP4, dev) on 2026-10-10. No code
> or index was changed during the investigation.

## Summary

Hybrid retrieval merges a vector result list with a "lexical" result list using
reciprocal rank fusion. The lexical list is not ranked. It is the first 200
chunks, in storage order, that contain any one of the query's terms. RRF treats
position in that list as relevance, so a chunk that happens to be stored early
gets the same score as the best vector hit.

Effect on results: unrelated chunks take roughly every other slot in top-k, and
a weak vector hit that also sits early in storage outranks the strongest vector
hit. On the Recipes silo, 5 of 8 test queries returned the same five unrelated
chunks as lexical ranks 1 to 5.

This is an engine defect, not a property of the Recipes corpus. Any silo is
exposed whenever a query contains a common word.

## Where it was found

| | |
|---|---|
| Silo | `recipes-55cf8f91` (`/Users/tjm4/Documents/Recipes`), 44 files, 5,401 chunks, last indexed 2026-10-04 |
| Host | TJ-MBP4 (dev). The Linux prod index has no Recipes silo. |
| Code read | `src/query/retrieval.py` (index mtime 2026-09-15) |
| Tooling | `explain_retrieval(query, silo="recipes-55cf8f91")` |

Prod (`/home/tj/Desktop/llmLibrarian`) was not inspected. Confirm it has the
same `retrieval.py` before assuming the fix applies there unchanged.

## Evidence

All queries were scoped to the Recipes silo. "Shared top 5" means lexical ranks
1 to 5 were these Nelson Family Recipe Book PDF chunks, in this order: p.1 table
of contents, p.2 dedication, p.5 Oatmeal Buns, p.6 Whole Wheat Bread, p.7 Buns.

| # | Query | Lexical result | What ranked #1 overall |
|---|---|---|---|
| 1 | `weeknight dinner with ground beef and spinach` | Shared top 5 | Nelson "Hot Dish" (vector rank 6, lexical 36), above vector #1 |
| 2 | `ingredients and quantities for chicken fricassee` | Shared top 5 | Nelson "Chicken & Rice" (vector 8, lexical 45) |
| 3 | `high protein smoothie with banana and peanut butter` | Shared top 5 | Correct smoothie PDF (vector 1), then Nelson contents page at #2 |
| 4 | `Chicken Fricassee` | 10 hits, none a fricassee | Nelson "Chicken & Rice" (vector **35**, lexical 12) |
| 5 | `lefse` | **0 hits** | Vector-only; a Boston plate caption |
| 6 | `What can I make with lentils, lemon, cumin, and one leafy green?` | Shared top 5 | Nelson "Fiesta Salad" (vector 22, lexical 25) |
| 7 | `Mint Julep Kentucky style` | #1 Nelson "Scalloped Corn" | A Roosevelt editorial from the Bullock intro |
| 8 | `how to fricassee chickens with cream and mushrooms` | Shared top 5 | Nelson "Chicken & Rice" (vector **37**, lexical 45) |

Observations that pin down the mechanism:

- **Lexical rank tracks page order.** In queries 1, 2 and 8 the Nelson chunks
  have lexical ranks that rise with page number (p.49 at 35, p.50 at 36, p.59 at
  45). In query 4 the lexical list runs Nelson PDF, then Boston PDF, then Boston
  text. That is storage order, not relevance.
- **The scores match unranked RRF exactly.** Lexical rank 1 scores
  `1/(60+1) = 0.016393`, identical to vector rank 1. Query 4's winner scores
  `1/(60+35) + 1/(60+12) = 0.024415`, which is the reported `rrf_score`.
- **Query 5.** "Lefse" appears in the Nelson contents page and in a section
  header ("Cookies, Bars, & Lefse"), both capitalized. The lowercase term
  matched nothing.
- **Query 7.** "Scalloped Corn" contains "cream-style corn". The term `style`
  matched it; nothing about juleps did.

## Root causes

### RC1. The lexical list has no relevance order

`run_hybrid_retrieve` builds the lexical list with a filtered `get`, not a
search:

```python
where_doc = {"$or": [{"$contains": t} for t in terms[:MAX_LEXICAL_FOR_RRF]]}
lex = collection.get(where_document=where_doc, include=["documents", "metadatas"])
ids_l = (lex.get("ids") or [])[:MAX_LEXICAL_FOR_RRF]
```

`collection.get` returns matches in the collection's internal order. `rrf_merge`
then computes `pos_l = {lid: i + 1 ...}` and scores each as `1 / (k + pos_l)`.
Position carries no information about how well the chunk matches.

The `[:MAX_LEXICAL_FOR_RRF]` slice (200) makes it worse. When a term is common,
thousands of chunks match and only the first 200 stored survive. Whichever file
was indexed first supplies the whole lexical list for every such query. In this
silo that file is the Nelson PDF.

The docstring on `extract_direct_lexical_terms` describes this as a "BM25-like
fallback". There is no term weighting anywhere in the path.

### RC2. Common words pass the term filter

`DIRECT_LEXICAL_STOPWORDS` has 24 entries. It omits `and`, `with`, `can`,
`make`, `one`, `that`, `this`, `have`, and `use`, among others. Terms are any
`\b[\w-]{3,}\b` token not in that set, up to 8.

Query 1 therefore produces the terms `weeknight, dinner, with, ground, beef,
and, spinach`. The `$or` matches every chunk containing "and" or "with", which
is nearly the whole silo. Combined with RC1, the result is "the first 200
chunks in the collection".

### RC3. Word terms are lowercased, but `$contains` is case-sensitive

`extract_direct_lexical_terms` runs `re.findall(..., q.lower())`. The module's
own comment on `PROFILE_LEXICAL_PHRASES` notes that Chroma's `$contains` is
case-sensitive. So a lowercased term can only match lowercase text.

Consequences:

- Recipe titles and headings never match lexically. "Chicken Fricassee" as a
  heading, "MINT JULEP" in Bullock, and "Lefse" in Nelson are all invisible to
  the lexical leg. Body text such as "4 pieces chicken" does match.
- The leg is biased toward ingredient lines and away from the title chunk, which
  is the opposite of what a known-item query needs.
- Quoted phrases keep their case (they are extracted before `.lower()`), so
  `"Mint Julep"` in quotes behaves differently from the unquoted words.

### RC4. Dual-listed chunks get an unearned boost

`rrf_merge` sums the two reciprocal ranks when a chunk is in both lists. Since
lexical membership under RC1 to RC3 is close to arbitrary, the sum promotes
chunks for being stored early and containing a common word. Query 4 is the clean
example: vector rank 35 became overall rank 1.

RC4 is correct RRF behavior. It becomes a defect only because the lexical input
is unranked. Fixing RC1 removes it; do not change the fusion formula to
compensate.

### Contributing factors

- **Silent failure.** The lexical call is wrapped in `except Exception: pass`.
  If the `get` raises, retrieval degrades to vector-only with no log line.
- **Misleading diagnostics.** `explain_retrieval` returned
  `retrieval_method: "hybrid_or_vector"` and a `signal_summary` reading
  "Vector-only retrieval: no exact-text terms were extracted" on runs that
  reported 5 to 10 lexical hits. The code that builds that summary was not read;
  locate it and make it reflect the path actually taken.
- **Exact-hash dedup only.** `dedup_by_chunk_hash` drops identical chunks. A PDF
  and its extracted text produce near-identical chunks with different hashes, so
  both survive (see Secondary findings).

### Possibly related, not verified

The docstring on `source_diversity_cap` describes an earlier case where a
one-file cookbook "can drop the vector-#1 recipe (Classic Carrot Cake at hybrid
rank 6)" and addresses it by raising the per-source cap for silo-scoped queries.
A vector-#1 chunk landing at hybrid rank 6 is what RC1 produces. Whether that
earlier case had the same cause was not checked. Worth re-running that case
after the fix to see if the cap workaround is still needed.

## Fix options

The requirement: the lexical list handed to `rrf_merge` must be ordered by a
real relevance score, computed over all matching chunks, with case-insensitive
matching and common words carrying little or no weight.

### Option A: FTS5 sidecar (durable)

Maintain a SQLite FTS5 table of chunk id, silo, and text beside Chroma. Query it
with `bm25()` ordering, pass the ranked ids to `rrf_merge`.

- Gives real BM25, case folding, tokenization, phrase queries, and prefix
  matching in one step.
- Costs an ingest-path change, a backfill for existing silos, and keeping the
  sidecar in sync on `remove_file`, `update_file`, `repair_silo`, and reindex.
- Note the default exclude patterns already skip `*.db` and `*.sqlite`, so a
  sidecar inside `my_brain_db/` will not be self-indexed.

### Option B: score candidates in process (interim, no schema change)

Keep the `$contains` fetch as a candidate generator and rank locally.

1. Emit case variants per term (`chicken`, `Chicken`, `CHICKEN`) in the `$or`.
2. Fetch candidates per term rather than one combined `$or`, so per-term match
   counts are known. Treat a term matching more than some fraction of the silo
   as non-discriminative and drop it.
3. Score each candidate with BM25 or TF-IDF using those counts, then sort.
4. Apply the 200 cap after sorting, never before.
5. If no discriminative terms remain, skip the lexical leg and return
   vector-only, labelled as such.

Cheaper to ship, but it pulls full documents for every candidate on each query
and will be slow on large silos with mid-frequency terms.

### Either way

- Expand the stopword set, or replace it with the document-frequency cutoff in
  B.2, which adapts per silo.
- Replace `except Exception: pass` with a logged warning that names the
  exception, then fall back.
- Make `retrieval_method` and `signal_summary` report `hybrid` or `vector_only`
  truthfully, with the extracted terms and how many were dropped.

Check before choosing: whether the installed Chroma version offers a
case-insensitive or regex document filter. If it does, B.1 simplifies. This was
not checked.

**Decision for TJ:** A or B first. B is a contained patch to `retrieval.py`. A
touches ingest and the on-disk layout, so it needs sign-off before work starts.

## Acceptance tests

Unit tests, using a fake collection whose `get` returns rows in a fixed storage
order:

1. A chunk stored first that matches only a common word does not outrank the
   vector #1 chunk.
2. A lowercase query term matches a Title Case and an ALL CAPS occurrence.
3. With more than 200 matching chunks, the best-scoring match is retained even
   when it is stored last.
4. A query made only of common words returns `vector_only` and says so.
5. An exception from the lexical call is logged and falls back to vector-only.

Regression queries against the Recipes silo (re-run the Evidence table):

| Query | Pass condition |
|---|---|
| Any five unrelated queries | Their lexical top 5 are not the same five chunks |
| `Chicken Fricassee` | Nelson "Chicken & Rice" is not rank 1 |
| `lefse` | `lexical_hit_count > 0`, a Nelson "Lefse" chunk is in the top 3 |
| `Mint Julep Kentucky style` | No Nelson chunk in the top 10; a Bullock julep recipe chunk in the top 3 |
| `high protein smoothie with banana and peanut butter` | Nelson contents page is not in the top 10 |

Then run the existing eval harness (`spikes/harness/eval_set.py` builds
known-item cases) on at least one non-recipe silo before and after, to confirm
the change does not regress silos where the current behavior happened to work.

## Secondary findings: Recipes corpus and ingest

These are separate from the lexical defect. They will still degrade results
after it is fixed.

**Format duplication, about 31% of the index.** The same source is indexed as
PDF and as extracted text. Roughly 1,650 of 5,401 chunks are redundant.
`has_duplicate_content` is false for every file.

| Source | Indexed copies (chunks) |
|---|---|
| Boston Cooking-School 1896 | PDF 768, text 1,314 |
| Book of American Baking | PDF 123, text 395 (`02-baking/`), text 400 (`05-extracted-text/`) |
| The Ideal Bartender | PDF 52, text 87 (`03-drinks/`), text 88 (`05-extracted-text/`) |
| USDA Harvest of Recipes | PDF 97, text 96 |
| Nelson Family Recipe Book | PDF 120, text 198 |
| UMaine Make Your Own | PDF 14, text 19 |

In query 7, three of the top ten slots were the same editorial passage from the
three Bullock copies, and two were the same Brandy Float recipe. Short-term:
exclude either `05-extracted-text/` or the source PDFs. Longer-term: near-
duplicate detection across files in a silo (shingle or MinHash), since exact
chunk hashes cannot catch PDF-versus-text pairs.

**Recipes are split across chunks.** Chunk boundaries follow page or window
size. Chunks open mid-recipe: one Boston chunk begins "Add to sauce, chicken,
yolks and whites of eggs..." and then starts "Chicken Souffle"; the p.249 chunk
begins "or make a sauce by browning butter and flour". The title and ingredient
list are in a different chunk from the method. For a recipe corpus the natural
unit is one recipe. The silo's own `RELATIONAL-RETRIEVAL.md` already calls for
chunking by recipe.

**Quantities are OCR-corrupted in both Boston copies.** Examples from retrieved
text: `\}4 cdps cold cooked chicken`, `3^ cup butter`, `J^ cup lemon juice`,
`1J< cups brown stock`, `y^ teaspoon celery salt`. Fractions are unrecoverable.
`CATALOG.csv` lists Project Gutenberg as the source; the transcribed Gutenberg
text would have intact fractions, whereas the indexed text is scan OCR. Any
grocery-list arithmetic over this text will be wrong.

**Hannah Glasse has long-s read as f.** `falmon`, `mufhrooms`, `fait` (salt),
`drefs`. Exact-term matching cannot hit these. A normalization pass at ingest,
keeping the raw text alongside, would fix it.

**Junk chunks.** A three-character chunk (`e I u`), plate captions ("Planked
Eggs. — Paye 100."), back-of-book index pages, and tables of contents are
indexed and surface for short queries. A minimum-content filter at ingest would
remove most of them.

**Catalog drift.** `CATALOG.csv` has 8 rows for 44 files. None of the 22
smoothie PDFs are listed. It lists
`01-general-cookbooks/Hannah-Glasse-The-Art-of-Cookery-1747.pdf`, which is not
in the index (only the text copy is). `Kids-Cook-Fantastic-Fruit-Smoothie.pdf`
contains a lentils section, so filename and folder do not describe its content.

**Possible failed parse.** `Fruit-and-Veggie-Smoothie-Recipes.pdf` produced one
chunk. Check whether it is an image-only PDF.

## Out of scope for this handoff

- The structured recipe layer (one record per recipe with normalized
  ingredients, quantities, and units) described in the silo's `README.md`. It is
  what ingredient-set queries and grocery-list generation need, and it is a
  separate piece of work. Query 6 fails for that reason, not because of ranking.
- The reranker. The S6 spike doc mentions an optional cross-encoder rerank of
  the top 40. Whether it was active for these runs is unknown. A reranker would
  mask RC1 for the top 40 without fixing the candidate set, so test with it off.

## What was not verified

- That `collection.get` ordering is insertion order. It is consistent with every
  observation above but was not confirmed against Chroma's documentation.
- The code path that builds `signal_summary` and `retrieval_method` in
  `explain_retrieval`.
- That the prod repo on Linux matches this `retrieval.py`.
- That the Boston fricassee recipe's title is in the p.248 chunk. The p.249
  chunk clearly continues a recipe; where it starts was inferred.
- Whether the earlier "Classic Carrot Cake" case has the same cause.

## Suggested order of work

1. Write the failing unit tests (acceptance tests 1 to 5).
2. Get the Option A versus B decision from TJ.
3. Implement, fix the diagnostics, replace the silent `except`.
4. Re-run the Evidence table and the eval harness; record before and after.
5. Separately: dedupe the Recipes silo inputs and swap the Boston text source,
   then reindex. Do this after step 4 so the two effects are measured apart.

## Resolution

Fixed on 2026-10-10 on the MacBook (dev) with **Option B**, the contained
in-process patch. Option A (FTS5 sidecar) needs sign-off and was not started.

### What changed

- **RC1.** `rank_lexical_candidates` in `src/query/retrieval.py` replaces the
  single `$or` get. Per term, it fetches matching ids only to get document
  frequency. It pre-orders the union by IDF-weighted term coverage, fetches up
  to `MAX_LEXICAL_SCORING_DOCS` (2000) documents, scores them with BM25
  (k1 1.2, b 0.75), and sorts. The 200 cap applies after sorting.
- **RC2.** `DIRECT_LEXICAL_STOPWORDS` grew from 24 to about 100 function words.
  A per-scope document-frequency cutoff (`LEXICAL_MAX_DF_RATIO` = 0.2, applied
  when the scope has at least 50 chunks) drops whatever else is common in that
  silo, such as `butter`, `cream`, and `lemon` in Recipes.
- **RC3.** Matching uses Chroma's `$regex` with `(?i)` and a leading `\b`
  (`lexical_term_pattern`). Chroma 1.5.5 supports it, and it was faster than
  `$contains` in testing (about 20ms per term on the Recipes silo). Terms,
  including quoted phrases, are lowercased and deduplicated.
- **RC4.** Unchanged, as recommended. The fusion formula is untouched.
- **Silent failure.** The `except Exception: pass` now logs a warning with the
  exception and records `status: error`.
- **Diagnostics.** `retrieve_locked.py` hardcoded `retrieval_method =
  "hybrid_or_vector"`. That constant is why `explain_retrieval` described
  hybrid runs as vector-only. It now reports the method actually taken.
  `explain_retrieval` returns a `lexical_terms` block (terms used, terms
  dropped and why, status), and its `signal_summary` is built from that block.
  `query_personal_knowledge` strips the block to keep its payload slim.

Tests: `tests/unit/test_hybrid_lexical_ranking.py` covers acceptance tests 1–5,
plus stopwords and where-filter scoping. Unit suite: 1370 passed. Integration
suite: 37 passed. Its 2 failures are in `test_image_fixture_ingest.py`, about
deferred-summary wording, and they also fail without this change.

### Recipes regression queries (after)

| Query | Pass condition | Result |
|---|---|---|
| All eight evidence queries | Lexical top 5 not the same five chunks | Pass. No shared Nelson run; each query's lexical list differs |
| `Chicken Fricassee` | Nelson "Chicken & Rice" not rank 1 | Pass. #1 is the Boston fricassee chunk (vector 1, lexical 2) |
| `lefse` | `lexical_hit_count > 0`, a Nelson Lefse chunk in the top 3 | Pass. 13 lexical hits; #1 is the Nelson "Cookies, Bars, & Lefse" header. The p.95 recipe is #4, behind a junk vector-#1 plate caption |
| `Mint Julep Kentucky style` | No Nelson chunk in the top 10; a Bullock julep chunk in the top 3 | Pass. All ten are Bullock; #1 contains "MINT JULEP--Kentucky Style" and its method |
| `high protein smoothie with banana and peanut butter` | Nelson contents page not in the top 10 | Pass. All ten are smoothie sources |

Lexical-leg latency on the live server: 86–240ms per query, scoped or
unscoped. The old single get took 91–301ms, because it pulled up to 5,000 full
documents when a term like `and` matched.

### Eval harness

`spikes/harness/score_eval.py` scores the vector leg only, so an A/B script ran
the S0 eval set (121 cases) through the same vector top 60 and RRF with three
lexical inputs.

| Cases | Metric | vector only | old lexical | BM25 lexical |
|---|---|---|---|---|
| known-item (50) | recall@12 | 0.260 | 0.400 | **0.740** |
| known-item (50) | nDCG@12 | 0.198 | 0.194 | **0.439** |
| historical-source (63) | recall@12 | 0.627 | 0.663 | 0.656 |
| historical-source (63) | nDCG@12 | 0.487 | 0.588 | 0.524 |

Known-item labels come from chunk text, independent of any retrieval run, so
they are the fair measure. Historical-source labels were harvested from past
production retrieval, which this bug shaped:

- 15 of the 17 Recipes historical-source labels point at the Nelson PDF, the
  storage-first file.
- In unscoped cases, 69% of label sources sit in the first 10% of storage
  (median first position 77 of 16,144 chunks). Scoped labels: 14%.

Of the 21 historical-source cases that scored worse, 11 are Recipes cases whose
label is that Nelson PDF. The rest are 4 small deltas (0.03–0.06) on a
33-chunk silo and 6 unscoped cases whose labels skew toward storage-early
sources. Rebuild the historical-source labels from post-fix retrieval before
using them as a regression gate.

### Still open

- **Prod.** The Linux repo was not inspected. The fix is in this checkout. Prod
  needs this commit plus Chroma 1.5.5+ (pinned in `pyproject.toml`) for `$regex`.
- **Resident MCP.** The macOS MCP serves this working tree but loaded the old
  module at startup. Restart it to pick up the fix.
- **Carrot-cake cap.** The `source_diversity_cap` silo-scoped widening was not
  re-tested. Re-run that case and drop the workaround if the fix covers it.
- **Secondary findings.** Format duplication is fixed (see below). Chunking
  across recipes, OCR fractions, long-s, junk chunks, and catalog drift are
  unchanged. Junk chunks
  now cost the most: in `lefse`, a vector-#1 plate caption sits above the real
  recipe.
- **Option A.** Still the durable path if per-query BM25 over up to 2000
  fetched documents becomes slow on large unscoped indexes.

## Recipes dedupe (2026-10-10)

One copy per book is indexed now. No source files were deleted: the other
copies are kept out by the silo's `exclude_patterns`, which `llmli add
--exclude` persisted to the registry and which the watcher reads at startup.
The silo went from 5,401 to 3,114 chunks and from 44 to 35 files.

| Book | Indexed copy | Why |
|---|---|---|
| Nelson Family Recipe Book | root PDF | One page per recipe, so one chunk per recipe, with page numbers |
| Boston Cooking-School | PDF | Both copies are the same scan OCR; the PDF chunks by page |
| USDA Harvest of Recipes | PDF | Clean extraction, page-aligned |
| Book of American Baking | `02-baking/*.txt` | The original Gutenberg text; the PDF is a local rendering of it |
| The Ideal Bartender | `03-drinks/*.txt` | Same reason |
| UMaine Make Your Own | `05-extracted-text/*.txt` | The PDF extraction splits words ("Cauliflo wer") |
| UT-Houston mocktails | PDF | Same content, one chunk |
| Hannah Glasse | `05-extracted-text/*.txt` | The PDF (51 MB) is over the 20 MB ingest limit; also excluded explicitly |

To restore a copy, remove its pattern from `exclude_patterns` in the registry
entry and re-run `llmli add`. `Vitamix-100th-Anniversary-Cookbook.pdf` (27 MB)
is also over the size limit and has never been indexed.

After the dedupe, the Mint Julep top 10 has no repeated passages. Before, three
slots held the same editorial from three Bullock copies. The other evidence
queries kept their pass results. `lefse` now has the p.95 recipe at #3.

The corpus-meta files (`README.md`, `RELATIONAL-RETRIEVAL.md`, `CATALOG.csv`,
`03-drinks/smoothies/WISHLIST.md`) are excluded the same way. The README held
the silo's test prompts verbatim, so it matched them lexically. Final state:
3,097 chunks in 31 files.
