---
description: Redesign of the macOS Overview's information hierarchy and the data contract behind it. Open before changing OverviewView, ActivityLedger.swift, or activity_ledger.overview().
type: plan
globs:
  - "macos/Sources/llmLibrarian/Views/OverviewView.swift"
  - "macos/Sources/llmLibrarian/Services/ActivityLedger.swift"
  - "src/activity_ledger.py"
updated: 2026-10-18
---

# Overview: information design and data contract

**Status:** implemented (summary/detail split, rollup views, Overview, Activity tabs). Deviations from the proposal are noted inline.

**Decision:** the Overview shows four things in priority order:

1. what needs action
2. three numbers with trends
3. the busiest collection
4. one collections table

Everything else, including file lists, query lists, top documents and assistants, moves to a detail page reached by clicking. The data contract follows the same split: a cheap aggregate read for the Overview, and on-demand detail reads with filters. The aggregation itself is defined once, as SQL rollup views inside the ledger.

Related: [library-activity-overview.md](library-activity-overview.md) (the ledger), [../TECH.md](../TECH.md#activity-ledger).

## What is wrong with the current page

The questions an Overview must answer in five seconds:

| # | Question | Where the current page answers it |
|---|---|---|
| Q1 | Is anything broken or need me? | "Attention needed", **below the fold**, after six other blocks |
| Q2 | Is the library changing / being used, more or less than usual? | Summary prose, two charts, two metric rows: **four times** |
| Q3 | Which collections are active or neglected? | Changes list, Use list, Health table: **three tables** keyed by collection |
| Q4 | What specifically changed / was asked? | Inline disclosures and filename lists, on the overview itself |

Diagnosis:

- **No hierarchy.** There are eight blocks: header, summary, two charts, changes, use, attention, health and connect. Each is a GroupBox with a header at equal weight, so the eye has no entry point.
- **The same fact is shown three or four times.** "42 searches, up from 28" appears in the summary sentence, the searches chart, the Use metric and the per-collection rows. Service health appears in the toolbar strip, the page header and the sidebar footer.
- **Priority is inverted.** The only actionable content, Attention, sits under two decorative charts.
- **Detail lives on the summary page.** Top-document filenames, assistants, file and query disclosures are Q4 material, and they are where the page gets dense.
- **Charts are expensive for what they say.** Seven daily bars with single-digit counts take about 180 pt of height. On real data the change chart is empty for weeks, because history started today. Two side-by-side charts with different y-scales invite a false comparison.
- **Prose hides numbers.** Four sentences on one line can't be scanned. Numbers belong in tiles; prose should add only the one thing tiles can't say, namely *which* collection.
- **Notation is cryptic.** "+35 ~56 −7" needs a legend, and the legend is on another chart.
- **Empty states cost full height.** "No documents changed" still renders a metric row, a breakdown and a divider.

## Proposed layout

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Overview                                         [Today | 7 days | 30 d] │
│ Research was the busiest collection this week.                           │  ← one line, the only prose
├──────────────────────────────────────────────────────────────────────────┤
│ ● 2 things need you   2 files failed to index in Research · Watcher …  ›│  ← only when non-empty; one row
├────────────────────────┬────────────────────────┬────────────────────────┤
│ Documents changed      │ Searches               │ Found no sources       │
│ 98        ▁▃▅▂▇▃▁      │ 42        ▂▅▃▇▄▂▅      │ 6  (14%)  ▁▁▂▁▁▁▂      │  ← value, sparkline, delta
│ ↑ 40% vs last week     │ ↑ 50% vs last week     │ ↓ from 9               │
├────────────────────────┴────────────────────────┴────────────────────────┤
│ Collections                                                  sorted: activity ▾
│  Research         21 changed   21 searches   2 hr ago     ● 2 failing    │
│  Project Notes    14 changed   14 searches   5 hr ago     ●              │
│  Photo Journal  🔒  —            —           12 days ago   ●              │
│  …                                                                       │
└──────────────────────────────────────────────────────────────────────────┘
```

**Rules:**

- **Attention is a strip, not a section.** It is a single row with a count and the top item, and it is absent when everything is fine. Clicking it opens the detail page filtered to problems.
  - Severity has two tiers:
    - **Needs action:** service down, files failing, folder missing, watcher stopped.
    - **Worth a look:** searches that found no sources. These are a usage signal, not an error, so they live in the third tile rather than in Attention.
- **Three tiles, no charts.** Each tile shows a value, a sparkline of the window's buckets, and a delta. With a short history the delta line reads "history since Oct 18" and no arrow is drawn. Clicking a tile opens the matching detail tab.
- **One collections table** replaces Library changes, Library use and Collection health.
  - Columns: name (with lock), changes, searches, last activity, status.
  - Rows are sorted by activity, then by name.
  - Unsearched or unchanged collections stay visible with "—", which keeps neglected collections in view.
  - Clicking a row opens the detail page filtered to that collection.
- **The summary is cut to one headline.** It is the only non-numeric insight. The full sentences stay in `pal activity`, where text is the medium.
- **Elsewhere:**
  - The header status line is dropped; the toolbar strip already says it.
  - Connect moves to Services.
  - The chunk count is not shown.

## Detail page: Activity, reworked

The existing Activity page becomes the destination for every click. It has three tabs, and all three share the window picker and a collection filter:

| Tab | Content | Source |
|---|---|---|
| Searches | The current query table, plus a "no sources" filter, top documents and assistants as a sidebar summary | ledger `searches` / `search_hits` (falls back to query audit for client detail) |
| Changes | File events: time, collection, file, kind; grouped by collection | ledger `file_events`, `silo_events` |
| Problems | Failure groups with paths and sample error, service issues, missing folders | ledger `failures` + `store.issues` |

Navigation state is `page = .activity`, `activityTab` and `activityFilter: ActivityFilter(silo:, kind:)`, set by the Overview click handlers.

## System design consequences

### Split the read contract

Today one `ActivityOverview` carries everything, including up to 20 files per silo and every search in the window, and it is rebuilt every 12 s refresh. The Overview needs about 2% of that.

```
           ┌──────────────── llmli_activity.sqlite3 ────────────────┐
writers →  │ file_events  silo_events  searches  search_hits failures│
           │        ▲ defined once, created by activity_ledger.py    │
           │   VIEW bucket_rollup(bucket, silo, added, updated,      │
           │        removed, searches, no_source)                    │
           └────────────────────────┬───────────────────────────────┘
                                    │
          ┌─────────────────────────┴──────────────────────────┐
          ▼                                                    ▼
 summary(window)  — every refresh                 detail(filter, window, limit) — on open
 totals, deltas, sparkline buckets,               file events / searches / failures
 per-silo aggregates, attention counts            paged, filtered by silo/kind
 (Overview, menu bar, pal activity)               (Activity page, pal activity --detail)
```

- **`summary`** returns only aggregates.
  - Its size is O(collections + buckets), not O(events).
  - It is the cross-language contract; the shared fixture pins it.
- **`detail`** returns rows for one filter, and only while the detail page is open.
  - No parity fixture is needed: it is a plain filtered SELECT whose shape the UI owns.

### Put the aggregation in SQL views, once

The Swift reader currently re-implements `overview()` in about 250 lines, and every UI change risks drift between the two. The proposal is for `activity_ledger.py` to create versioned views, with `schema_version` bumped:

- **`daily_rollup`**: one row per (local day, silo) with change and search counts. Changes exclude the `repair` and `rehydrate` origins.
- **`hourly_rollup`**: the same per local hour, for Today.
- **`open_failure_groups`**: one row per (silo, error_class) with count, last_seen and a sample error.

*As built:* the views stay row-level (`activity_changes_v1`, `activity_searches_v1`, …) with
`day`/`hour` columns rather than pre-aggregated, because the previous period ends at a
time of day (`now − N days`) that a daily or hourly rollup cannot cut exactly. Readers
filter by `ts` and `GROUP BY day, hour`.

Both readers then do `SELECT … FROM daily_rollup WHERE day BETWEEN ? AND ?`. That leaves period arithmetic (which days count as "this week") as the only logic to mirror, and it is about 15 lines.

Local time in SQLite (`'localtime'`) follows the reading process's time zone. Python and the app run on the same machine, so the two agree.

Per-silo **unique files surfaced** can't be summed from daily rows, because files repeat across days. Recommendation: drop it from the Overview, where it is detail-level information, and compute it in `detail`.

### Refresh cost

- **Today:** about 8 queries every 12 s, scanning all events in the window and the previous window, plus the hit join.
- **After the change:**
  - 3 queries against the views, which use the `ts` indexes.
  - Detail queries run only while the page is visible.
- It is fine at the expected scale: thousands of events a month, with retention of 180 days.
- **Revisit** above roughly 1M events. At that point, materialise the rollups into a table maintained on write instead of using views.

### Attention model

There is one `AttentionItem { severity, kind, title, detail, count, target }` list, built from two sources:

- **`store.issues`** (services, watchers, folders, bookmarks)
- **ledger `open_failure_groups`**

Deduplication: a watcher-down issue and failures for the same silo show as one row with both facts. No-source searches are not attention items.

## Trade-offs

| Choice | Gain | Cost |
|---|---|---|
| Tiles + sparklines instead of charts | Half the height; scannable; works on sparse data | Loses hour-level shape on the Overview (available on detail) |
| One collections table | Removes 2 of 3 per-collection lists | Wider rows; unique-files column dropped |
| Headline only, no summary prose | Removes the third copy of every number | The "story" voice moves to the CLI only |
| SQL views as the shared contract | Logic lives once; Swift reader shrinks ~70% | Schema migration (views versioned); SQL less testable than Python, though the fixture still pins outputs |
| Summary/detail split | Cheap refresh; detail pageable | Two code paths; detail needs its own navigation state |
| No-source searches as a usage tile, not attention | Attention stays truly actionable | A real retrieval regression shows as a rising rate, not an alert, until a threshold rule exists |

## What to revisit as it grows

- **Alerting threshold.** If the no-source rate exceeds N% of searches for a collection, promote it to Attention. Needs a few weeks of real history to set N.
- **Materialised rollups** once events exceed about 1M, or if the menu bar starts showing live counts.
- **Many collections.** With 30+ collections the table needs grouping (active / quiet / private) or a "show quiet" toggle.
- **Linux.** A web or TUI view of the same `summary()` contract. The views make that cheap.

## Implementation order

1. **Data contract.** Add the views and `summary()`/`detail()` in Python; regenerate the fixture; add `pal activity --detail`. No UI change.
2. **Swift reader.** Rewrite it on the views; parity test against the new fixture.
3. **Overview.** Rebuild it to the layout above.
4. **Activity page.** Add the tabs, the filters and click-through navigation.
5. **Docs and screenshot.**
