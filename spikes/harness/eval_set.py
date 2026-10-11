"""Build the private S0 retrieval eval set from query audit and known items."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from query.intent import route_intent


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUDIT = Path.home() / ".pal" / "logs" / "query-audit.jsonl"
DEFAULT_OUTPUT = ROOT / "spikes" / "private" / "s0" / "s0-eval-v1.jsonl"
DEFAULT_SUMMARY = ROOT / "spikes" / "harness" / "eval-manifest.json"
DEFAULT_FIXTURE = ROOT / "spikes" / "private" / "s0" / "fixtures" / "s0-mixed-v1.jsonl"


def source_key(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()[:20]


def load_audit(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def known_item_cases(path: Path, limit: int = 50) -> list[dict[str, Any]]:
    """Create deterministic rare-term searches with exact chunk relevance."""
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    terms_by_id = {}
    document_frequency: Counter[str] = Counter()
    for row in rows:
        terms = {
            token.lower() for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{4,}", row["text"])
            if not token.lower().startswith(("http", "www"))
        }
        terms_by_id[row["id"]] = terms
        document_frequency.update(terms)
    candidates = []
    for row in rows:
        scored = sorted(
            terms_by_id[row["id"]],
            key=lambda term: (-math.log((len(rows) + 1) / (document_frequency[term] + 1)), term),
        )
        query_terms = scored[:8]
        if len(query_terms) < 4:
            continue
        query = " ".join(query_terms)
        candidates.append({
            "id": hashlib.sha256(f"known\0{row['id']}\0{query}".encode()).hexdigest()[:20],
            "kind": "known-item", "query": query, "silo": row.get("silo"),
            "intent": route_intent(query), "relevant_chunk_ids": [row["id"]],
            "shape": row.get("shape"), "observations": 1,
            "label_warning": "Query is generated from rare terms in the target chunk; easier than natural-language retrieval.",
        })
    return sorted(candidates, key=lambda case: case["id"])[:limit]


def build(args: argparse.Namespace) -> dict[str, Any]:
    grouped: dict[tuple[str, str | None], list[dict[str, Any]]] = defaultdict(list)
    rows = load_audit(Path(args.audit))
    for row in rows:
        for query in row.get("queries") or []:
            grouped[(str(query).strip().lower(), row.get("silo"))].append(row)
    cases = []
    agreement_scores = []
    for (query, silo), events in sorted(grouped.items()):
        vote_counts: Counter[str] = Counter()
        event_sets = []
        for event in events:
            sources = {
                source_key(str(src.get("path") or src.get("file") or ""))
                for src in (event.get("result") or {}).get("sources") or []
                if src.get("path") or src.get("file")
            }
            event_sets.append(sources)
            vote_counts.update(sources)
        if len(event_sets) > 1:
            for left, right in zip(event_sets, event_sets[1:]):
                agreement_scores.append(len(left & right) / max(1, len(left | right)))
        cases.append({
            "id": hashlib.sha256(f"audit\0{silo}\0{query}".encode()).hexdigest()[:20],
            "kind": "historical-source",
            "query": query,
            "silo": silo,
            "intent": route_intent(query),
            "relevance": {key: count / len(events) for key, count in vote_counts.items()},
            "observations": len(events),
            "label_warning": "Returned-source proxy, not a click or human relevance judgment.",
        })
    audit_case_count = len(cases)
    cases.extend(known_item_cases(Path(args.fixture), args.known_items))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    intents = Counter(case["intent"] for case in cases)
    silos = Counter(str(case["silo"] or "unscoped") for case in cases)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    summary = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eval_set": "s0-eval-v1",
        "private_path": "spikes/private/s0/s0-eval-v1.jsonl",
        "sha256": digest,
        "audit_events": len(rows),
        "unique_cases": len(cases),
        "historical_source_cases": audit_case_count,
        "known_item_cases": len(cases) - audit_case_count,
        "intent_counts": dict(sorted(intents.items())),
        "silo_case_counts": dict(sorted(silos.items())),
        "repeated_case_pairs": len(agreement_scores),
        "repeat_source_jaccard_mean": sum(agreement_scores) / len(agreement_scores) if agreement_scores else None,
        "limitations": [
            "Audit sources are retrieval outputs, not user clicks or human judgments.",
            "The observed query mix is dominated by LOOKUP and cannot validate routing intents with sparse or zero coverage.",
            "Source-level labels cannot distinguish relevant from irrelevant chunks within the same file.",
            "Known-item rare-term queries are deliberately easier than natural-language queries.",
        ],
    }
    summary_path = Path(args.summary)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(description=__doc__)
    out.add_argument("--audit", default=str(DEFAULT_AUDIT))
    out.add_argument("--output", default=str(DEFAULT_OUTPUT))
    out.add_argument("--summary", default=str(DEFAULT_SUMMARY))
    out.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    out.add_argument("--known-items", type=int, default=50)
    return out


if __name__ == "__main__":
    print(json.dumps(build(parser().parse_args()), indent=2))
