# Development Roadmap

pal is optimized for: **local-first, predictable, low-friction retrieval + Q&A**.
The next gains come from better retrieval, stronger guardrails, cleaner UX, and optional domain packs — not more agent complexity.

---

## Current Open Items (2026-02-11)

These are the roadmap items that are still open after the latest implementation pass.

### Open Implementation Work
- `1a` BM25 lexical fallback for weak semantic matches (general retrieval path).
- `1d` Semantic boundary-aware chunking (avoid mid-function / mid-JSON splits).
- `2a` Source-grounded inline citations (`[N]`) with validation/post-check.
- `2c` Generic structured field extraction beyond tax-specific form/line logic.

### Open Product/Design Decisions
- None currently open. (`4d` resolved to hybrid override + config fallback.)

Reference: `docs/IMPLEMENTATION_STATUS_AND_FIT_GAP_REPORT.md`

---

## 1. Retrieval Quality

### 1a. BM25 lexical fallback

Vector search fails on exact terms (account numbers, error codes, proper nouns). Add BM25 as a fallback when vector distance is poor.

Status update (2026-02-11):
- Phase 1 shipped for direct intents behind config flag `query.direct_decisive_mode`.
- Implemented: direct lexical fallback terms + RRF merge + canonical/deprioritized source weighting + canonical-aware confidence relaxation.
- Implemented validation: adversarial eval now records `strict_mode` and `direct_decisive_mode`, and supports strict/direct A/B controls.
- Still open for full closure: apply the same weak-distance lexical fallback pattern across broader non-direct retrieval paths (current implementation is targeted, not universal BM25).

```python
# src/query/retrieval.py

def _bm25_search(collection, query: str, silo: str | None, n: int) -> list[dict]:
    """Keyword search over chunk text. Used when vector similarity is weak."""
    all_docs = collection.get(
        where={"silo": silo} if silo else None,
        include=["documents", "metadatas"],
    )
    from math import log
    terms = query.lower().split()
    scores = []
    for i, doc in enumerate(all_docs["documents"] or []):
        doc_lower = doc.lower()
        tf = sum(doc_lower.count(t) for t in terms)
        if tf > 0:
            scores.append((tf, i))
    scores.sort(reverse=True)
    return [
        {"id": all_docs["ids"][i], "document": all_docs["documents"][i],
         "metadata": all_docs["metadatas"][i], "distance": 1.0 / (1 + tf)}
        for tf, i in scores[:n]
    ]


def retrieve(collection, query, silo, n, intent, ...):
    vector_results = _vector_search(collection, query, silo, n)
    # If top result is weak, blend in lexical
    if vector_results and vector_results[0]["distance"] > 0.8:
        lexical = _bm25_search(collection, query, silo, n)
        return _rrf_merge(vector_results, lexical, k=60)[:n]
    return vector_results
```

### 1b. Per-file chunk cap tuning by intent

EVIDENCE_PROFILE needs breadth (many sources, few chunks each). AGGREGATE needs depth (more chunks per file for synthesis).

```python
# src/query/retrieval.py

DIVERSITY_CAPS = {
    "LOOKUP": 3,
    "EVIDENCE_PROFILE": 2,
    "AGGREGATE": 6,      # need more chunks per doc to synthesize
    "REFLECT": 4,
    "FIELD_LOOKUP": 8,   # want full form pages
}

def _apply_diversity(results, intent):
    cap = DIVERSITY_CAPS.get(intent, 3)
    by_source = {}
    out = []
    for r in results:
        src = r["metadata"].get("source", "")
        by_source[src] = by_source.get(src, 0) + 1
        if by_source[src] <= cap:
            out.append(r)
    return out
```

### 1c. Query expansion for common domains

```python
# src/query/expansion.py

SYNONYMS = {
    "income": ["wages", "salary", "compensation", "earnings", "gross income"],
    "address": ["street", "mailing address", "residence"],
    "phone": ["telephone", "cell", "mobile", "contact number"],
}

def expand_query(query: str) -> str:
    """Append synonym terms to improve recall. Keeps original query intact."""
    tokens = query.lower().split()
    extras = []
    for t in tokens:
        if t in SYNONYMS:
            extras.extend(SYNONYMS[t])
    if extras:
        return query + " " + " ".join(extras)
    return query
```

### 1d. Semantic chunking (respect boundaries)

Current chunking splits mid-function, mid-JSON. Detect boundaries and avoid breaking them.

```python
# src/ingest.py — enhance chunk_text()

BOUNDARY_PATTERNS = [
    r"^#{1,3} ",           # markdown headings
    r"^def ",              # python functions
    r"^class ",            # python classes
    r"^function ",         # javascript functions
    r"^---$",              # markdown horizontal rules / frontmatter
    r"^\},?\s*$",          # end of JSON/code blocks
]

def _is_boundary(line: str) -> bool:
    return any(re.match(p, line) for p in BOUNDARY_PATTERNS)

def chunk_text_semantic(text, size=1000, overlap=100):
    """Line-aware chunking that prefers splitting at semantic boundaries."""
    lines = text.split("\n")
    chunks = []
    current = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        # If adding this line exceeds size AND we can split at a boundary
        if current_len + line_len > size and current_len > size * 0.5:
            if _is_boundary(line) or current_len >= size:
                chunks.append("\n".join(current))
                # Keep overlap from end
                overlap_lines = []
                overlap_len = 0
                for ol in reversed(current):
                    overlap_len += len(ol) + 1
                    if overlap_len > overlap:
                        break
                    overlap_lines.insert(0, ol)
                current = overlap_lines
                current_len = sum(len(l) + 1 for l in current)
        current.append(line)
        current_len += line_len

    if current:
        chunks.append("\n".join(current))
    return chunks
```

---

## 2. Guardrails & Citations

### 2a. Source-grounded citations

Tag each sentence in the LLM answer with which chunk(s) support it. Post-process the answer to add inline citations.

```python
# src/query/citations.py

def build_citation_prompt(context_blocks: list[dict]) -> str:
    """Instruct the LLM to cite sources inline."""
    return (
        "When you make a factual claim, cite the source using [N] where N is "
        "the context block number. Example: 'Total income was $85,000 [3].'\n"
        "If no context supports a claim, say 'not found in indexed data'."
    )

def validate_citations(answer: str, n_sources: int) -> list[str]:
    """Check that cited numbers are valid source indices."""
    import re
    cited = [int(m) for m in re.findall(r"\[(\d+)\]", answer)]
    warnings = []
    for c in cited:
        if c < 1 or c > n_sources:
            warnings.append(f"Citation [{c}] references non-existent source")
    return warnings
```

### 2b. Confidence signaling

When retrieval quality is low (high distance, few matches), prepend a confidence warning.

```python
# src/query/core.py — add to run_ask()

def _confidence_signal(results: list[dict], intent: str) -> str | None:
    if not results:
        return "No matching content found in indexed data."
    avg_distance = sum(r["distance"] for r in results) / len(results)
    unique_sources = len({r["metadata"].get("source") for r in results})
    if avg_distance > 0.7:
        return "Low confidence: query is weakly related to indexed content."
    if unique_sources == 1 and intent != "FIELD_LOOKUP":
        return "Single source: answer is based on one document only."
    return None
```

### 2c. Generic structured field extraction

Extend guardrails beyond tax. Any silo with structured docs can use field lookup.

```python
# src/query/guardrails.py

def extract_field_value(chunks: list[dict], field_pattern: str) -> str | None:
    """Search chunks for a labeled field value. Works for any form/table."""
    import re
    for chunk in chunks:
        text = chunk.get("document", "")
        # Match patterns like "Field Name: Value" or "Field Name | Value"
        pattern = rf"(?i){re.escape(field_pattern)}\s*[:\|]\s*(.+?)(?:\n|$)"
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    return None
```

---

## 3. UX & Workflows

### 3a. `pal ask --show-sources` (opt-in verbose sources)

Default: answer only. Flag to show full source list with relevance + snippets.

```python
# pal.py — add to ask_command

@app.command("ask", help="Ask a question across your indexed data.")
def ask_command(
    query: list[str] = typer.Argument(..., metavar="QUESTION"),
    in_silo: str | None = typer.Option(None, "--in", help="Query only this silo."),
    show_sources: bool = typer.Option(False, "--show-sources", help="Show source chunks and relevance."),
    strict: bool = typer.Option(False, "--strict", help="Only answer when evidence is strong."),
):
    ...
```

### 3b. `pal diff <silo>` — show what changed since last pull

```python
# pal.py

@app.command("diff", help="Show files changed since last pull.")
def diff_command(
    silo: str = typer.Argument(..., help="Silo to check."),
) -> None:
    _ensure_src_on_path()
    from file_registry import _read_file_manifest
    db_path = os.environ.get("LLMLIBRARIAN_DB", "./my_brain_db")
    manifest = _read_file_manifest(db_path)
    silo_data = (manifest.get("silos") or {}).get(silo, {})
    files = silo_data.get("files") or {}
    changed, added, removed = [], [], []
    for path_str, meta in files.items():
        p = Path(path_str)
        if not p.exists():
            removed.append(path_str)
        else:
            stat = p.stat()
            if stat.st_mtime != meta.get("mtime") or stat.st_size != meta.get("size"):
                changed.append(path_str)
    # Check for new files not in manifest
    # (would need collect_files from ingest)
    if not changed and not removed:
        print("No changes.")
        return
    for f in changed:
        print(f"  M {f}")
    for f in removed:
        print(f"  D {f}")
```

### 3c. `pal status` — one-line health summary

```python
@app.command("status", help="Quick health check.")
def status_command() -> None:
    _ensure_src_on_path()
    from silo_audit import load_registry, load_manifest, find_count_mismatches
    db_path = os.environ.get("LLMLIBRARIAN_DB", "./my_brain_db")
    registry = load_registry(db_path)
    manifest = load_manifest(db_path)
    mismatches = find_count_mismatches(registry, manifest)
    total_silos = len(registry)
    total_chunks = sum(int(s.get("chunks_count", 0) or 0) for s in registry)
    stale = len(mismatches)
    if stale:
        print(f"{total_silos} silos, {total_chunks} chunks, {stale} stale")
    else:
        print(f"{total_silos} silos, {total_chunks} chunks, all current")
```

### 3d. Quiet mode for scripting

`pal ask --quiet "question"` — answer only, no warnings, no color. Useful for piping.

```python
# Already partially supported via LLMLIBRARIAN_QUIET env var.
# Add --quiet flag to ask:
quiet: bool = typer.Option(False, "--quiet", "-q", help="Answer only, no warnings.")
```

---

## 4. Domain Packs (Optional, Not Core)

Domain packs are archetype configs + optional guardrails. They don't add core complexity — they're YAML + a few regex patterns.

### 4a. Legal pack

```yaml
# archetypes.yaml
legal:
  prompt: |
    You are a legal document assistant. Answer factual questions about
    contracts, agreements, and legal filings. Always cite section numbers.
    Never give legal advice. Say "consult an attorney" for interpretation.
  include: ["*.pdf", "*.docx"]
  exclude: ["*draft*", "*template*"]
```

### 4b. Medical/health pack

```yaml
health:
  prompt: |
    You are a health records assistant. Answer questions about lab results,
    visit summaries, and prescriptions using only indexed documents.
    Never diagnose or recommend treatment. Always include dates and values.
  include: ["*.pdf", "*.docx", "*.txt"]
```

### 4c. Code review pack

```yaml
codebase:
  prompt: |
    You are a code context engine. Answer questions about this codebase
    using only indexed source files. Prefer README and doc files for
    architectural questions. Include file paths and line numbers.
  include: ["*.py", "*.ts", "*.js", "*.go", "*.rs", "*.md", "*.yaml"]
  exclude: ["node_modules/*", ".git/*", "__pycache__/*", "*.min.js"]
```

### 4d. Per-silo prompt overrides via `pal pull --prompt`

Implemented (hybrid):
- `pal pull <path> --prompt "..."` sets `prompt_override` on the silo in `llmli_registry.json`
- `pal pull <path> --clear-prompt` clears it
- For `pal ask --in <silo>`, prompt precedence is:
1. per-silo registry override
2. archetype prompt (`archetypes.yaml`) by exact slug, then base slug without hash suffix, then normalized display name
3. built-in default prompt

---

## Priority Order

| Phase | Area | Effort | Impact |
|-------|------|--------|--------|
| 6 | Per-intent diversity caps | Small | Medium — better AGGREGATE answers |
| 4 | Semantic chunking | Medium | High — better retrieval for code/structured docs |
| 9 | Generic field extraction | Medium | Medium — extends guardrails beyond tax |
| 10 | `--show-sources`, `--quiet` | Small | Low — polish |
| 5 | `pal diff`, `pal status` | Small | Medium — workflow friction |
| 2 | Inline citations `[N]` | Small | High — trust + verifiability |
| 8 | Query expansion | Medium | Medium — better recall for synonyms |
| 3 | Confidence signals | Small | Medium — user knows when to doubt |
| 1 | BM25 lexical fallback | Small | High — fixes exact-term misses |
| 7 | Domain packs (YAML only) | Small | Low-medium — expands use cases |
