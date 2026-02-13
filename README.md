# llmLibrarian (llmli)

llmLibrarian helps you ask real questions about your own files and get answers you can verify.

It is a local, file-first AI assistant that reasons only over documents you choose to index: notes, PDFs, slides, code, research, and archives. There is no hidden chat memory, no long-running persona, and no context carryover between questions.

Think of it as search + reasoning for your personal archive.

## What Makes It Different

1. Your files are the context.
llmLibrarian does not try to know everything. It only uses indexed files, then cites paths and locations so you can verify the answer.

2. Stateless by design.
Each ask starts fresh. No hidden memory from prior prompts, no behavior drift, no opaque conversation state.

3. Deterministic where it matters.
Scope selection, guardrails, and retrieval are handled before generation. If a query can be answered deterministically, it avoids free-form guessing.

4. Built for fragmented real-life data.
It handles messy folders, mixed formats, duplicates, and time-bound questions like “what files are from 2022?” or “in my stuff.”

## Good For / Not Trying To Be

Good for:
- Reasoning over personal archives and project folders
- Auditing what you saved and where
- Recovering specific facts from old notes, slides, and docs
- Treating files as memory, not just storage

Not trying to be:
- A general-purpose internet chatbot
- A cloud knowledge engine
- A persona assistant that “learns you” over time

## Design Philosophy

- Local-first
- User-curated context
- Stateless answers
- Observable behavior
- Trust over cleverness

## Quick Start

Setup (copy/paste):

```bash
uv venv
source .venv/bin/activate
uv sync
ollama pull llama3.1:8b
```

Example flows:
These are intentionally broad to show how the tool scales from single-folder lookups to cross-silo synthesis over large, scattered personal archives.

```bash
# Option A: pal (recommended day-to-day, multi-silo)
pal pull ~/Documents/School
pal pull ~/Documents/Taxes
pal pull ~/Documents/WorkNotes
pal pull ~/Desktop/Stuff
pal ls

# Silo-focused retrieval
pal ask --in school "what classes did I take in 2022 and which files support that?"
pal ask --in taxes "list every 1099 form mentioned in my 2023 tax files"
pal ask --in stuff --quiet "what files are from 2022"
pal ask --in stuff --quiet "show structure snapshot"
pal ask --in stuff --quiet "recent changes"
pal ask --in stuff --quiet "file type inventory"

# Cross-silo synthesis (--unified)
pal ask --unified "compare risk themes in my work incident notes and personal security project slides"
pal ask --unified "what decisions did I make about job search, tuition, and relocation across my notes?"
pal ask --unified "create a timeline of major events across school, tax, and work documents"

# Debug deterministic catalog behavior when needed
pal ask --in stuff --explain "what files are from 2022"
pal ask --in stuff --force --quiet "what files are from 2022"

# Option B: llmli directly
llmli add ~/Research
llmli add ~/Archive
llmli ask --in research "which papers discuss retrieval evaluation and contradiction handling?"
llmli ask --unified "synthesize recurring themes across my research notes and archived drafts"
llmli ls
```

Scoping:
- Default ask searches all indexed silos.
- `--in <silo>` limits to one silo.
- `--unified` explicitly searches everything.
- Natural-language scope phrases (for example, “in my stuff”) are best-effort and conservative.
- For structure-style asks without scope, llmLibrarian returns deterministic scope guidance with likely silos instead of guessing.

Help:

```bash
pal -h
pal --help
llmli -h
llmli --help
```

## 30-Second Sanity Check

```bash
pal pull /path/to/folder
pal ask --in <silo> "what is this folder mostly about, and what are the top source files?"
pal inspect <silo> --top 5
```

Expected result:
- `pal pull` indexes files
- `pal ask --in` returns an answer plus source footer
- `pal inspect` shows top files/chunk distribution so you can validate coverage

## Commands

`pal` (recommended):

| Command | Description |
|--------|-------------|
| `pal pull` | Update changed files across registered folders. |
| `pal pull <path>` | Pull one folder and register it in `~/.pal/registry.json`. |
| `pal pull <path> --watch` | Keep one folder in sync while you work. |
| `pal pull --status` | Show watcher locks and process state for all watched silos. |
| `pal pull <path> --status` | Show watcher state for the silo mapped to that path. |
| `pal pull --stop <target>` | Stop watcher by PID, silo slug/display name, or watched path. |
| `pal pull <path> --prompt "..."` | Set per-silo prompt override in llmli registry. |
| `pal pull <path> --clear-prompt` | Clear per-silo prompt override. |
| `pal ask "..."` | Ask across silos; use `--in <silo>` to scope. |
| `pal ask --explain ...` | Print deterministic catalog/scope diagnostics to stderr when applicable. |
| `pal ask --force ...` | Allow deterministic catalog queries on stale scope. |
| `pal ls` | List silos (path, files, chunks). |
| `pal inspect <silo>` | Silo details and per-file chunk counts. |
| `pal capabilities` | Supported file types and extractors. |
| `pal log` | Last add failures. |
| `pal sync` | Re-index repo self-silo in dev mode. |
| `pal tool llmli <args...>` | Passthrough to llmli. |

`llmli` (direct control):

| Command | Description |
|--------|-------------|
| `add <path>` | Index folder (silo = basename). |
| `ask [--in <silo> \| --unified \| --archetype <id>] <query...>` | Query indexed content. |
| `ask --explain ...` | Print deterministic catalog diagnostics to stderr when applicable. |
| `ask --force ...` | Allow deterministic catalog queries to run on stale scope. |
| `ls` | List silos. |
| `inspect <silo>` | Silo details and top files by chunk count. |
| `capabilities` | Supported file types (source of truth). |
| `index --archetype <id>` | Rebuild archetype collection from config. |
| `rm <silo>` | Remove silo and its chunks. |
| `log --last` | Show last indexing failures. |
| `eval-adversarial ...` | Run synthetic trustfulness eval and output JSON report. |

## Prompt Precedence (`pal ask --in <silo>`)

For silo-scoped unified asks:
1. Per-silo override set by `pal pull <path> --prompt ...`
2. Archetype prompt from `archetypes.yaml` (exact slug, then base slug, then normalized display name)
3. Built-in default prompt

Use `pal pull <path> --clear-prompt` to revert.

## Natural Language Scope Notes

- Explicit `--in` always wins.
- Phrase binding like “in my stuff” is conservative and deterministic.
- Ambiguous phrase matches do not auto-bind.
- Structure asks with no clear scope return a deterministic “No scope selected” hint plus likely silos.

## Troubleshooting (Top 5)

1. `Self-silo stale ... Run pal sync`
- Meaning: repo changed since last self index.
- Fix: run `pal sync`.

2. `Low confidence ...`
- Meaning: retrieval match quality is weak or mixed.
- Fix: scope with `--in`, ask a more specific question, or add missing files.

3. watcher suspended after `^Z`
- Meaning: process is stopped but still holds watcher lock.
- Fix:

```bash
pal pull --status
pal pull --stop <silo-or-pid>
```

Watcher locks live at `~/.pal/watch_locks`.

4. `no extractable text` for PDFs
- Meaning: scanned/image PDF or unreadable structure.
- Fix: OCR first, or use text-exportable documents.

5. stale catalog + `--force`
- Meaning: deterministic catalog query detected stale scope.
- Fix: `pal pull <path>` to refresh; use `--force` only if you accept stale results.

6. wrong binary/environment
- Symptom: command behavior doesn’t match repo changes.
- Fix:

```bash
which llmli
which pal
uv run python cli.py --help
uv run python pal.py --help
```

## Need Details?

- Technical/operator reference: [`docs/TECH.md`](docs/TECH.md)
- Security and test coverage: [`SECURITY_AND_TESTING.md`](SECURITY_AND_TESTING.md)
- Adversarial eval smoke:

```bash
uv run llmli eval-adversarial --limit 20 --no-strict-mode --direct-decisive-mode --out ./adversarial_eval_smoke.json
```
