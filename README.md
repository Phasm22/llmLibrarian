# llmLibrarian

Local-first search and Q&A over folders you choose to index. Data stays on disk; you pick what becomes a **silo** (one indexed folder per silo).

This repo is built as **personal infrastructure**: opinionated defaults, my workflows first, and a path for others who want the same shape of tool. It is not positioned as a generic product for every team or OS.

**Primary job:** give an LLM **grounded retrieval** over *your* files (chunk text, scores, sources, silo scope)—via the **MCP server** and the same engine the CLI calls. The human shell is mainly **how you build and maintain** that index (pull folders, repair Chroma, inspect coverage), not the only reason the project exists.

- `**pal`** — operator CLI for ingest, health, and occasional direct `ask` when you are not going through an assistant.
- `**llmli`** — engine CLI for scripting, automation, and low-level debugging.

---

## Quick start

```bash
uv venv
source .venv/bin/activate
uv sync
ollama pull llama3.1:8b
```

## Secrets / environment variables

Do **not** keep API keys in a repo-local `.env` (easy to leak via backups, screen sharing, or accidental copies).

Preferred order (`pal` / `llmli` / `mcp_server.py` all call the same bootstrap):

1. **Explicit file**: set `LLMLIBRARIAN_ENV_FILE=/path/to/.llmlibrarian.env` (recommended for systemd units).
  - If this variable is set but the file **does not exist**, bootstrap falls through to (2) and then (3).
2. **XDG config file**: create `~/.config/llmLibrarian/.llmlibrarian.env` (or `$XDG_CONFIG_HOME/llmLibrarian/.llmlibrarian.env`) with `chmod 600`.
3. **Legacy user config**: `~/.config/llmLibrarian/llmlibrarian.env` is still read for compatibility, but new installs use the hidden filename.
4. **Legacy dev only**: repo-root `.env` is supported **only** when `LLMLIBRARIAN_DOTENV=1`.

Example user config file:

```bash
install -d -m 700 ~/.config/llmLibrarian
${EDITOR:-nano} ~/.config/llmLibrarian/.llmlibrarian.env
chmod 600 ~/.config/llmLibrarian/.llmlibrarian.env
```

Optional vision model for image-heavy silos:

```bash
export LLMLIBRARIAN_VISION_MODEL=qwen2.5vl:7b
```

---

## Telescope vs pinpoint (query width)

Retrieval behaves differently when you ask **wide** questions (survey the corpus) vs **narrow** ones (one fact, one file, one identifier). Below: same tool, different scopes.

### Wide (telescope) — “what’s going on here?”

Loose language, big picture, often **unified** across silos or a whole folder.

```bash
# First time: what did I even put in this silo?
pal ask --in <silo> "what kinds of documents are in here?"

# Patterns across everything you indexed
pal ask --unified "what themes show up in more than one place?"

# Vague but useful for exploration (still scoped to one silo)
pal ask --in <silo> "what was I preoccupied with last year?"
```

### Medium — scoped but still interpretive

You name a silo (or topic) and want synthesis, not a single cell from a spreadsheet.

```bash
pal ask --in <silo> "summarize decisions about the API redesign"
pal ask --unified "compare how I describe project A vs project B"
```

### Narrow (pinpoint) — one place, one answer

Tight language helps: filenames, years, IDs, “where does it say…”. Prefer `**--in**` when you know the silo.

```bash
# Silo known: reduce noise
pal ask --in <silo> "where is chroma_shared_lock used?"

# Exact-ish lookup style
pal ask --in <silo> "what line or box reports total tax for 2023?"

# Shorthand (normalized to --in)
pal ask in <silo> "W-2 employer name"
```

**Rule of thumb:** if the answer should cite **one** chunk, ask like a librarian (specific noun + silo). If you want **overview**, ask like a reviewer and expect more breadth and more hedging.

---

## Typical flow

```bash
pal pull /path/to/folder
pal ls
pal ask --in <silo> "pinpoint or telescope question from above"
pal inspect <silo> --top 20
```

Unified query (no `--in`):

```bash
pal ask --unified "telescope-style question only when you mean all silos"
```

---

## What gets indexed

Supported types include common text/code, PDF, DOCX, XLSX, PPTX, ZIPs of those, and optional image/vision paths. See:

```bash
pal capabilities
# or
llmli capabilities
```

Cloud-sync roots (OneDrive, iCloud, Dropbox, etc.) are **blocked by default**; use `--allow-cloud` only when paths are really local and stable.

---

## Maintenance and health

```bash
pal ls --status
pal ls --jobs
pal pull --status
llmli log --last
```

Chroma or registry inconsistency (e.g. `Error finding id`, empty silo after a crash): repair re-wipes that silo’s vectors and re-indexes from disk:

```bash
llmli repair <silo>
# Read-only L2 diagnostics (sqlite integrity + segment scan + repair ladder)
llmli repair-ladder

# L3 helper: rehydrate from llmli_registry into the current DB path
llmli rehydrate --dry-run
llmli rehydrate
```

---

## Images and OCR

- PDFs: text extraction first; OCR fallback chain depends on OS (macOS Vision where available; else PaddleOCR / tesseract).
- Per-silo multimodal summaries: `pal pull <path> --image-vision` (requires a vision-capable `LLMLIBRARIAN_VISION_MODEL`).
- Image embedding extras: install with your chosen optional deps / `uv sync` as documented in `pyproject.toml`.

---

## Troubleshooting

**Low confidence / sparse context**  
Narrow the question, add `--in <silo>`, or improve source files. Retrieval is only as good as what was indexed.

`**InternalError: Error finding id`**  
Chroma index inconsistency; run `llmli repair <silo>` (full re-index from source folder).

`**no extractable text` (PDF)**  
Likely scan-only PDF or missing OCR. On macOS, `swiftc` for Vision path; otherwise install OCR optional deps.

**Wrong binary / old checkout**

```bash
which pal
which llmli
uv run python pal.py --help
uv run python cli.py --help
```

---

## Shared MCP server for `pal pull --watch`

`pal pull --watch` requires the shared HTTP MCP server to be running and routes
all per-file writes through it. This enforces ChromaDB's one-`PersistentClient`-per-DB
invariant and prevents HNSW corruption when multiple watchers run alongside other
write paths.

If the server is not reachable, `pal pull --watch` exits with code 2 and prints a
clear error. There is no autostart and no fallback.

**Environment** (read by both server and watcher):
- `LLMLIBRARIAN_MCP_URL` (default `http://127.0.0.1:8765/mcp`)
- `LLMLIBRARIAN_MCP_BEARER_TOKEN` (optional; required if the server enforces auth)

Write-tool note: MCP `add_silo` supports `confirm` and defaults to `true`. Keep
`confirm=true` when calling write tools from external clients to preserve explicit intent.

**Install as a user-level systemd service** (`~/.config/systemd/user/llmlibrarian-mcp.service`):

```ini
[Unit]
Description=llmLibrarian shared MCP server
After=network.target

[Service]
Environment=LLMLIBRARIAN_MCP_TRANSPORT=streamable-http
Environment=LLMLIBRARIAN_MCP_HOST=127.0.0.1
Environment=LLMLIBRARIAN_MCP_PORT=8765
Environment=LLMLIBRARIAN_MCP_BEARER_TOKEN=<openssl rand -hex 24>
Environment=LLMLIBRARIAN_DB=%h/Desktop/llmLibrarian/my_brain_db
ExecStart=%h/Desktop/llmLibrarian/.venv/bin/python %h/Desktop/llmLibrarian/mcp_server.py
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now llmlibrarian-mcp.service
curl http://127.0.0.1:8765/healthz   # {"ok":true,"service":"llmLibrarian-mcp","version":"...","db_exists":true,"started_at":"..."}
```

One-shot writes (`pal pull <path>` without `--watch`, `llmli add`, `llmli repair`)
keep their direct-write path and serialize via flock — they do not require the
shared server.

---

## Artifact + context controls

Optional artifact compilation is post-ingest and additive (raw chunks remain):

- `LLMLIBRARIAN_ARTIFACT_SILOS` — comma list of parent silos, or `*` to enable for all
- `LLMLIBRARIAN_ARTIFACT_MAX_FACTS` — cap extracted artifact rows per compile
- `LLMLIBRARIAN_ARTIFACT_MAX_INPUT_CHARS` — cap parent chunk text scanned during compile

Ask-time context budget:

- `LLMLIBRARIAN_CONTEXT_BUDGET_TOKENS` — global budget (approx chars/4). On overflow,
  low-ranked chunks are dropped; if none fit, ask returns a deterministic budget warning.

---

## Further reading

- Runtime and architecture: `[docs/TECH.md](docs/TECH.md)`
- Security and testing notes: `[SECURITY_AND_TESTING.md](SECURITY_AND_TESTING.md)`
- MCP over HTTPS via Tailscale Funnel: `[docs/MCP_TAILSCALE_FUNNEL.md](docs/MCP_TAILSCALE_FUNNEL.md)`
- Agent and contributor contracts: `[AGENTS.md](AGENTS.md)`

