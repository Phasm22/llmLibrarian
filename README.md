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

1. **Explicit file**: set `LLMLIBRARIAN_ENV_FILE=/path/to/llmlibrarian.env` (recommended for systemd units).
   - If this variable is set but the file **does not exist**, bootstrap falls through to (2) and then (3).
2. **XDG config file**: create `~/.config/llmLibrarian/llmlibrarian.env` (or `$XDG_CONFIG_HOME/llmLibrarian/llmlibrarian.env`) with `chmod 600`.
3. **Legacy dev only**: repo-root `.env` is supported **only** when `LLMLIBRARIAN_DOTENV=1`.

Example user config file:

```bash
install -d -m 700 ~/.config/llmLibrarian
${EDITOR:-nano} ~/.config/llmLibrarian/llmlibrarian.env
chmod 600 ~/.config/llmLibrarian/llmlibrarian.env
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
pal status
pal pull --status
llmli log --last
```

Chroma or registry inconsistency (e.g. `Error finding id`, empty silo after a crash): repair re-wipes that silo’s vectors and re-indexes from disk:

```bash
llmli repair <silo>
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

## Further reading

- Runtime and architecture: `[docs/TECH.md](docs/TECH.md)`
- Security and testing notes: `[SECURITY_AND_TESTING.md](SECURITY_AND_TESTING.md)`
- Agent and contributor contracts: `[AGENTS.md](AGENTS.md)`

