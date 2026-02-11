# llmLibrarian (llmli)

llmLibrarian treats personal data the way people actually experience it: fragmented, time-bound, and meaningful only in context. Instead of optimizing for maximum knowledge or universal answers, it optimizes for **continuity**—helping you reason over what you’ve saved, why you saved it, and how it relates over time. By keeping context local, scoped, and user-defined, it appeals to people who want tools that work with their thinking, not over it: those who prefer intentional curation over infinite feeds, clarity over convenience, and systems that respect personal history rather than flatten it into generic “knowledge.”

llmLibrarian keeps the language model **intentionally stateless**. There’s no long-running chat memory, no hidden conversational context, and no evolving agent persona. Every answer is derived entirely from the data you’ve chosen to index and the question you ask in that moment. That design makes the system more predictable and trustworthy: context lives in the files themselves, not in an opaque conversation history. Unlike traditional agents that accumulate state and subtly shift behavior over time, llmLibrarian resets on every query—so answers stay grounded in evidence, reproducible, and free from unintended carryover.

---

## Quick start

```bash
uv venv && source .venv/bin/activate
uv sync
ollama pull llama3.1:8b

# Option A: pal (orchestrates llmli; state in ~/.pal/registry.json)
pal pull /path/to/folder
pal ask "what did I write about X?"
pal ls

# Option B: llmli directly
llmli add /path/to/folder
llmli ask "what is in my docs?"
llmli ask --in tax "what forms?"
llmli ls
```

**Scoping:** Default ask searches all indexed silos. Use `--in <silo>` to limit to one folder (e.g. `--in tax`). Use `--unified` to explicitly search everything (overrides `--in` if both given). Answers are deterministic for the same DB and query (temperature=0, seed=42).

---

## Commands

**pal** — Daily workflow; delegates to llmli.

| Command | Description |
|--------|-------------|
| `pal pull` | Update changed files across all registered folders. |
| `pal pull <path>` | Pull one folder into memory and register it in ~/.pal. |
| `pal pull <path> --watch` | Keep one folder in sync while you work. |
| `pal pull <path> --prompt "..."` | Set a per-silo system prompt override in the llmli silo registry. |
| `pal pull <path> --clear-prompt` | Clear that per-silo prompt override. |
| `pal ask ["question"]` | Ask across all silos; use `--in <silo>` to scope. |
| `pal ls` | List silos (path, files, chunks). |
| `pal inspect <silo>` | Per-silo details and per-file chunk counts. |
| `pal capabilities` | Supported file types and extractors. |
| `pal log` | Last add failures. |
| `pal ensure-self` | Ensure dev-mode self-silo exists (manual refresh). |
| `pal add <path>` | Compatibility alias for `pal pull <path>` (hidden in default help). |
| `pal tool llmli <args...>` | Passthrough to llmli. |

**llmli** — Full control.

| Command | Description |
|--------|-------------|
| `add <path>` | Index folder (silo = basename). Cloud paths blocked by default. |
| `ask [--in \<silo\> \| --unified \| --archetype \<id\>] <query...>` | Query. Default: all silos. `--in` = one silo; `--archetype` = archetype collection from archetypes.yaml. |
| `ls` | List silos. |
| `inspect <silo>` | Silo details and top files by chunk count. |
| `capabilities` | Supported file types (source of truth). |
| `index --archetype <id>` | Rebuild archetype from archetypes.yaml. |
| `rm <silo>` | Remove silo and its chunks. |
| `log [--last]` | Add failures. |
| `eval-adversarial [--out <json>] [--limit N]` | Run synthetic adversarial trustfulness eval with machine-readable report. |

---

## Prompt precedence for `pal ask --in <silo>`

For silo-scoped unified asks, prompt resolution order is:
1. Per-silo override from `pal pull <path> --prompt ...`
2. Archetype prompt from `archetypes.yaml` (exact slug, then base slug without hash suffix, then normalized display name)
3. Built-in default prompt

Use `pal pull <path> --clear-prompt` to revert to archetype/default behavior.

---

## Trying it

- Ask in natural language across silos (“what did I decide about X?”, “where did I list the 2022 classes?”). Sources link to the file; click to open.
- Scope with `--in` when you know the folder (“pal ask --in school …”).
- Ask about a tool or project by name (e.g. “why is the scribe tool fast”) without `--in`; retrieval scopes to paths containing that name when possible.
- After pulling a folder, ask about something only in it; use `pal ls` and `pal log` to see state and failures.

---

## Self-silo (dev mode)

When running `pal` inside this repo, it uses a dev-only `__self__` silo (display name `self`) so capability-related answers stay grounded in local code. `pal ask` and `pal capabilities` do not reindex automatically; they only warn if the self-silo is missing or stale. Use `pal ensure-self` to index or refresh it.

If the repo changes since the last self index, `pal` prints:

`Self-silo stale (repo changed since last index). Run pal ensure-self.`

Auto-reindex (folder): `pal pull <path> --watch` uses a watchdog observer plus a periodic missed-change check (default 10s) to update changed files only. Logs are quiet by design: one startup line and one line per update/remove/check summary.

Legacy compatibility: `pal watch-self` is available but hidden from default help.

---

## Tests

- `uv run pytest -q`
- `python -m pytest -q`

Adversarial trust eval:
- `uv run llmli eval-adversarial --out ./adversarial_eval_report.json`

---

## Env (optional)

Common: `LLMLIBRARIAN_DB` (default `./my_brain_db`), `LLMLIBRARIAN_MODEL` (default `llama3.1:8b`), `LLMLIBRARIAN_TRACE` (file path for per-ask JSON trace + retrieval receipt). Full list, chunking, scaling, and interrupt behavior: **[docs/TECH.md](docs/TECH.md)**.
