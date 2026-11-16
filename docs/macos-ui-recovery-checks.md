# macOS UI recovery checks

The UI must preserve a path from a visible problem to evidence, an appropriate
action, observable progress, and a verified result. A subprocess exit code is
evidence about that command, not proof that the desired state exists.

## Repeatable checks

Run `macos/test.sh` on macOS. It includes `bash macos/test-flows.sh`, which
compiles the production `LibrarianStore` with a temporary filesystem, real
fixture subprocesses, and simulated service state. It never mutates launchd or
the user's index. Build/install lifecycle tests run with
`uv run pytest -q tests/unit/test_macos_build.py` and fake platform tools.

| Scenario | Required outcome | Coverage |
|---|---|---|
| Missing folder at Add | Explain before starting a process | Store scenarios |
| Existing folder added twice | Select existing collection; no second ingest | Store scenarios |
| Index process fails | Keep stderr and log; stop watcher setup | Store scenarios |
| Index process is busy (75) | No false “added” message or downstream setup | Store scenarios |
| Exit zero with unresolved failures | Report problems; stop success chain | Store scenarios |
| Exit zero without indexed files | Report unverified result | Store scenarios |
| Unreadable activity history | Do not claim indexing is clean | Store scenarios |
| Watcher setup exits zero but watcher absent | Explain incomplete setup | Store scenarios |
| Missing Python executable | Finish failed job and notify workflow callback | Store scenarios |
| Large stdout/stderr | No pipe stall; bounded UI tail; full durable log | Store scenarios |
| Repeated reindex clicks | One active job for the same folder | Store scenarios |
| Remove or cancel confirmation | No process before confirmation; source files kept | Store scenarios |
| Repeated removal | One job; selected collection cleared after verified success | Store scenarios |
| Removal leaves registry, bookmark, or watcher | Report incomplete cleanup with durable log | Store scenarios |
| Chroma deletion fails | Nonzero exit, retry instructions, retained collection context | CLI + Store scenarios |
| Replacement indexing succeeds | Keep old index; removal is separate | Store scenarios |
| Private-folder replacement | Refuse shared replacement; preserve old index | Store scenarios |
| Service command accepted, health fails | Retain service/log context; no success claim | Store scenarios |
| Service becomes healthy | Report verified running state | Store scenarios |
| Service command denied | Explain error and offer service/log context | Store scenarios |
| Image model not ready | Specific guidance despite ledger category “other” | UI contract tests |
| Unknown error | Admit unknown cause; offer full diagnostic | UI contract tests |
| Problem filter | Match names, paths, errors, and collection slug | UI contract tests |
| Build metadata absent or invalid | Explicit unknown state; no invented freshness | UI contract tests |
| Build/sign/stage failure | Installed app kept running | Build script tests |
| Rebuild | Cache icon; stage before closing; launch installed bundle | Build script tests |

## Visual and navigation checks

Use the existing isolated snapshot mode from `macos/README.md`. Inspect both
appearances for Overview, Silos, Services, searches, changes, problems, About,
and Jobs. The fixtures include encrypted PDFs and repeated image-model failures.
In the installed app verify:

- Overview failing collection → scoped Problems → full file error → collection details.
- More than 200 failures → Load More; filters explicitly search loaded rows.
- Unreadable problem ledger still leaves service/folder actions accessible.
- Toolbar Jobs → completed result → full log or retry → Problems.
- Service start/restart → checking state → success or log-backed failure.
- About → build identity → rebuild log; rebuild disabled while a job is running.
- Filtering Silos/Services clears selections hidden by the filter.

## Boundaries

These checks do not prove every external model, filesystem, or launchd failure
is repaired. Model installation and OCR/conversion remain user actions with
diagnostic guidance. Private-folder relocation has no safe migration command
in the current UI: restore the original path rather than creating a shared
copy. The app keeps old indexes during shared-folder replacement and requires
explicit removal after review. Shared replacement is a new collection, not a
metadata-preserving migration.

Job cards cover the current app session; full logs persist under
`PAL_HOME/logs/jobs`. Logs may contain local file paths and error text. Problem
occurrence counts are labeled “recorded times,” not claimed to be unique model
attempts. A rebuild updates the GUI bundle and does not restart Python services.
