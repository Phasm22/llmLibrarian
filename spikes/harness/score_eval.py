"""Score full, truncated, and random retrieval on the private S0 eval set."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chromadb
import numpy as np
from sentence_transformers import SentenceTransformer


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVAL = ROOT / "spikes" / "private" / "s0" / "s0-eval-v1.jsonl"
DEFAULT_OUTPUT = ROOT / "spikes" / "results" / "S0" / "quality-sabotage.json"


def source_key(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()[:20]


def dcg(grades: list[float]) -> float:
    return sum(grade / math.log2(rank + 2) for rank, grade in enumerate(grades))


def metrics_for(case: dict[str, Any], ranked_indices: list[int], rows: list[dict[str, Any]], k: int) -> tuple[float, float]:
    if case["kind"] == "known-item":
        relevant = set(case["relevant_chunk_ids"])
        grades = [1.0 if rows[index]["id"] in relevant else 0.0 for index in ranked_indices[:k]]
        recall = len({rows[index]["id"] for index in ranked_indices[:k]} & relevant) / len(relevant)
        return recall, dcg(grades) / max(dcg([1.0] * len(relevant)), 1e-9)
    relevance = case.get("relevance") or {}
    seen = set()
    grades = []
    for index in ranked_indices:
        key = rows[index]["source_key"]
        if key in seen:
            continue
        seen.add(key)
        grades.append(float(relevance.get(key, 0.0)))
        if len(grades) == k:
            break
    recall = sum(1 for key in seen if key in relevance) / max(1, len(relevance))
    ideal = sorted((float(value) for value in relevance.values()), reverse=True)[:k]
    return recall, dcg(grades) / max(dcg(ideal), 1e-9)


def resolve_silo(requested: str | None, available: set[str]) -> str | None:
    if not requested:
        return None
    if requested in available:
        return requested
    base = requested.rsplit("-", 1)[0] if "-" in requested else requested
    matches = sorted(silo for silo in available if silo == base or silo.startswith(base + "-"))
    return matches[0] if len(matches) == 1 else "__unresolved__"


def fetch(args: argparse.Namespace) -> tuple[list[dict[str, Any]], np.ndarray]:
    collection = chromadb.HttpClient(host=args.host, port=args.port).get_collection(args.collection)
    rows, vectors = [], []
    for offset in range(0, collection.count(), 500):
        page = collection.get(offset=offset, limit=500, include=["embeddings", "metadatas"])
        for chunk_id, embedding, metadata in zip(page["ids"], page["embeddings"], page["metadatas"]):
            metadata = dict(metadata or {})
            source = str(metadata.get("source_path") or metadata.get("source") or "")
            if "/spikes/" in source:
                continue
            rows.append({"id": str(chunk_id), "silo": str(metadata.get("silo") or ""), "source_key": source_key(source)})
            vectors.append(embedding)
    matrix = np.asarray(vectors, dtype=np.float32)
    matrix /= np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
    return rows, matrix


def run(args: argparse.Namespace) -> dict[str, Any]:
    cases = [json.loads(line) for line in Path(args.eval).read_text().splitlines() if line.strip()]
    rows, vectors = fetch(args)
    model = SentenceTransformer(args.model, device=args.device)
    query_vectors = model.encode([case["query"] for case in cases], normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)
    available = {row["silo"] for row in rows}
    by_silo: dict[str, list[int]] = defaultdict(list)
    for index, row in enumerate(rows):
        by_silo[row["silo"]].append(index)
    rng = random.Random(0)
    configs = {"full-768": 768, "truncate-64": 64, "random": 0}
    scored: dict[str, dict[str, Any]] = {}
    unresolved = 0
    for label, dimension in configs.items():
        accum = {12: [], 40: []}
        by_kind: dict[str, dict[int, list[tuple[float, float]]]] = defaultdict(lambda: defaultdict(list))
        used = 0
        for case, query_vector in zip(cases, query_vectors):
            silo = resolve_silo(case.get("silo"), available)
            if silo == "__unresolved__":
                if label == "full-768":
                    unresolved += 1
                continue
            candidates = by_silo[silo] if silo else list(range(len(rows)))
            if not candidates:
                continue
            if label == "random":
                ranked = candidates.copy()
                rng.shuffle(ranked)
                ranked = ranked[:40]
            else:
                docs = vectors[candidates, :dimension]
                docs = docs / np.maximum(np.linalg.norm(docs, axis=1, keepdims=True), 1e-12)
                query = query_vector[:dimension]
                query = query / max(float(np.linalg.norm(query)), 1e-12)
                scores = docs @ query
                top = np.argsort(-scores)[:40]
                ranked = [candidates[int(index)] for index in top]
            used += 1
            for k in (12, 40):
                values = metrics_for(case, ranked, rows, k)
                accum[k].append(values)
                by_kind[case["kind"]][k].append(values)
        scored[label] = {"cases": used}
        for k in (12, 40):
            scored[label][f"recall_at_{k}"] = float(np.mean([v[0] for v in accum[k]]))
            scored[label][f"ndcg_at_{k}"] = float(np.mean([v[1] for v in accum[k]]))
        scored[label]["by_kind"] = {
            kind: {
                str(k): {
                    "recall": float(np.mean([v[0] for v in values])),
                    "ndcg": float(np.mean([v[1] for v in values])),
                }
                for k, values in per_k.items()
            }
            for kind, per_k in by_kind.items()
        }
    result = {
        "spike": "S0", "run_id": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eval_set": "s0-eval-v1", "model": args.model, "device": args.device,
        "corpus_chunks": len(rows), "eval_cases": len(cases), "unresolved_silo_cases": unresolved,
        "results": scored,
        "sabotage_detected": scored["random"]["recall_at_12"] < scored["full-768"]["recall_at_12"] - 0.25,
        "bias_warning": "Historical-source labels came from prior production retrieval; full-768 is advantaged. Known-item queries are easier than natural language.",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    return result


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(description=__doc__)
    out.add_argument("--eval", default=str(DEFAULT_EVAL))
    out.add_argument("--output", default=str(DEFAULT_OUTPUT))
    out.add_argument("--host", default="127.0.0.1")
    out.add_argument("--port", type=int, default=8000)
    out.add_argument("--collection", default="llmli")
    out.add_argument("--model", default="sentence-transformers/all-mpnet-base-v2")
    out.add_argument("--device", default="mps")
    return out


if __name__ == "__main__":
    print(json.dumps(run(parser().parse_args()), indent=2))
