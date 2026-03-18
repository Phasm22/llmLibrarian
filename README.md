# llmLibrarian

Local-first search + reasoning over files you choose to index.

`pal` is the operator-facing CLI. `llmli` is the direct engine CLI.

## Quick Start

```bash
uv venv
source .venv/bin/activate
uv sync
ollama pull llama3.1:8b
```

If you want multimodal image summaries, also set a vision-capable Ollama model:

```bash
export LLMLIBRARIAN_VISION_MODEL=qwen2.5vl:7b
```

## Core Flow

```bash
pal pull /path/to/folder
pal pull /path/to/photos --image-vision
pal pull /path/to/folder --workers 12 --embedding-workers 4
pal ls
pal ask --in <silo> "what is this folder mostly about?"
pal ask --unified "what themes repeat across my indexed folders?"
pal inspect <silo> --top 5
```

Natural shorthand is supported:

```bash
pal ask in <silo> "what files are from 2022"
```

## What `pal pull` Does

- Indexes supported files in the folder.
- Registers the folder as a silo.
- Re-pulls changed files on later runs.
- Prints preflight counts and image progress for image-heavy folders.
- Accepts `--image-vision` per silo.
- Accepts `--workers` and `--embedding-workers` per run.

Cloud-sync folders are blocked by default. Use `--allow-cloud` only if the files are fully local and pinned.

## What `pal ask` Does

- Default ask searches indexed silos.
- `--in <silo>` scopes to one silo.
- `--unified` searches across silos.
- Deterministic structure asks use manifest/registry data, not embedding retrieval.
- Answers cite sources so you can verify them.

## Images and OCR

- PDFs use normal text extraction first.
- On macOS, OCR fallback order is Vision, then PaddleOCR, then `tesseract`.
- On other platforms, OCR fallback order is PaddleOCR, then `tesseract`.
- Multimodal image summaries are off by default. Enable them per silo with `pal pull <path> --image-vision`.
- Standalone images (`.png`, `.jpg`, `.jpeg`, `.heic`, `.heif`, `.tif`, `.tiff`) index as:
  - one `image_summary` chunk
  - zero or more `image_region` chunks when OCR finds useful text
  - one image-vector row in a sibling image collection
- If image vision is enabled for a silo, natural-photo images are usually deferred at ingest and the first matching query may lazily summarize one deferred image and cache it.
- Low-signal OCR gibberish is dropped instead of indexed.

If standalone images are present:
- `LLMLIBRARIAN_VISION_MODEL` is required only when `--image-vision` is enabled.
- OpenCLIP image embedding dependencies must be installed (`uv sync`).

## Common Commands

| Command | Purpose |
|---|---|
| `pal pull <path>` | Index one folder and register/update its silo. |
| `pal pull --status` | Show watcher state. |
| `pal pull --stop <target>` | Stop a watcher. |
| `pal ask --in <silo> "..."` | Ask inside one silo. |
| `pal ask --unified "..."` | Ask across silos. |
| `pal ls` | List silos. |
| `pal inspect <silo>` | Show silo details and top files. |
| `pal capabilities` | Show supported file types/extractors. |
| `pal log` | Show recent indexing failures. |
| `pal sync` | Refresh the repo self-silo in dev mode. |

## Troubleshooting

`Low confidence ...`
- Retrieval was weak or mixed. Scope with `--in`, ask a narrower question, or add better source files.

`no extractable text`
- The PDF is scanned/image-only or OCR is unavailable. On macOS, make sure `swiftc` exists. Otherwise install `paddleocr` or `tesseract`.

Standalone image ingest fails
- Set `LLMLIBRARIAN_VISION_MODEL` to a vision-capable Ollama model and run `uv sync`.

Behavior does not match the repo
- Check which binary is running:

```bash
which pal
which llmli
uv run python pal.py --help
uv run python cli.py --help
```

## More Detail

- Technical/runtime contracts: [`docs/TECH.md`](docs/TECH.md)
- Security and test notes: [`SECURITY_AND_TESTING.md`](SECURITY_AND_TESTING.md)
