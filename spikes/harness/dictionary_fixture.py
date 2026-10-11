"""Recreate the synthetic dictionary-words workload that motivated S0."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path

from transformers import AutoTokenizer

from spikes.harness.corpus import Chunk, MODEL, summarize, write_fixture


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "spikes" / "private" / "s0" / "fixtures" / "s0-dictionary-v1.jsonl"
DEFAULT_STATS = ROOT / "spikes" / "harness" / "dictionary-manifest.json"


def build(args: argparse.Namespace) -> dict:
    words = [
        word.strip() for word in Path(args.words).read_text(errors="replace").splitlines()
        if word.strip().isalpha() and len(word.strip()) > 2
    ]
    rng = random.Random(args.seed)
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    texts = []
    for _ in range(args.count):
        picked = []
        length = 0
        while length < args.characters:
            word = rng.choice(words)
            picked.append(word)
            length += len(word) + 1
        texts.append(" ".join(picked)[: args.characters])
    encoded = tokenizer(texts, add_special_tokens=True, truncation=False, padding=False)
    chunks = [
        Chunk(
            hashlib.sha256(f"{args.seed}\0{index}\0{text}".encode()).hexdigest()[:24],
            text, {}, len(tokens), "synthetic-dictionary",
        )
        for index, (text, tokens) in enumerate(zip(texts, encoded["input_ids"]))
    ]
    digest = write_fixture(Path(args.output), chunks)
    result = {
        "schema_version": 1, "synthetic": True, "seed": args.seed,
        "source_words": args.words, "characters_per_chunk": args.characters,
        "tokenizer": args.model, "statistics": summarize(chunks, digest=digest),
    }
    Path(args.stats).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(description=__doc__)
    out.add_argument("--words", default="/usr/share/dict/words")
    out.add_argument("--count", type=int, default=512)
    out.add_argument("--characters", type=int, default=1000)
    out.add_argument("--seed", default="s0-dictionary-v1")
    out.add_argument("--model", default=MODEL)
    out.add_argument("--output", default=str(DEFAULT_OUTPUT))
    out.add_argument("--stats", default=str(DEFAULT_STATS))
    return out


if __name__ == "__main__":
    print(json.dumps(build(parser().parse_args()), indent=2))
