---
description: Read before changing what llmLibrarian's MCP tools return to small local models (Open-WebUI on :8766), before touching intent routing over MCP, private-silo visibility on HTTP, image vision defaults, or stdio/HTTP process lifecycle. Verified findings from the 2026-10-02 Open-WebUI session, root causes with file:line, the ranked fix list and what shipped for each, and the policy switches TJ still has to flip.
type: plan
updated: 2026-10-03
globs:
  - mcp_server.py
  - src/query/intent.py
  - src/query/core.py
  - src/query/retrieve_locked.py
  - src/query/core_support.py
  - src/tax/ledger.py
  - src/ingest/__init__.py
  - macos/Sources/llmLibrarian/Model/Store.swift
---

# MCP contract for small local models — findings and plan

**Status (2026-10-03):** every item in the fix list has shipped on branch
`claude/infallible-goodall-5949ce`. The two privacy fixes (ranks 0–1) are also
on local `dev` (fast-forwarded to 5d56fcc; not pushed). The running :8766
process (pid 95443, started 2026-09-30) still serves its in-memory code — **it
leaks the tax ledger until it is restarted**. See [What TJ still has to
do](#what-tj-still-has-to-do).

Analysed code: `dev` @ 13a2855. The findings below are as investigated on
2026-10-02; [Status of the fix list](#status-of-the-fix-list) records what
landed and where it deviated from the plan.

Related: [CLAUDE.md](../../CLAUDE.md) (privacy, registry, Chroma safety),
[.claude/rules/silo-privacy.md](../../.claude/rules/silo-privacy.md),
[HANDOFF_hnsw_desync_2026-05-21.md](../../HANDOFF_hnsw_desync_2026-05-21.md),
[docs/MCP_DISCOVERY_VERBOSITY_ISSUE.md](../MCP_DISCOVERY_VERBOSITY_ISSUE.md).

## The session, reconstructed

Source: `~/.pal/logs/query-audit.jsonl`, 2026-10-02 local (MDT, UTC−6). 49
records; the last two (05:35:46Z, 05:36:32Z) were the previous investigation's
verification calls, leaving **47 client calls, 27 empty** — as reported.

| Chat | Calls | What happened |
|---|---|---|
| journalLinker / food (04:31–04:49Z) | 18 | All non-empty. 6 identical calls at 04:47:23–24Z (one `n=10`, five `n=5`), 4 identical at 04:46:18–19Z. Identical non-empty results — the model emitted parallel duplicate tool calls. |
| Dad's car (05:14Z) | **1** | `n=40` → 20 chunks, top score 0.054, confidence low. Then `ask_image` on DSCN2769 (outside the audit log). |
| README image indexing (05:26–05:35Z) | 28 | **27 empty, every one containing "capabilities"**. 10 unscoped calls at 05:32:49Z (two queries × 5) and 5 scoped at 05:33:49Z. One query without the word (05:34:25Z) returned 14 chunks — all scored 0.0. |

So: **100% of the empty results were finding 1.** The "10× in a second" burst
belongs to the README chat, not the car chat.

## Stop-the-line findings (new, not in the brief)

### S1. a30aebf was dropped from `dev` — privacy regression on disk — CONFIRMED

`a30aebf` ("Close the privacy gaps main's filter did not cover", 2026-09-30) and
its parent merge `cafefe9` are on **no branch, local or remote** — only in the
reflog. Tonight's `dev` rebase (reflog 23:41:02: `rebase (start): checkout
origin/dev` → picked 13a2855 only) replaced local `dev` with `origin/dev`, which
had its own merge of main (9beb4e2) but never had a30aebf.

```bash
git merge-base --is-ancestor a30aebf dev || echo "a30aebf NOT in dev"
git branch -a --contains a30aebf     # (empty)
```

Of a30aebf's four fixes, three are gone from `dev`:

| Guard | On `dev` | Where |
|---|---|---|
| Display name ("Tax") must not open a private silo | **missing** | `src/query/core.py:112-114` resolves the display name and proceeds |
| `silo_roster` (lite) omits private slugs | **missing** | `mcp_server.py:2125-2135` lists every slug; `retrieve_knowledge` (`mcp_server.py:2213-2226`) accepts any listed slug |
| `$nin` also covers `<slug>-artifacts` | **missing** | `src/state.py:313` |
| `ask_image` resolves private images only when named | present | `mcp_server.py:2280` |

The running :8766 process (pid 95443) started 2026-09-30 01:01:59, eleven
seconds after a30aebf landed, so **it still enforces these in memory**. Any
restart — KeepAlive after a crash, reboot, `pal mcp restart` — serves the
regressed code. Its integration test `tests/integration/test_silo_privacy_mcp_tools.py`
was also lost; it is restored on this branch and fails 3/7.

### S2. Unscoped `TAX_QUERY` attaches private tax-ledger rows — CONFIRMED (live now)

`execute_retrieve_chroma_phase` applies the private `$nin` to chunk queries,
then, for `TAX_QUERY`, reads `tax_ledger.json` with `silo=None` and no privacy
filter (`src/query/retrieve_locked.py:282-309` → `src/tax/ledger.py:35-46`).

`route_intent` sends ordinary questions there: any year plus
`sell|sold|stock|federal|proceeds|tax…` (`src/query/intent.py:232-237`), e.g.
*"how much did my dad sell his old car for in 2024"* → `TAX_QUERY`.

Live ledger, counted by `(silo, tax_year)` only — no values read: 274 rows from
`tax-0c9821db`, 39 from `chat-archive`, 1 from `lab-history-d8dd1667`, all
private. The example query would attach the 27 `tax-0c9821db` rows for 2024
(form, field label, raw and normalized value) to an Open-WebUI context whose
tool description says "Do not query Tax". a30aebf never covered this path, so
:8766 leaks today. I did not trigger it live; `test_unscoped_tax_query_does_not_attach_private_ledger_rows`
proves it with fixtures.

Side note: the ledger also holds rows for `llmlibrarian-322853c4`, a silo no
longer in the roster, and for `chat-archive`, which is not a tax corpus — the
extractor is running on non-tax silos. (Fixed later: the non-private rows were
"extracted" from the repo's own tax test files; see N6.)

Correction: an earlier draft said 296 tax rows; the per-year counts sum to 274,
all from PDFs. Commit 5d56fcc's message, already on `dev`, repeats the 296.

## Findings from the brief

### 1. CAPABILITIES (and five other intents) hijack content questions — CONFIRMED, broader than reported

**Repro (live, :8766):**

```text
query_personal_knowledge("README image indexing capabilities", silo="llmlibrarian-46ad0cbe")
→ {"intent":"CAPABILITIES","note":"Intent 'CAPABILITIES' is deterministic and does not use
   vector retrieval. Try rephrasing as a descriptive question for semantic retrieval.",
   "chunks":[],"answer_confidence":"low","coverage_note":"no chunks returned"}
query_personal_knowledge("what did the docs from 2024 say about sourdough", silo="recipes-55cf8f91")
→ same shape, intent FILE_LIST
```

(`explain_retrieval` from the brief returns the intent but **drops the `note`**,
`mcp_server.py:1330-1341`.)

**Root cause** is two layers:

1. `run_retrieve` short-circuits six intents to `chunks: []` before touching
   Chroma (`src/query/core.py:27-34`, `100-110`). Over MCP there is **no**
   deterministic handler — those live only in `run_ask`
   (`src/query/ask/orchestrator.py:249-660`). So over MCP a deterministic route
   can never produce an answer; it can only produce nothing.
2. The routing regexes match single common words anywhere in the query.

**Audit of every deterministic intent** (30 ordinary content questions run
through `route_intent`; 13 returned empty over MCP):

| Intent | Trigger (`src/query/intent.py`) | Content questions it swallows | MCP caller gets |
|---|---|---|---|
| `CAPABILITIES` | bare `\bcapabilities\b`, `what formats?`, `what (file )?types?` (L82-87) | "README image indexing capabilities", "camera's low-light capabilities", "what formats does the recipe book use", "what file types does journalLinker's ingest accept" | `[]` + "try rephrasing" — doesn't name the word, doesn't return `capabilities()` text |
| `FILE_LIST` | file noun + year + `what/which/show/find/from` (L93-99) | "what did the docs from 2023 say about the warranty", "show me what my files from 2022 say about sourdough" | `[]` + note |
| `TIMELINE` | `history|evolution|sequence|timeline…` + `changes|events|year` (L123-130) | "history of changes to the intent ledger", "evolution of my thinking on food tracking in 2025" | `[]` + note. Also `chronolog\b` never matches "chronological" |
| `STRUCTURE` | `inventory|directory|layout|snapshot|recent changes…` + a weak noun (L132-150) | "what's in my pantry inventory", "directory of contacts in the documents", "recent changes to the intent ledger docs", "how many .jpg files are in the car folder" | `[]` + note |
| `CODE_LANGUAGE` | one year + `language` (L156-171) | "what language did I study in 2019", "body language notes from 2023" | `[]` + note |
| `METADATA_ONLY` | `document types?|file counts?` (L117-121) | "what document types does the DMV need" | `[]` + note |
| `FILENAME_DATE_LOOKUP` | date phrase + file noun (L50-67, L90-91) | "notes from last week" | **falls through to retrieval** (not in `_DETERMINISTIC_INTENTS`) — fine |
| `TAX_QUERY` | year + tax/stock/sell words (L225-242) | "how much did my dad sell his old car for in 2024", "federal grants 2025 notes" | retrieval runs, **plus private ledger rows** (S2) |
| `PROJECT_COUNT` | `how many` + `projects` (L244-245) | "how many projects use chroma" | retrieval runs — fine |

**Principle evaluated** — "when `silo=` is set, or a deterministic intent yields
nothing, fall through to retrieval": correct, and over MCP the second clause is
always true, so the rule reduces to **never short-circuit in `run_retrieve`**.
Keep the intent label for diagnostics and attach a hint pointing at the tool
that *can* answer deterministically (`capabilities`, `find_files`). Tightening
the regexes is still worth doing for `pal ask`, where the same hijack returns a
STRUCTURE snapshot instead of an answer, but it isn't the MCP fix.

### 2. No way to read a found file — CONFIRMED

`find_files(name_glob="README*")` returns paths (verified on :8766: 6 matches,
including `/Users/tjm4/Developer/active/llmLibrarian/README.md`). No tool takes a
path. `find_files`' docstring says *"Pairs with: `query_personal_knowledge`
after selecting a target file/silo"* (`mcp_server.py:1504`), but
`query_personal_knowledge` has no file filter (`mcp_server.py:980-986`). The
docstring promises a next step that doesn't exist, so the model reached for
Open-WebUI's own knowledge-base tools.

**Options:**

| Option | Shape | Pros | Cons |
|---|---|---|---|
| A. `read_document` tool | `read_document(path, silo=None, start_chunk=0, max_chars=6000)` → chunks for that `source` ordered by `chunk_index`, joined; `next_start_chunk` for paging | Discoverable by name; one call answers "what does the README say"; works for PDFs/images (reads extracted text, not bytes) | New tool in the catalog (~1 KB of schema) |
| B. `source=` on `query_personal_knowledge` | adds `{"source": path}` to the where-clause (`retrieve_locked.py:72-86`) | Tiny change; semantic ranking *within* one file | A small model won't infer that `source=` means "read this file" |

**Recommend both**, A as the documented next step. Hard requirements: resolve
the path through `read_visible_manifest` exactly like `_resolve_indexed_image`
(`mcp_server.py:2266-2296`), so a private file opens only when its silo is named.
Read from the index, never from disk — that keeps it from becoming an arbitrary
local-file read. Cap the size and page it.

### 3. Payload bloat — CONFIRMED, with two multipliers the brief missed

Measured on :8766 (bytes of JSON):

| Response | Payload | Wire | Where the bytes go |
|---|---|---|---|
| Tool catalog, all 19 tools | — | 25,352 | `add_silo` 2.5 KB, `query_personal_knowledge` 2.1 KB, `multi_query_knowledge` 2.0 KB … |
| Catalog as Open-WebUI sees it (7-tool allowlist) | — | ~10,600 | ≈2.7k tokens of a 16k window, resent every turn |
| Lite catalog (2 tools) | — | 1,326 | |
| `list_silos` (9 silos) | 10,106 | 20,818 | **`exclude_patterns` 52.6%** — one identical 40-entry list ×9; `language_stats` 8.8% |
| `session_context` | 13,944 | 28,588 | `list_silos` + health + scope text |
| Car query, `n_results=40` default | 19,419 | 39,693 | 20 chunks (caps cut 40→20); `photo_metadata` 32% of chunk bytes, and it duplicates lines already in each chunk's `text`; internal fields (`_signals`, `rank`, `indexed_at`, `chunk_index`, `record_type`, null vision flags) ≈25% |
| Unscoped query, `n_results=10` | 19,915 | 40,381 | **6 chunks**: `chunks` 9,508 + `chunks_by_silo` 9,557 — the same dicts serialized twice (`mcp_server.py:1049-1053`) |

The two multipliers:

- **Wire = 2× payload on every dict-returning tool.** FastMCP emits both
  `content[0].text` and `structuredContent`. The brief's "20 KB `list_silos`"
  is the wire number. Which half Open-WebUI feeds the model is a client-side
  question (separate session).
- **Unscoped results are 2× again** because of `chunks_by_silo`.

What a small model actually uses from a chunk is `text`, `source`, `score`, and
sometimes `section`/`page`. That's the set the lite projection already keeps
(`mcp_server.py:2144-2164`).

### 4. Repeated identical calls — CONFIRMED (attribution corrected)

6× identical at 04:47:23–24Z (food), 10× at 05:32:49Z and 5× at 05:33:49Z
(README chat). The car chat sent one query.

**Concurrency today on :8766 (HTTP Chroma):**

- The in-process read mutex is disabled (`_mcp_read_lock_disabled`,
  `mcp_server.py:381-404`).
- `chroma_shared_lock` is a no-op (`src/chroma_lock.py:354-366`).
- FastMCP runs sync tools on a threadpool.

So 10 duplicates are 10 concurrent embed + query round-trips against `chroma
run`. That's safe for HNSW, since the server orders its own I/O and the
680 GB / desync incidents were embedded-client concurrency, but it's wasted CPU.

**Design that doesn't touch HNSW safety:**

- **Single-flight coalescing** (one in-flight computation per key, followers wait
  on its future) plus a **short TTL cache** (≈30 s, LRU ≈64), both *above*
  `run_retrieve`. The parallel bursts arrive before the first call completes,
  so TTL alone wouldn't catch them; coalescing does.
- **Key:** `(tool, normalized query, silo, n_results, section, doc_type)` plus
  `mtime_ns` of `llmli_registry.json`, `llmli_file_manifest.json` and
  `tax_ledger.json`. A privacy flip or a finished ingest therefore misses the
  cache. `bump_generation` is a no-op in HTTP mode (`src/chroma_client.py:442`),
  so file mtimes are the only write signal available.
- **Never cache** a result carrying `write_in_progress`, `busy`, `error` or
  `retryable`.
- **Response:** add `repeat_of` (timestamp of the first identical call) and,
  from the 3rd repeat on, `recommended_action: "identical result already
  returned — answer from it or change the query"`.
- **Audit:** still record every call, with `cached: true`.
- **Embedded mode:** reads still take `_chroma_lock`. The cache only removes
  Chroma calls; it never adds one, so no new concurrency reaches HNSW.

### 5. Private silos leak through the roster — CONFIRMED as designed; two more channels found

`list_silos` returns slug, path and `private: true` for `tax-0c9821db`,
`lab-history-d8dd1667` and `chat-archive` (`mcp_server.py:1368-1396` →
`state.list_silos`). That follows the written rule ("Knowing a private corpus
exists is allowed", `.claude/rules/silo-privacy.md`), and the exact slug is the
consent key. The roster therefore hands any caller the key.

Two more channels hand it out:

- **Every unscoped query response** carries `excluded_private_silos` (exact
  slugs) and a `privacy_note` that says *"pass silo=<slug> explicitly"*
  (`mcp_server.py:951-976`). The 10 unscoped README calls each received this.
- `session_context` returns `private_silos` and a `scope_policy` that names
  them (`mcp_server.py:1435-1460`).

**Consumers, and what reads the roster over MCP:**

| Consumer | Roster source | Opens private silos over MCP? |
|---|---|---|
| `pal` CLI (`pal ls`, `pal ask --in`) | `state.list_silos` directly | no — local process |
| macOS app | reads `llmli_registry.json` directly (`macos/Sources/llmLibrarian/Services/Collector.swift:76`) | no |
| `pal pull --watch` watchers | none; call `update_file`/`remove_file` on :8766 with their slug (`pal.py:1610`) | **write** path only — tax, lab-history and chat-archive all have watchers |
| Claude Code / Desktop | stdio processes, own `list_silos` | yes, by design ("naming is consent") |
| Open-WebUI | :8766, 7-tool allowlist | yes, technically — told "Do not query Tax" in prose only |

Hiding private silos from **read** tools on the HTTP endpoint breaks none of
these, as long as write tools keep accepting private slugs (watchers). See
[P1](#p1-private-silo-visibility-on-http).

### 6. Image silo indexed without vision — CONFIRMED, and "enable" is a no-op

State **changed during the investigation**. The registry now says
`image_vision_enabled: true` for `dad-new-car-d6e890e2`, updated 05:47:47Z. That
was done outside this session, probably with the new macOS "Enable image
vision" action (13a2855). All 22 chunks are still `summary_status: disabled`,
`indexed_at` 05:07:58Z. Turning vision on changed the flag and nothing else.

- **How it was added:** `pal pull <path>` at 05:07:58Z, watcher started
  05:08:01Z. That is exactly what macOS `SiloAddWorkflow` emits; the logs can't
  distinguish it from a manual `pal pull`. Every path defaults to off:
  - `_resolve_image_vision_enabled` returns `False` when nothing is requested
    (`src/ingest/__init__.py:505-521`);
  - the macOS add dialog's toggle is `@State imageVision = false`
    (`macos/Sources/llmLibrarian/Views/Components.swift:347`), whatever the
    folder contains;
  - the CLI is opt-in `--image-vision` (`cli.py:683`).
- **Why enable doesn't backfill:**
  - `applyImageVision` runs `pal pull <path> --image-vision` without `--full`
    (`macos/Sources/llmLibrarian/Model/Store.swift:464-476`);
  - incremental ingest skips any file whose mtime, size and hash are unchanged
    (`src/ingest/__init__.py:2404-2412`), and a vision-mode change isn't
    treated as a change.
- **Even when vision is on, MCP may not get summaries.**
  - Only text-forward images get an eager summary. Plain photos get `deferred`
    unless `LLMLIBRARIAN_IMAGE_EAGER_SUMMARY=1` (`src/processors.py:195-206`,
    `1724-1735`).
  - Query-time hydration is capped at one image per query
    (`src/query/core_support.py:391`, `lazy_budget = 1`).
- **Backfill cost:**
  - One `ask_image` on DSCN2763 with `gemma4:12b` took **38.7 s** end to end
    (may include model load).
  - Budget roughly 20–40 s per image: `dad-new-car` (9 images) ≈ 3–6 min,
    `photos_local` (15) ≈ 5–10 min, a 1,000-photo folder ≈ 6–11 h.
  - Re-embedding is negligible next to that.

**Related root cause: image hits always score 0.0.** `llmli_image` uses
Chroma's default `l2` space (created at `src/ingest/__init__.py:1767` without
`hnsw:space`), while `llmli` is `cosine`. `_query_image_collection` averages the
image L2² distance with the text cosine distance (`src/query/core_support.py:421-428`),
and `score = max(0, 1 - dist)` (`src/query/retrieve_locked.py:221-226`) clamps
to 0.

So every image-ranked chunk reports score 0.0, answer confidence "low" and
*"sparse match — consider a broader or rephrased query"*, which invites exactly
the rephrase loop seen. That happened even when the answer was in the payload:
the top chunk's OCR reads "THE ULTIMATE DRIVING MACHINE", i.e. BMW.

### 7. `ask_image` takes one file — CONFIRMED

`ask_image(file, question, silo)` (`mcp_server.py:2299-2340`). The query's
`recommended_action.files` listed `[DSCN2763, DSCN2771, DSCN2769]`
(`retrieve_locked.py:266-278`), unranked and with no instruction to try more
than one. The model picked the third and got "not discernible".

The same question on the first file (DSCN2763) answers *"make is BMW … model
not discernible"* in 38.7 s.

**Recommend:** `ask_image(question, files=None, silo=None, top_k=3)`.

- When `files` is omitted, take the top-k images from the image-vector search
  for `question` in `silo`.
- Send up to 3 images in one Ollama request (one multi-image message). That's
  cheaper than 3 sequential calls and lets the model compare them.
- Return per-file answers and an overall answer.
- Bound it by an overall timeout below Open-WebUI's tool timeout (to confirm in
  the client session).
- Make `recommended_action` ranked, and phrase it as "call `ask_image` with
  these files" rather than a bare list.

### 8. Process and code drift — ROOT CAUSE CHANGED

**8a. The ImportError is not a fresh Claude Code process.** The server answering
this session's `llmlibrarian` tools reports `"self": true` on **pid 51687**. It
was started **Mon 2026-09-28 13:17:23** by the Claude Desktop main app (pid
51442) and is shared with Code-tab sessions. On 09-28, `dev`'s
`src/file_registry.py` had no `read_visible_manifest`; that arrived with the
2026-09-30 01:01:48 fast-forward (file mtime matches).

Tools import lazily. `_collect_health_summary` imports `file_registry` early
(`mcp_server.py:536`, reached via `session_context` or `health`), so the 09-28
version stayed cached. `find_files` later imports `operations_find` from
today's disk (`mcp_server.py:1518`), which does
`from file_registry import read_visible_manifest` (`src/operations_find.py:23`)
and fails.

That makes it a **mixed-version module graph**, not a shadowing module, stale
bytecode or a uv env problem. Ruled out directly:

- a fresh `uv --directory ~/llmLibrarian run mcp_server.py` over stdio answers
  `find_files` correctly;
- importing in-process works;
- `.venv` has no copy of `file_registry`.

The same hazard applies to the :8766 HTTP server: it started 09-30 01:01:59 and
lazily imports modules that changed on disk afterwards (`mcp_server.py` itself
changed by 153 lines, plus `core.py` and `state.py`).

**8b. The 12 "stale" stdio processes aren't orphans.** Every `uv` launcher's
parent is a live Claude Code session process (`claude --output-format
stream-json …`, started 09-28 to 10-02) that the desktop app keeps resident. The
servers exit when their client does; the clients haven't exited.

- **Memory:** 4–19 MB RSS each, so the cost is stale code, not RAM.
- **Diagnosis:** `mcp_runtime_status` calls them "a stale one was likely never
  reaped", which is wrong.

**8c. New: the checked-in `.mcp.json` breaks in worktrees.** It sets
`LLMLIBRARIAN_DB=${LLMLIBRARIAN_DB:-./my_brain_db}` with `--directory
${LLMLIBRARIAN_HOME:-.}`. In a worktree that resolves to
`<worktree>/my_brain_db`, which doesn't exist, and creates a separate
Python 3.13 `.venv` there. Every `llmLibrarian` tool in this session returned
`db_exists: false`.

## Also checked

### Lite profile

Measured over stdio from `dev` against the live DB:

| Question | Lite today |
|---|---|
| Catalog cost | 1,326 B (vs ~10.6 KB for Open-WebUI's full allowlist) — the main win |
| Finding 1 (hijack) | **Worse.** `retrieve_knowledge` uses the same `run_retrieve`; `_compact_lite_retrieval` (`mcp_server.py:2167-2193`) drops `intent` and `note`, so the caller gets `{"chunks": [], "results_may_be_incomplete": false}` and nothing else |
| Finding 2 (read a file) | Same gap; lite has no `find_files` either |
| Finding 4 (duplicates) | Same |
| Finding 5 (roster) | **Regressed on `dev`** — `silo_roster` lists `chat-archive`, `lab-history-d8dd1667` (and tax) again (S1) |
| S2 (ledger) | Not affected — `retrieve_knowledge` requires `silo` |
| RAM for a second process | Idle ≈400 MB (f46826a); :8766 measured 567 MB idle, **1,353 MB** right after queries (embedding model loaded, reaped after idle) |

### Docstrings vs what the 3B-active model did

| Tool | Text | What happened |
|---|---|---|
| `find_files` | "Pairs with: `query_personal_knowledge` after selecting a target file/silo" | No such pairing exists; the model went to Open-WebUI's KB tools |
| `query_personal_knowledge` | "call list_silos first rather than inferring a silo's domain" | Costs 10 KB per call for a small model; `silo_roster` is the right size |
| `query_personal_knowledge` | "Intent routing is applied automatically." | Says nothing about routing producing an empty result |
| deterministic `note` | "Try rephrasing as a descriptive question" | The model did — four times, keeping "capabilities" |
| `coverage_note` on image hits | "sparse match — consider a broader or rephrased query" | Caused by the 0.0 score bug, not a sparse match |
| `ask_image` | "Do not use when … you need to find which image to ask about (`find_files` first)" | Filenames are `DSCN27xx`; `find_files` can't find a car by name. The real locator is `query_personal_knowledge` → `recommended_action` |
| server `instructions` | "call session_context(check_staleness=True)" | 14 KB and a source-tree walk at session start |

### Empty-result response audit

| Empty path | Signal returned | Actionable next step? |
|---|---|---|
| Deterministic intent (`core.py:100-110`) | `note` "try rephrasing" | **No** — the trigger isn't named, no tool is named; lite and `explain_retrieval` drop even the note |
| Private-scoped-away (unscoped) | `excluded_private_silos`, `privacy_note` | Yes, but the action is "pass the private slug" (finding 5) |
| Rebuild in flight | `write_in_progress`, `retryable`, coverage note | Yes |
| Lock busy | `busy`, `retry_after_seconds` | Yes |
| DB missing | `db_exists: false`, error | Yes for an operator; not actionable for a chat model |
| Unknown slug (lite) | "Call silo_roster and use one of its exact slugs" | Yes |
| Unknown slug (full) | `silo_warning` from `_safe_query` | Partly |
| Genuine no match | `coverage_note` "no chunks returned", confidence low | **No** — no suggestion to drop `silo`, widen, or call `find_files` |
| Image hits scored 0.0 | "sparse match — rephrase" | **Misleading** |

## Ranked fix list

Effort: S < 2 h, M ≈ half day, L 1–2 days. Compatibility columns: **pal**
(CLI and watchers), **mac** (macOS app), **CC** (Claude Code/Desktop stdio and
HTTP clients).

| # | Fix | Effort | Risk | Test plan | pal | mac | CC |
|---|---|---|---|---|---|---|---|
| 0 | **Restore a30aebf on `dev`** (`git cherry-pick a30aebf`; expect a conflict in `mcp_server.py` around `silo_roster`) and push `origin/dev`. Hold the :8766 restart until it lands | S | Low; conflict resolution is the only risk | Restored `tests/integration/test_silo_privacy_mcp_tools.py` (3 failing → green); `test_silo_privacy.py` display-name, roster and artifacts cases | none | none | `silo="Tax"` on a private silo now errors; must use the slug (intended) |
| 1 | **Privacy-filter `tax_ledger`**: when `silo_slug is None`, drop rows whose silo is in `private_silo_slugs(db)` (`retrieve_locked.py:282-309`); audit `run_ask`'s ledger use the same way. Separately, stop extracting ledger rows from non-tax silos | S | Low | `test_unscoped_tax_query_does_not_attach_private_ledger_rows` (fails today) + `test_explicit_silo_still_gets_its_ledger_rows` (control) | `pal ask --in tax-…` unchanged | none | Unscoped tax-ish queries lose ledger rows they shouldn't have had |
| 2 | **Never short-circuit in `run_retrieve`**: run retrieval for all intents (retrieve as `LOOKUP` for the six deterministic ones), keep `intent`, and add `recommended_action` pointing at `capabilities` or `find_files` with suggested args when the intent was deterministic (`core.py:100-110`) | S | Low–med: a true inventory ask ("file counts") now gets weak chunks plus a hint instead of nothing | `test_mcp_small_model_contract.py` (16 reach-retrieval cases + empty-result contract) | `pal ask` unaffected (uses `run_ask`) | none | Gets chunks where it got `[]` |
| 3 | **Empty-result contract**: every `chunks == []` response carries `recommended_action {tool, args?, reason}`. Lite projection keeps `note`/`intent`/`recommended_action`; `explain_retrieval` returns `note`. Genuine no-match suggests dropping `silo` or `find_files` | S–M | Low | `test_empty_deterministic_result_tells_the_model_what_to_do`, `test_lite_profile_keeps_the_reason_for_an_empty_result`; add one per row of the audit table | none | none | Additive fields |
| 4 | **Fix image scoring**: convert image L2² to cosine distance before averaging (L2²/2 for unit-norm vectors — confirm the adapter normalizes), or score image-path chunks by rank; never emit the "rephrase" note for image-vector hits (`core_support.py:421-428`) | S | Low; confidence numbers change for image queries only | Unit test with fake collections: an image hit with typical CLIP distances scores > 0 and isn't "low" | none | none | Better calibration |
| 5 | **Payload diet**: (a) drop `chunks_by_silo` or replace it with `{silo: [ranks]}`; (b) default `n_results` 40 → 10; (c) strip internal chunk fields from full-profile responses (keep in `explain_retrieval`), drop null fields, drop `photo_metadata` lines duplicated in `text`; (d) `list_silos`: hoist `exclude_patterns` to one top-level `default_exclude_patterns` (per-silo only when different), move `language_stats` behind `verbose=True`; (e) decide on `structuredContent` (P5) | M | Med: field-presence assumptions in tests and clients | Byte-budget tests per tool (`list_silos` < 4 KB for 9 silos; 10-chunk query < 8 KB); existing `test_mcp_*` suites | Watchers use `structuredContent` with text fallback (`pal.py:1635`) — safe either way | Reads registry file — none | Clients reading `chunks_by_silo` or `_signals` lose them (no reader outside `mcp_server.py` and tests; `_signals` stays internal to the pipeline and `explain_retrieval`) |
| 6 | **`read_document` + `source=`** (finding 2), visible-manifest resolution, index-only, paged; rewrite `find_files`' "Pairs with" | M | Med (privacy): must go through `read_visible_manifest`; add to `test_silo_privacy.py` per the rule file | Unit: private file refused unscoped, allowed with slug; paging; cap. Integration: README round-trip | none | none | New tool; Open-WebUI allowlist must add it (client session) |
| 7 | **Duplicate-call guard**: single-flight + 30 s TTL as specified in finding 4 | M | Med: stale result after a write if the key misses a signal; mitigated by mtime keys and no-cache on write/busy | Concurrency unit test (10 threads, 1 underlying call); invalidation on registry and manifest touch; `write_in_progress` not cached; privacy flip invalidates | none | none | Faster; new optional fields |
| 8 | **Vision backfill and defaults**: (a) treat a vision-mode change as "changed" for image files in incremental ingest (`ingest/__init__.py:2404-2412`), or make `applyImageVision` pass `--full`; (b) set eager summaries for vision-enabled silos; (c) warn on add when most files are images (see P3) | M | Med: unexpected long vision runs; show the cost estimate | Ingest unit test: incremental pull with flag false → true re-extracts images; Swift test for the dialog default | `pal pull --image-vision` starts doing work | Enable action starts doing what its label says | none |
| 9 | **Multi-image `ask_image`** (finding 7) and a ranked `recommended_action` | M | Med: latency (~40 s/image) vs client tool timeout | Resolver tests (privacy, top-k from search), timeout path | none | none | Signature gains optional params, backward compatible |
| 10 | **Drift hygiene**: (a) catch `ImportError` in tools and return "server code changed on disk since <start>; restart this MCP client" with pid and start time; (b) report `code_drift` (startup HEAD and `src` mtime vs now) in `mcp_runtime_status`/`health`; (c) import tool dependency modules eagerly at startup so a process is consistently old, not mixed; (d) classify stdio processes by parent liveness ("idle client session" vs orphan); (e) fix `.mcp.json` to need an absolute `LLMLIBRARIAN_DB` or point at the HTTP service | S–M | Low | Unit tests for the drift detector and the parent-liveness classifier | none | Could surface `code_drift` in status | Clearer failures; restart prompts |
| 11 | **Lite endpoint** (P2) | M | Med | Profile tests for the mounted app; RAM check | none | Show which endpoints are up | Open-WebUI repoint (client session) |
| 12 | **Tighten routing regexes** for `pal ask` (CAPABILITIES needs a "you / llmLibrarian / supported / index" context; STRUCTURE drops bare `inventory`/`directory`; TIMELINE needs an explicit ordering ask) | M | Med: shifts CLI answers; existing routing tests define the boundary | Extend `tests/unit/test_intent_routing.py` with the 13 hijack cases as non-deterministic | `pal ask` answers change for those phrasings | none | none once #2 lands |

Order rationale: 0–1 are privacy, one live and one a restart away. 2–4 remove
every empty or misleading result seen in the session for an S-sized change
each. 5–7 are context and loop economics. 8–9 are image quality. 10–12 are
hygiene and policy-gated work.

## Status of the fix list

All on `claude/infallible-goodall-5949ce` (base `dev` @ 13a2855). Full suite:
1,322 unit + 31 integration passing (`test_concurrent_subprocess_hnsw::t1` is
flaky under full-suite load and passes alone, 3/3).

| # | Commit | What landed | Differs from plan |
|---|---|---|---|
| 0 | 09e645e | a30aebf cherry-picked; conflict in `_resolve_indexed_image` resolved keeping `dev`'s `read_visible_manifest` *and* a30aebf's exact-slug guard | Also on local `dev` |
| 1 | 5d56fcc, faf3b32 | Ledger privacy filter at the single reader `load_tax_ledger_rows` (covers MCP and the CLI tax resolver); a folder path is no longer consent in the image resolver; ledger reads only PDF/scan/office/CSV/TXT sources and skips removed silos | Filter moved from `retrieve_locked` to the choke point. 5d56fcc is also on local `dev` |
| 2–3 | f56db0a | `run_retrieve` retrieves deterministic intents as LOOKUP and reports `deterministic_intent`; `src/mcp_contract.py` attaches `recommended_action` to every empty result (and `alternative_tool` to non-empty inventory-shaped ones) in all four retrieval tools; lite keeps `note`/`error`/`recommended_action` | — |
| 12 | f86b416 | Router rules need the question to be about the index; content verbs veto FILE_LIST/STRUCTURE | Done before 4–11 since 2 depended on it for clean tests |
| 4 | c309c21 | Image L2² → cosine (L2²/2, CLIP vectors verified unit-norm); image-led results get an "image match" coverage note instead of "rephrase" | — |
| 5 | 5da5c27 | Slim chunks, `silo_counts` instead of `chunks_by_silo`, n_results 40→12 (multi 20→10, cap 50→30), roster hoists `exclude_patterns`, `language_stats` behind `verbose` | Measured: list_silos 10.1→4.7 KB, session_context 13.9→6.0, car query 19.4→9.2, unscoped 19.9→8.5. `structuredContent` untouched (P5) |
| — | da6060f | See N1, N2 | New |
| 6 | ade5341 | `read_document` + `source=` (full and lite), `_resolve_indexed_file`, `find_files` `next_step` | — |
| — | c148440 | See N3 | New |
| 7 | 072c2c6 | Single-flight + 30 s TTL (`src/mcp_result_cache.py`), `repeat_of` / `repeat_notice`, audit per call with `cached` | — |
| 9 | d008d0e | `ask_image(question, files=[…])` one vision call for ≤4 images, or best `top_k` (default 2) matches; ranked `recommended_action` with args | Default 2, not 3 (see N7) |
| 8 | c2cf706 | Vision-mode change re-extracts images on incremental pulls (macOS action fixed with no Swift change); warning on new mostly-photo silos (CLI + `add_silo`) | (b) eager summaries not changed — still `LLMLIBRARIAN_IMAGE_EAGER_SUMMARY` |
| 10 | 44b7c16 | Light modules eagerly imported; `code_drift` in runtime status/health; ImportError middleware; stdio classified by client liveness; `.mcp.json` DB default; DB fallback requires a real store (N5) | query.core stays lazy (65 MB, chromadb) |
| 11 + P1 | d31ff58 | `LLMLIBRARIAN_MCP_PRIVATE_READS=none` and `LLMLIBRARIAN_MCP_LITE_PATH` mount (lite read policy default `none`) | Mechanisms only; defaults keep today's behavior |
| — | fd7f61a, 3512b77 | Server instructions and AGENTS/GUIDE/CLAUDE/rule docs | — |

## Found while fixing

- **N1. "image" replaced the text results.** `_IMAGE_QUERY_PATTERN` (image, ui,
  screen, dog, face, shown…) decided both "merge image hits" and "replace text
  hits with image hits", so "README image indexing capabilities" returned two
  fixture screenshots and no README. The replace path now needs a photo request
  (photo, picture, screenshot, or "image" not followed by indexing/vision/OCR…).
- **N2. Placeholder text embedded in every photo chunk.** "Text-forward image
  indexed with OCR only; multimodal vision disabled." matched every question
  about image indexing. New placeholders carry no such vocabulary; **each image
  silo picks this up on its next reindex**.
- **N3. Agent worktrees indexed.** The `llmlibrarian` silo watches the repo
  root, so `.claude/worktrees/<name>/…` and `.pytest_cache/README.md` were indexed
  (find_files listed the cache README first). Now default-excluded; applied on
  the silo's next pull.
- **N4. Exclude patterns match as substrings** — "env/" drops
  `environment-setup.md`, "build" drops `build_notes.md`, "dist" drops
  `distance.py`. Confirmed; out of scope here and offered as a separate task.
- **N5. A stray empty DB won the fallback.** Taking the Chroma lock on a missing
  DB path created `<worktree>/my_brain_db/.llmli_chroma.flock`, and
  `_resolve_db_path` accepted any existing directory. It now requires
  `llmli_registry.json` or `chroma.sqlite3`.
- **N6. The ledger held rows from test code.** `tests/unit/test_tax_resolver.py`
  and `tests/integration/test_tax_deterministic_qa.py` (indexed as part of the
  repo silo) produced W-2 rows, so an unscoped "W-2 wages 2025" via `pal ask`
  would have answered from fixtures. After the fixes, an unscoped ledger read
  returns no rows; `tax-0c9821db` by exact slug still returns its 274.
- **N7. `ask_image` latency is GPU contention, not image size.** One image
  ~21–23 s warm or cold; three in one call 94 s, and 370 s while
  `qwen3.6:35b-a3b` (24 GB, resident 8 h for Open-WebUI) held the GPU.
  Downscaling to 1,600 px measured identical (Ollama resizes), so it was
  reverted. Live: `ask_image(question="What make and model … color?",
  silo="dad-new-car-d6e890e2")` → "a black BMW … logo on the wheel hubs in
  DSCN2764/2763/2765".

## What TJ still has to do

These need your hands or your consent; none were done.

1. **Restart :8766** (`pal mcp stop && pal mcp start`, or let launchd restart it).
   Until then the resident process (pid 95443) attaches private tax-ledger rows
   to unscoped TAX_QUERY results. After the restart it serves local `dev`
   (privacy fixes only) — or merge this branch into `dev` first to get the rest.
2. **Push `dev`** (`git push origin dev`) so a30aebf cannot be lost again, and
   merge this branch when reviewed.
3. **Re-pull the affected silos** when convenient: `dad-new-car-d6e890e2` and
   `photos_local-9777636e` (N2 placeholders; dad-new-car's vision flag is on, so
   the pull now re-summarizes its 9 images, ~20–40 s each), and
   `llmlibrarian-46ad0cbe` (N3 worktree excludes, N6 ledger rows).
4. **Flip the policy switches you choose** (P1/P2 below) in `.env.mcp` and
   repoint Open-WebUI (client session) — e.g. `LLMLIBRARIAN_MCP_LITE_PATH=/lite`
   and Open-WebUI at `http://host.docker.internal:8766/lite/mcp` with tools
   `silo_roster,retrieve_knowledge,read_document,ask_image`.
5. **Restart Claude Desktop** to replace stdio pid 51687 (started 09-28; still
   raises the `find_files` ImportError, now with a restart hint once it runs new
   code — which it will not until restarted).

## Policy questions for TJ

The mechanisms for P1 and P2 shipped (d31ff58) with defaults that keep today's
behavior; P3's warning shipped (c2cf706) with the default still off.

### P1. Private-silo visibility on HTTP

Today any caller of :8766 sees the slugs (`list_silos`, `session_context`, and
every unscoped response's `excluded_private_silos`), and the slug opens the
silo. Options:

- **(a) Status quo** — the rule file's stance.
- **(b) Hide private slugs from read-side responses on HTTP only.** Return
  `private_silo_count` instead of names; keep stdio unchanged.
- **(c) Per-endpoint read policy** (`LLMLIBRARIAN_MCP_PRIVATE_READS=none|named`):
  `none` on the endpoint Open-WebUI uses, `named` on stdio. Write tools keep
  accepting private slugs, so watchers are unaffected.
- **(d) Per-client allowlist via auth tokens.** Strongest, but :8766 has no auth
  today (`LLMLIBRARIAN_MCP_REQUIRE_AUTH=false`, bound to 127.0.0.1;
  Open-WebUI on :3000 is reachable across the tailnet).

**Recommendation: (c) plus (b)'s count-only roster for `none` endpoints.**
Breaks no listed consumer (see the table under finding 5). It changes the
documented rule for HTTP, which is why it's your call.

### P2. Lite endpoint

- **(a)** a second process on :8767: +≈400 MB idle and up to ≈1.3 GB while
  active, plus a second Chroma HTTP client;
- **(b)** a second FastMCP app mounted at `/lite` in the :8766 process,
  sharing models, locks and the result cache;
- **(c)** keep full and rely on Open-WebUI's allowlist.

**Recommendation: (b)**, with lite = `silo_roster`, `retrieve_knowledge`
(+ `source=`), `read_document`, `ask_image`, and Open-WebUI pointed at
`/lite/mcp`. Don't make lite the default for every non-watcher HTTP client;
choosing by path keeps it explicit. Lite must get fixes 0, 2, 3 and 7 first, or
it inherits the empty-and-silent failure.

### P3. Vision default for photo folders

- **(a)** off (today);
- **(b)** detect and ask — the macOS dialog pre-toggles on when most files are
  images and shows a cost estimate (~30 s/image); `pal pull` and MCP `add_silo`
  print or return a warning;
- **(c)** auto-on.

**Recommendation: (b).** Silent auto-on would turn a 1,000-photo folder into
hours of vision calls.

### P4. Deterministic intents over MCP

Retire them for MCP entirely (always retrieve, hint the right tool — fix 2), or
keep a deterministic answer path in MCP too (return the `capabilities` text or
the `find_files` result inline)? **Recommendation:** hint only; inline answers
re-create a second, hidden router.

### P5. `structuredContent`

Drop it (halves wire bytes) or keep it for typed clients? `pal` handles both.
Decide after the client session confirms which half Open-WebUI feeds the model.

### P6. Claude Desktop/Code transport

Point both at the HTTP service instead of per-session stdio. That ends drift and
the 13 processes, but makes :8766 a single point of failure for interactive use.
In the meantime, restart Claude Desktop to replace pid 51687.

## Tests committed

Originally committed red against `dev` @ 13a2855 as b399a15. The branch was then
rebuilt on `dev` with the privacy fixes first, so b399a15 is no longer in its
history: its privacy tests landed with 09e645e/5d56fcc, and this plan and the
contract tests with f56db0a. All pass now, alongside the tests each fix added:
`test_mcp_payload.py`, `test_mcp_result_cache.py`, `test_read_document.py`,
`test_image_vision_mode.py`, `test_code_drift.py`, and new cases in
`test_ask_image.py`, `test_intent_routing.py`, `test_mcp_lite_profile.py`,
`test_silo_privacy.py` and `test_silo_privacy_mcp_tools.py`.

| Test | Pins |
|---|---|
| `tests/unit/test_mcp_small_model_contract.py` (18 failing, 1 control passing) | Finding 1 (8 hijack phrasings × scoped/unscoped), empty-result contract, lite projection |
| `tests/unit/test_silo_privacy.py` (5 failing) | S1 (display name, lite roster, `-artifacts` ×2), S2 (ledger leak) plus passing control |
| `tests/integration/test_silo_privacy_mcp_tools.py` (restored from a30aebf; 3 of 7 failing) | S1 end-to-end through the tool functions |
| `tests/integration/test_silo_privacy_retrieval.py` (1 assertion restored from a30aebf) | S1 `-artifacts` clause after a real reindex |

At the time of the investigation: 1,217 passed, 23 failed — exactly these cases.

## Evidence notes

- **No private content was read.** Private silos were touched only through row
  counts (tax ledger by `(silo, year)`), slugs and process metadata. The live
  `TAX_QUERY` leak was not triggered.
- **Queries this investigation added** to `query-audit.jsonl` on 2026-10-03Z,
  all public silos: the CAPABILITIES and FILE_LIST repros, the car query, an
  unscoped sourdough query, and one `ask_image` on DSCN2763.
- **Probe scripts** (raw streamable-HTTP and stdio JSON-RPC) lived in the
  session scratchpad. Re-create them from the payload tables above if needed.
- **Fix-phase live checks** ran the branch code in-process against the live DB
  (Chroma over HTTP, read-only, `LLMLIBRARIAN_QUERY_AUDIT=0`), plus four local
  `ask_image` vision calls on `dad-new-car`. Nothing was reindexed, and :8766,
  the watchers and Chroma were not restarted.
