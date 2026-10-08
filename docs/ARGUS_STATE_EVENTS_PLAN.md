# Plan: push state-change events to the monitor instead of being polled

Status: proposed (2026-10-07). No code changes yet.

## Why

The local monitor (Argus) now stores health as changes, not samples: a probe
row marks the moment a service's observed state changed. The remaining
continuous load is the monitor itself asking `GET /healthz` on the MCP server
every 10 seconds. Process death is already caught without polling: the monitor
watches the `llmlibrarian-mcp.service` unit over systemd D-Bus. What polling
still adds is "the process is up but cannot serve", and only this server knows
that. So the server should say so when it happens.

## Contract

Emit one JSON object per line on **stdout** of the MCP service process. The
monitor already tails this unit's journal and accepts any line containing a
JSON object with an `"event"` key.

```json
{"event": "service.state", "ts": "2026-10-07T22:15:03+00:00", "status": "ready", "reason": "listening; db present"}
```

| field    | required | meaning |
|----------|----------|---------|
| `event`  | yes      | always `service.state` |
| `ts`     | yes      | ISO-8601 UTC, seconds precision |
| `status` | yes      | `ready`, `degraded`, `recovered`, `stopping`, or `error` |
| `reason` | yes      | short human text; stable wording for the same cause |
| `metrics`| no       | small flat object, e.g. `{"db_exists": false}` |

Rules:

- **Transitions only.** Emit when the status or its reason changes, never on a
  timer. A healthy server that stays healthy emits once, at startup.
- `error` and `degraded` mean "can't do its job / partially can't". The monitor
  treats `error` (and `failed`) as unhealthy.
- Best-effort, like `_emit_usage_event`: a telemetry failure must never affect
  a request or startup.
- Keep usage telemetry where it is (`usage.log`); this stream is for health.

## Emission points

1. **Startup ready**: after the HTTP app is listening and the DB path check
   has run. Use `ready`, or `degraded` if `db_exists` is false.
2. **DB availability changes**: missing → present (`recovered`) and present →
   missing (`degraded`). The current `/healthz` computes `db_exists` per
   request; move that into a small state holder that emits on change.
3. **Store lock / write-in-progress**: when queries start failing because of
   the Chroma lock or a rebuild, emit `degraded` with a stable reason. Emit
   `recovered` when the first query succeeds afterwards.
4. **Graceful shutdown**: emit `stopping` from the shutdown hook.
5. **Fatal error**: the top-level handler emits `error` before exiting
   non-zero.

A tiny `HealthState` helper (current status + reason; `set(status, reason)`
emits only when either changed) keeps this to one call per site.

## Monitor-side follow-up (separate repo)

Once these events flow:

- relax the `llmlibrarian.mcp` HTTP binding from 10 s to a slow fallback
  (≥5 min), or disable it;
- have the monitor's evaluation honour the latest `service.state` event for
  the service (today it only considers work events and probes);
- stop applying the silence threshold to this service. With no heartbeat,
  silence is the normal healthy state, and D-Bus covers process death.

## Acceptance

- Unit test: driving `HealthState` through ready → degraded → degraded (same
  reason) → recovered emits exactly three lines, each one valid JSON matching
  the contract.
- No periodic emitter exists (grep for timers/sleeps around the emitter).
- `journalctl --user -u llmlibrarian-mcp -o cat | grep service.state` shows a
  single `ready` line after a restart.
