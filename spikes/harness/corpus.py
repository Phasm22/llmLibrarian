"""Build privacy-safe manifests and local fixtures from the production index.

The sampled text is written below ``spikes/private`` (gitignored).  The public
manifest contains only aggregate statistics and content digests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import chromadb
from transformers import AutoTokenizer


ROOT = Path(__file__).resolve().parents[2]
PRIVATE_ROOT = ROOT / "spikes" / "private" / "s0"
DEFAULT_MANIFEST = ROOT / "spikes" / "harness" / "fixtures-manifest.json"
MODEL = "sentence-transformers/all-mpnet-base-v2"
TOKEN_CEILING = 384
HISTOGRAM_EDGES = (32, 64, 96, 128, 192, 256, 320, 383)
CODE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".css", ".go", ".h", ".hpp", ".html", ".java",
    ".js", ".jsx", ".kt", ".lua", ".m", ".mdx", ".php", ".py", ".rb",
    ".rs", ".scala", ".sh", ".sql", ".swift", ".ts", ".tsx", ".vue",
}
TRANSCRIPT_MARKERS = ("transcript", "meeting notes", "speaker 1", "speaker:")


@dataclass(frozen=True)
class Chunk:
    id: str
    text: str
    metadata: dict[str, Any]
    token_count: int
    shape: str


def classify_shape(metadata: dict[str, Any], text: str) -> str:
    """Assign one mutually-exclusive workload shape, most-specific first."""
    source = str(metadata.get("source_path") or metadata.get("source") or "")
    suffix = Path(source).suffix.lower()
    doc_type = str(metadata.get("doc_type") or "").lower()
    silo = str(metadata.get("silo") or "").lower()
    haystack = f"{Path(source).name.lower()} {text[:500].lower()}"
    if doc_type in {"tax", "tax_return", "financial"} or "tax" in silo:
        return "tax-financial"
    if any(marker in haystack for marker in TRANSCRIPT_MARKERS):
        return "transcripts"
    if suffix in CODE_SUFFIXES:
        return "source-code"
    if doc_type == "pdf" or suffix == ".pdf":
        return "pdf-layout"
    return "prose-notes"


def stable_rank(seed: str, chunk_id: str) -> str:
    return hashlib.sha256(f"{seed}\0{chunk_id}".encode()).hexdigest()


def histogram(values: Iterable[int]) -> dict[str, int]:
    counts = Counter()
    for value in values:
        lower = 0
        placed = False
        for edge in HISTOGRAM_EDGES:
            if value <= edge:
                counts[f"{lower}-{edge}"] += 1
                placed = True
                break
            lower = edge + 1
        if not placed:
            counts["384+"] += int(value >= TOKEN_CEILING)
    labels = []
    lower = 0
    for edge in HISTOGRAM_EDGES:
        labels.append(f"{lower}-{edge}")
        lower = edge + 1
    labels.append("384+")
    return {label: counts[label] for label in labels}


def percentile(values: list[int], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return float(ordered[lo])
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def summarize(chunks: list[Chunk], *, digest: str) -> dict[str, Any]:
    tokens = [c.token_count for c in chunks]
    return {
        "chunk_count": len(chunks),
        "total_tokens_untruncated": sum(tokens),
        "total_tokens_model_input": sum(min(n, TOKEN_CEILING) for n in tokens),
        "token_percentiles": {
            "p5": round(percentile(tokens, 0.05), 1),
            "p50": round(percentile(tokens, 0.50), 1),
            "p95": round(percentile(tokens, 0.95), 1),
            "p99": round(percentile(tokens, 0.99), 1),
            "max": max(tokens, default=0),
        },
        "token_histogram": histogram(tokens),
        "at_or_above_384": sum(n >= TOKEN_CEILING for n in tokens),
        "at_or_above_384_pct": round(100 * sum(n >= TOKEN_CEILING for n in tokens) / max(1, len(tokens)), 3),
        "shape_counts": dict(sorted(Counter(c.shape for c in chunks).items())),
        "fixture_sha256": digest,
    }


def fetch_chunks(
    host: str,
    port: int,
    collection_name: str,
    exclude_path_fragments: tuple[str, ...] = (),
) -> tuple[list[tuple[str, str, dict[str, Any]]], int]:
    client = chromadb.HttpClient(host=host, port=port)
    collection = client.get_collection(collection_name)
    rows: list[tuple[str, str, dict[str, Any]]] = []
    scanned = collection.count()
    page_size = 1000
    for offset in range(0, collection.count(), page_size):
        page = collection.get(offset=offset, limit=page_size, include=["documents", "metadatas"])
        for chunk_id, text, metadata in zip(page["ids"], page["documents"], page["metadatas"]):
            metadata = dict(metadata or {})
            source = str(metadata.get("source_path") or metadata.get("source") or "")
            if text and not any(fragment in source for fragment in exclude_path_fragments):
                rows.append((str(chunk_id), str(text), metadata))
    return rows, scanned


def fixture_digest(chunks: list[Chunk]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk.id.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(chunk.text.encode()).digest())
    return digest.hexdigest()


def write_fixture(path: Path, chunks: list[Chunk]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = fixture_digest(chunks)
    with path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            # This file is private. Source paths are still omitted because the
            # benchmark only needs text, shape, and stable identifiers.
            record = {
                "id": chunk.id,
                "text": chunk.text,
                "shape": chunk.shape,
                "token_count": chunk.token_count,
                "silo": chunk.metadata.get("silo"),
                "doc_type": chunk.metadata.get("doc_type"),
                "text_sha256": hashlib.sha256(chunk.text.encode()).hexdigest(),
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return digest


def build(args: argparse.Namespace) -> dict[str, Any]:
    rows, scanned = fetch_chunks(
        args.host, args.port, args.collection, tuple(args.exclude_path_fragment)
    )
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    texts = [text for _, text, _ in rows]
    encoded = tokenizer(texts, add_special_tokens=True, truncation=False, padding=False)
    chunks = [
        Chunk(chunk_id, text, metadata, len(input_ids), classify_shape(metadata, text))
        for (chunk_id, text, metadata), input_ids in zip(rows, encoded["input_ids"])
    ]
    by_shape: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        by_shape.setdefault(chunk.shape, []).append(chunk)

    mixed = sorted(chunks, key=lambda c: stable_rank(args.seed, c.id))[: min(args.mixed_size, len(chunks))]
    tail_count = max(1, math.ceil(len(chunks) * 0.05))
    tail = sorted(chunks, key=lambda c: (-c.token_count, stable_rank(args.seed, c.id)))[:tail_count]
    fixtures = {"s0-mixed-v1": mixed, "s0-tail-v1": tail}
    for shape, members in sorted(by_shape.items()):
        fixtures[f"s0-{shape}-v1"] = sorted(
            members, key=lambda c: stable_rank(args.seed, c.id)
        )[: min(args.shape_size, len(members))]

    fixture_stats: dict[str, Any] = {}
    for name, members in fixtures.items():
        output = Path(args.private_root) / "fixtures" / f"{name}.jsonl"
        digest = write_fixture(output, members)
        fixture_stats[name] = summarize(members, digest=digest)

    corpus_digest = fixture_digest(sorted(chunks, key=lambda c: c.id))
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "privacy": "Aggregate statistics only. Text lives under gitignored spikes/private/s0.",
        "source": {
            "transport": "chroma-http", "collection": args.collection,
            "scanned_chunk_count": scanned, "eligible_chunk_count": len(chunks),
            "excluded_path_fragments": args.exclude_path_fragment,
        },
        "tokenizer": args.model,
        "token_ceiling": TOKEN_CEILING,
        "selection": {"seed": args.seed, "mixed_size": args.mixed_size, "shape_size": args.shape_size, "tail_fraction": 0.05},
        "corpus": summarize(chunks, digest=corpus_digest),
        "fixtures": fixture_stats,
    }
    manifest_path = Path(args.manifest)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(description=__doc__)
    out.add_argument("--host", default="127.0.0.1")
    out.add_argument("--port", type=int, default=8000)
    out.add_argument("--collection", default="llmli")
    out.add_argument("--model", default=MODEL)
    out.add_argument("--seed", default="s0-v1")
    out.add_argument("--mixed-size", type=int, default=2048)
    out.add_argument("--shape-size", type=int, default=512)
    out.add_argument("--private-root", default=str(PRIVATE_ROOT))
    out.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    out.add_argument(
        "--exclude-path-fragment", action="append", default=["/spikes/"],
        help="Exclude self-referential spike artifacts from the source snapshot.",
    )
    return out


if __name__ == "__main__":
    result = build(parser().parse_args())
    print(json.dumps(result, indent=2))
