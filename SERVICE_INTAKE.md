# Service Intake Flow — llmLibrarian

This intake process determines whether a service is ready to be monitored. The goal is to define, per service, what healthy looks like before any dashboard or alerting logic is built. Outputs feed directly into the service registry and log contract — nothing gets defined in the monitoring app that wasn't answered here first.

**Scope:** operator-facing processes in this repo (`pal`, `llmli`, `mcp_server.py`, systemd/launchd units under `deploy/`). Ollama and the host OS are external dependencies; note them where relevant but do not register them as llmLibrarian-owned services unless you operate them.

**Registry anchors:** `~/.pal/` (pal state, watch logs, `daemon.json`), `LLMLIBRARIAN_DB` (Chroma + `llmli_registry.json`), MCP HTTP default `127.0.0.1:8765`.

---

## Summary


| Service                                                   | Stage 1      | Notes                                                           |
| --------------------------------------------------------- | ------------ | --------------------------------------------------------------- |
| MCP HTTP server (`llmlibrarian-mcp`)                      | **Pass**     | systemd/launchd; `/healthz` liveness                            |
| Silo watch daemon (`llmlibrarian-watch-<slug>`)           | **Pass**     | One unit per indexed bookmark; depends on MCP HTTP              |
| Foreground watch (`pal pull PATH --watch`)                | **Pass**     | Same semantics as daemon; lock file under `~/.pal/watch_locks/` |
| One-shot ingest (`pal pull`, `llmli add`, MCP `add_silo`) | **Deferred** | On-demand unless you add a schedule                             |
| MCP stdio (`mcp_server.py`, Claude Desktop)               | **Deferred** | Ephemeral per IDE session; no silence window                    |
| `ensure_self_silo` / dev `__self__` index                 | **Deferred** | Triggered by `pal ask` in dev checkouts, not a daemon           |
| Query / ask (`pal ask`, MCP `retrieve`)                   | **Deferred** | Request/response; not a background job                          |


---

## Stage 1 — Gate (pass all 4 or stop)

### MCP HTTP server — `llmlibrarian-mcp`


| Gate                | Answer                                                                                                                                                                  |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Defined trigger?    | **Yes** — continuous (`KeepAlive` / `Restart=always`); starts at boot via `pal install --mcp` or `deploy/systemd/install.sh`                                            |
| Observable success? | **Yes** — `GET /healthz` returns HTTP 200 with `ok`, `service`, `version`, `db_exists`, `started_at`; process listens on `LLMLIBRARIAN_MCP_HOST:PORT` (default `127.0.0.1:8765`) |
| Structured output?  | **Yes** — JSON probe body (no Chroma open); deep diagnostics via MCP `health()`; supervisor logs via journald (Linux) or `{{LOG_DIR}}/llmlibrarian-mcp.{stdout,stderr}.log` (launchd) |
| Max silence window? | **Yes** — **60s** without a successful `/healthz` while the unit is supposed to be enabled                                                                              |


### Silo watch daemon — `llmlibrarian-watch-<slug>` / `io.llmlibrarian.watch.<slug>`


| Gate                | Answer                                                                                                                                                     |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Defined trigger?    | **Yes** — continuous: filesystem events (watchdog) + periodic reconcile (`--interval`, default **600s** in daemon units; foreground default **10s**)       |
| Observable success? | **Yes** — process running; rotating log at `~/.pal/logs/watch-<slug>.log`; periodic reconcile lines; `pull complete after +N…` after work                  |
| Structured output?  | **Yes** — human lines (`%(asctime)s %(message)s`) plus JSON `event` lines (`reconcile`, `batch_drain`, …); failures appended to `llmli_last_failures.json` |
| Max silence window? | **Yes** — **2× reconcile interval** (default **20 min**) with **no** log activity while source files have changed; tighten per silo if high-churn          |


### Foreground watch — `pal pull <path> --watch`


| Gate                | Answer                                                           |
| ------------------- | ---------------------------------------------------------------- |
| Defined trigger?    | **Yes** — same as daemon watch; stops on Ctrl+C                  |
| Observable success? | **Yes** — PID lock in `~/.pal/watch_locks/`; `pal pull --status` |
| Structured output?  | **Yes** — same log path convention as daemon                     |
| Max silence window? | **Yes** — same as daemon watch for that silo                     |


### Deferred (Stage 1 fail or N/A)


| Service                  | Blocking reason                                                                                |
| ------------------------ | ---------------------------------------------------------------------------------------------- |
| One-shot ingest          | No schedule unless you cron it; success = exit code + optional `LLMLIBRARIAN_STATUS_FILE` JSON |
| MCP stdio                | No long-lived process; "sometimes" when the IDE connects                                       |
| `ensure_self_silo`       | Runs on demand when dev `pal ask` detects stale/missing `__self__`; not a service              |
| `pal ask` / MCP retrieve | User- or agent-initiated RPC, not a background runner                                          |


---

## Stage 2 — Questionnaire (gate-passers only)

### 1. MCP HTTP server (`llmlibrarian-mcp`)

#### Identity

- **One sentence:** Exposes llmLibrarian MCP tools over HTTP/SSE so `pal pull --watch` and remote agents can call `add_silo`, `retrieve`, `health`, etc., without spawning a new Python process per request.
- **Triggers:** systemd user unit `llmlibrarian-mcp@<user>.service` or launchd `com.llmlibrarian.mcp`; `ExecStart` → `scripts/run_mcp_http.sh` → `mcp_server.py` with `LLMLIBRARIAN_MCP_TRANSPORT=streamable-http` (from `.env.mcp`).

#### Execution

- **Frequency / silence:** Continuous; expected always-on when watch mode or HTTP clients are in use.
- **Max run duration before hung:** N/A (long-lived). Treat **no response on `/healthz` for 60s** as hung/dead.

#### Health

- **Success:** HTTP 200 on `/healthz` with `db_exists: true` and non-null `started_at`; MCP `health()` for silo audit, storage bloat, and `ingest_failures`.
- **Silent failure risk:** **Low** for liveness (process down → healthz fails). **Medium** for functional health: `db_exists: false` on probe while process is up; broken embeddings or stale `mcp_server.py` vs packaged extension — use MCP `health()` and `pal extension pack`, not healthz alone.
- **Failure from outside:** Connection refused; 401 if `LLMLIBRARIAN_MCP_REQUIRE_AUTH=true` without token; crash loop in journald; lock contention if a second HTTP instance tries to start.

#### Alerting

- **Consecutive failures:** **1** for healthz down (hard down).
- **Missed window:** N/A (continuous). Use healthz interval check, not cron.
- **Downstream impact:** **Yes** — `pal pull --watch` refuses to start without MCP; all daemon watch units call MCP for incremental index; agents lose RAG tools.

#### Logging current state

- **Where / format:** Linux: `journalctl -u llmlibrarian-mcp@$USER`; macOS: `deploy/launchd` stdout/stderr paths; app log level `LLMLIBRARIAN_MCP_LOG_LEVEL` (default `warning`).
- **Tells what happened?** Partially — startup/errors and HTTP stack; tool-level failures are in MCP tool responses, not always stderr.
- **How you know it's broken:** `curl -sS http://127.0.0.1:8765/healthz` fails; `pal pull --watch` prints MCP unreachable; `pal install --mcp` unit inactive.

#### Dashboard

- **At a glance:** healthz up/down, `version`, `started_at`, `db_exists` from probe; silo audit from MCP `health()` when needed.

---

### 2. Silo watch daemon (`llmlibrarian-watch-<slug>`)

#### Identity

- **One sentence:** Keeps one indexed silo in sync with its source folder by debouncing filesystem events and calling MCP to re-ingest changed paths.
- **Triggers:** Per-bookmark systemd/launchd unit generated by `pal daemon install` / `pal daemon sync`; runs `pal pull <source> --watch --interval <N> --debounce <N>` (daemon defaults: interval **600s**, debounce **30s**).

#### Execution

- **Frequency / silence:** Event-driven + reconcile every **interval** seconds. Silence alert if no log lines for **2× interval** while files under the source root have newer mtime than registry `updated`.
- **Max run duration before hung:** No fixed cap on a single ingest (large folders can run minutes). Treat **stuck queue** as: repeated `queue loop error` or same path re-queued with growing `attempts` for **> 30 min**.

#### Health

- **Success:** Watcher process alive; reconcile runs without error spam; after edits, log contains `pull complete after +N queued…` or `All files up to date` equivalent via MCP path; silo `chunks_count` / registry `updated` advances when content changed.
- **Silent failure risk:** **Yes** — process up, MCP down after start (healthcheck only at watch start); process up, MCP up, but ingest fails per-file (`N file(s) failed via MCP`) while the watcher keeps running; missed events partially mitigated by reconcile loop.
- **Failure from outside:** MCP unreachable; Chroma lock held by concurrent `pal pull`; disk full; source path deleted (job may be removed on `pal daemon sync`).

#### Alerting

- **Consecutive failures:** **2** consecutive reconcile cycles with MCP/queue errors before page (noise from transient locks).
- **Missed window:** **Warning** at 1× interval without activity if silo is low-churn; **alert** at 2× interval if high-churn or `is_stale` from `list_silos(check_staleness=True)`.
- **Downstream impact:** **Yes** — stale index; `pal ask` and MCP `retrieve` return outdated or empty context for that silo.

#### Logging current state

- **Where / format:** `~/.pal/logs/watch-<slug>.log` (rotating, 1 MB × 5); stderr may mirror to `watch-<slug>.stderr.log` for systemd units.
- **Tells what happened?** **Yes** for automation — JSON `batch_drain` events include `errors`, `last_failures_path`, `last_failure_count`; human lines for tailing. Full inventory: `llmli log --last` / `llmli_last_failures.json` (watch and bulk ingest append here).
- **How you know it's broken:** `pal ls --status` / `pal ls --jobs`; `pal daemon logs <slug>`; empty retrieval with `chunks_count > 0`; `systemctl --user status llmlibrarian-watch-<slug>`.

#### Dashboard

- **At a glance:** per-silo watcher state (running / missing), last log line timestamp, last successful `pull complete`, `stale_file_count` from registry staleness check.

---

### 3. Foreground watch (`pal pull <path> --watch`)

Same answers as **Silo watch daemon**, except:

- **Identity:** Manual/dev session; lock file `~/.pal/watch_locks/<silo>.pid` instead of systemd.
- **Triggers:** Operator command, not install-time unit.
- **Dashboard:** Include in watch table via `pal pull --status` / `pal ls --jobs` when lock present.

---

## Log contract (cross-service)


| Signal            | Location                                   | Shape                                                                                   |
| ----------------- | ------------------------------------------ | --------------------------------------------------------------------------------------- |
| MCP liveness      | `GET /healthz`                             | `{"ok", "service", "version", "db_exists", "started_at"}` — probe only, no Chroma open |
| MCP deep health   | MCP tool `health()`                        | JSON: `silo_audit`, `ingest_failures`, `query_health`, Chroma bloat hints              |
| Ingest completion | `LLMLIBRARIAN_STATUS_FILE` (optional)      | JSON: `slug`, `files_indexed`, `failures`, `chunks_count`, `updated`, `elapsed_seconds` |
| Ingest failures   | `$LLMLIBRARIAN_DB/llmli_last_failures.json` | `[{"path", "error"}, …]` capped; bulk replace + watch/MCP append                        |
| Watch activity    | `~/.pal/logs/watch-*.log`                  | Human: `YYYY-MM-DD HH:MM:SS message`; machine: JSON lines with `"event"` key            |
| Watch events      | same log file                              | `watch_start`, `reconcile`, `batch_drain`, `queue_loop_error` (disable: `PAL_WATCH_JSON=0`) |
| Silo registry     | `$LLMLIBRARIAN_DB/llmli_registry.json`     | per-silo `updated`, paths, chunk counts                                                 |
| Operator summary  | `pal ls --status`                          | human text: health summary + daemon job table                                           |
| Query-time errors | `$LLMLIBRARIAN_DB/llmli_query_health.json` | recent index/query errors                                                               |


---

## External dependencies (not intake-complete)


| Dependency          | Role                                   | Monitor separately?                          |
| ------------------- | -------------------------------------- | -------------------------------------------- |
| **Ollama**          | LLM for `pal ask` / `llmli ask`        | **Yes** — ingest and watch do not require it |
| **ChromaDB / disk** | Vector store under `LLMLIBRARIAN_DB`   | Via MCP `health()` storage summary           |
| **Embedding model** | ONNX / sentence-transformers at ingest | Via MCP `health()` embedding fields          |


---

## Recommended registry entries

```yaml
services:
  - id: llmlibrarian-mcp
    kind: http_daemon
    check: GET http://127.0.0.1:8765/healthz
    silence_seconds: 60
    logs: journald|launchd  # platform-specific

  - id: llmlibrarian-watch
    kind: per_silo_daemon
    unit_pattern: "llmlibrarian-watch-*.service"  # or io.llmlibrarian.watch.*
    log_glob: "~/.pal/logs/watch-*.log"
    silence_seconds: "{{ 2 * job.interval }}"
    depends_on: [llmlibrarian-mcp]
```

---

## Open gaps (before alerting goes live)

1. **`pal sync` command** — docs/warnings reference it for `__self__` staleness; monitoring should key off `ensure_self_silo` / registry mtime until a dedicated sync command exists.
2. **Auth** — when `LLMLIBRARIAN_MCP_REQUIRE_AUTH=true`, health checks must send `Authorization: Bearer …` (same as `pal` watch mode).

### Watch JSON example (`batch_drain`)

```json
{"event":"batch_drain","ts":"2026-05-19T12:00:00+00:00","silo":"docs","updated":2,"removed":0,"skipped":1,"errors":[],"duration_ms":412,"last_failures_path":"/path/db/llmli_last_failures.json","last_failure_count":0}
```

---

*Filled for repository state as of 2026-05-19. Update when defaults in `src/jobs_runtime.py`, `deploy/`, or MCP transport change.*