# llmLibrarian

Local-first search + reasoning over files you choose to index.

`pal` is the operator-facing CLI for indexing, asking, and keeping the index healthy.
`llmli` is the direct engine CLI for scripting and lower-level automation.

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

## Typical Flow

```bash
pal pull /path/to/folder
pal ask --in <silo> "what is this folder mostly about?"
pal ask --unified "what themes repeat across my indexed folders?"
pal inspect <silo>
```

Natural shorthand is supported:

```bash
pal ask in <silo> "what files are from 2022"
```

## What It Is For

Use it to turn a folder into a searchable silo, then ask grounded questions about that silo or across all silos.

`pal pull` handles ingest and refresh. Cloud-sync folders are blocked by default; use `--allow-cloud` only if the files are fully local and pinned.

`pal capabilities`, `pal log`, `pal status`, and `pal diff` are the main maintenance views when you need to check what changed, what is supported, or whether an index is drifting.

`pal ask` searches indexed data and cites sources so you can verify the answer.

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
