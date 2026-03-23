# llmLibrarian

Local-first search + reasoning over files you choose to index.

Your data stays on your machine. You decide what gets indexed. 
You own the silo.

`pal` is the operator-facing CLI — indexing, querying, and keeping 
the index healthy.
`llmli` is the direct engine CLI for scripting and lower-level automation.

---

## What This Actually Is

llmLibrarian turns folders into searchable silos, then lets you (or an 
AI assistant) ask grounded questions against them — with citations back 
to the source.

The RAG layer is wide by design. It can handle documents, code, journals, 
PDFs, images, tax records, old schoolwork — whatever you choose to pull in.

But the real value isn't retrieval. It's what you can do *with* retrieval:

- A journal silo becomes a thinking partner that remembers everything 
  you've written and reads between your lines
- A project folder becomes an agent that knows your actual codebase, 
  not a hallucinated version of it
- A document archive becomes a research assistant that cites before it claims
- Multiple silos become a unified view across different domains of your life

The tool is wide. What you point it at is up to you.

---

## Quick Start
```bash
uv venv
source .venv/bin/activate
uv sync
ollama pull llama3.1:8b
```

For multimodal image summaries, set a vision-capable Ollama model:
```bash
export LLMLIBRARIAN_VISION_MODEL=qwen2.5vl:7b
```

---

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

---

## Silo Ideas

The tool is only as useful as what you put in it. Some starting points:

**Personal**
- Daily journal → reflective AI conversations grounded in your actual 
  writing, not generic advice
- Notes & ideas → surface patterns and connections you forgot you made
- Health/habit logs → correlate energy, sleep, and output over time

**Professional**
- Codebase or project folder → grounded code assistance without hallucination
- Work documents → ask questions across meeting notes, specs, and 
  decisions without digging manually
- Research archive → cited answers from your own collected sources

**Multi-silo**
- Index multiple folders and use `--unified` to ask questions that 
  cut across all of them
- Each silo can feed a dedicated AI conversation with its own lens — 
  same data, different angle of inquiry

---

## What It Is For

`pal pull` handles ingest and refresh. Cloud-sync folders are blocked 
by default; use `--allow-cloud` only if files are fully local and pinned.

`pal ask` searches indexed data and cites sources so you can verify 
the answer.

`pal capabilities`, `pal log`, `pal status`, and `pal diff` are the 
main maintenance views — check what changed, what's supported, or 
whether an index is drifting.

---

## Images and OCR

- PDFs use normal text extraction first.
- On macOS, OCR fallback order is Vision → PaddleOCR → tesseract.
- On other platforms: PaddleOCR → tesseract.
- Multimodal image summaries are off by default. Enable per silo 
  with `pal pull <path> --image-vision`.
- Standalone images index as one `image_summary` chunk, zero or more 
  `image_region` chunks when OCR finds useful text, and one image-vector 
  row in a sibling image collection.
- Low-signal OCR gibberish is dropped, not indexed.

If standalone images are present:
- `LLMLIBRARIAN_VISION_MODEL` required only when `--image-vision` enabled
- OpenCLIP image embedding dependencies must be installed (`uv sync`)

---

## Troubleshooting

`Low confidence ...`
Retrieval was weak or mixed. Scope with `--in`, ask a narrower 
question, or add better source files.

`no extractable text`
The PDF is scanned/image-only or OCR is unavailable. On macOS, 
confirm `swiftc` exists. Otherwise install `paddleocr` or `tesseract`.

Standalone image ingest fails
Set `LLMLIBRARIAN_VISION_MODEL` to a vision-capable Ollama model 
and run `uv sync`.

Behavior does not match the repo
Check which binary is running:
```bash
which pal
which llmli
uv run python pal.py --help
uv run python cli.py --help
```

---

## More Detail

- Technical/runtime contracts: [`docs/TECH.md`](docs/TECH.md)
- Security and test notes: [`SECURITY_AND_TESTING.md`](SECURITY_AND_TESTING.md)
