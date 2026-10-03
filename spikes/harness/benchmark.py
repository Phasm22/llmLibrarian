"""Comparable embedding benchmark runner for every embedding/GPU spike."""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import resource
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np
import sentence_transformers
import torch
from sentence_transformers import SentenceTransformer

from spikes.harness.power import PowerCapture, measure_idle


ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "spikes" / "results" / "S0"
QUALITY_RESULTS = RESULTS / "quality-sabotage.json"


def pct(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values), q)) if values else 0.0


def load_fixture(path: Path) -> tuple[list[str], int, str, bool]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return (
        [row["text"] for row in rows],
        sum(min(384, int(row["token_count"])) for row in rows),
        path.stem,
        any(str(row.get("shape", "")).startswith("synthetic") for row in rows),
    )


def machine_info() -> dict[str, Any]:
    chip = platform.processor() or platform.machine()
    try:
        chip = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True).strip()
        ram = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)) / 1024**3
    except Exception:
        ram = None
    power = "unknown"
    try:
        power = "ac" if "AC Power" in subprocess.check_output(["pmset", "-g", "batt"], text=True) else "battery"
    except Exception:
        pass
    return {
        "chip": chip, "ram_gb": round(ram) if ram else None,
        "os": platform.mac_ver()[0] or platform.platform(), "power": power,
        "torch": torch.__version__, "sentence_transformers": sentence_transformers.__version__,
    }


def thermal_state() -> str:
    try:
        return subprocess.check_output(["pmset", "-g", "therm"], text=True).strip()
    except Exception as exc:
        return f"unavailable: {exc}"


def baseline_quality() -> dict[str, Any]:
    """Attach the separately measured baseline quality to speed results."""
    try:
        full = json.loads(QUALITY_RESULTS.read_text())["results"]["full-768"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        full = {}
    return {
        "eval_set": "s0-eval-v1",
        "recall_at_12": full.get("recall_at_12"),
        "ndcg_at_12": full.get("ndcg_at_12"),
        "recall_at_40": full.get("recall_at_40"),
        "ndcg_at_40": full.get("ndcg_at_40"),
        "delta_vs_baseline": 0.0 if full else None,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    texts, fixture_tokens, fixture_name, synthetic = load_fixture(Path(args.fixture))
    model = SentenceTransformer(args.model, device=args.device)
    if args.precision == "fp16":
        model.half()
    encode_kwargs = {"batch_size": args.batch, "convert_to_numpy": True, "normalize_embeddings": True, "show_progress_bar": False}
    model.encode(texts[: min(len(texts), args.batch)], **encode_kwargs)  # warmup, explicitly discarded
    idle = measure_idle(args.idle_seconds, args.power_interval_ms) if args.energy else {}
    throughputs: list[float] = []
    token_throughputs: list[float] = []
    energies: list[float] = []
    gross_energies: list[float] = []
    power_notes: list[dict[str, Any]] = []
    durations: list[float] = []
    cycles_per_run = max(1, math.ceil(args.minimum_run_seconds / max(0.001, args.probe_seconds))) if args.cycles is None else args.cycles
    # Refine a user-independent cycle count using one untimed probe.
    if args.cycles is None:
        start = time.perf_counter()
        model.encode(texts, **encode_kwargs)
        probe = time.perf_counter() - start
        cycles_per_run = max(1, math.ceil(args.minimum_run_seconds / max(probe, 0.001)))
    for _ in range(args.repeats):
        capture = PowerCapture(interval_ms=args.power_interval_ms) if args.energy else None
        if capture:
            capture.start()
        start = time.perf_counter()
        for _cycle in range(cycles_per_run):
            model.encode(texts, **encode_kwargs)
        duration = time.perf_counter() - start
        power = capture.stop() if capture else {"mean": {}, "samples": []}
        chunks = len(texts) * cycles_per_run
        tokens = fixture_tokens * cycles_per_run
        durations.append(duration)
        throughputs.append(chunks / duration)
        token_throughputs.append(tokens / duration)
        if power["mean"] and tokens:
            gross_mw = power["mean"].get("combined_mw", 0.0)
            net_mw = gross_mw - idle.get("combined_mw", 0.0)
            gross_energies.append((gross_mw / 1000.0) * duration * 1_000_000 / tokens)
            if net_mw > 0:
                energies.append((net_mw / 1000.0) * duration * 1_000_000 / tokens)
        power_notes.append({"mean": power["mean"], "sample_count": len(power["samples"])})
    one_latencies = []
    for text in texts[: min(20, len(texts))]:
        start = time.perf_counter()
        model.encode([text], **encode_kwargs)
        one_latencies.append((time.perf_counter() - start) * 1000)
    now = datetime.now(timezone.utc)
    run_id = f"{now.strftime('%Y-%m-%dT%H-%M-%SZ')}-{args.label}"
    spread = (pct(throughputs, 95) / max(pct(throughputs, 5), 1e-9))
    result = {
        "spike": "S0", "run_id": run_id,
        "hypothesis": "Production embedding throughput and energy are reproducible on real corpus text",
        "verdict": "inconclusive", "confidence": "medium",
        "machine": machine_info(),
        "config": {"backend": "sentence-transformers", "model": args.model, "precision": args.precision, "device": args.device, "batch": args.batch, "synthetic": synthetic, "fixture": fixture_name},
        "throughput": {"unit": "chunks/s", "median": median(throughputs), "p5": pct(throughputs, 5), "p95": pct(throughputs, 95), "n": len(throughputs), "model_input_tokens_per_s_median": median(token_throughputs)},
        "latency_ms": {"batch1_median": median(one_latencies), "batch1_p99": pct(one_latencies, 99)},
        "quality": baseline_quality(),
        "memory": {"rss_peak_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2, "gpu_mb": None, "released_after_unload": None},
        "energy": {
            "joules_per_1m_tokens": median(energies) if len(energies) == len(throughputs) else None,
            "gross_joules_per_1m_tokens": median(gross_energies) if gross_energies else None,
            "idle_mean_mw": idle.get("combined_mw"), "valid_net_runs": len(energies), "n": len(throughputs),
            "valid": len(energies) == len(throughputs),
        },
        "cost": "baseline; existing sentence-transformers and torch dependencies",
        "notes": {"durations_s": durations, "cycles_per_run": cycles_per_run, "thermal": thermal_state(), "power": power_notes, "p95_p5_ratio": spread},
        "would_change_my_mind": "p95/p5 above 1.5, a representative fixture mismatch, or an independently judged quality regression",
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    output = RESULTS / f"{run_id}.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    return result


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(description=__doc__)
    out.add_argument("--fixture", required=True)
    out.add_argument("--label", required=True)
    out.add_argument("--model", default="sentence-transformers/all-mpnet-base-v2")
    out.add_argument("--device", choices=("cpu", "mps", "cuda"), default="cpu")
    out.add_argument("--precision", choices=("fp32", "fp16"), default="fp32")
    out.add_argument("--batch", type=int, default=32)
    out.add_argument("--repeats", type=int, default=5)
    out.add_argument("--cycles", type=int)
    out.add_argument("--minimum-run-seconds", type=float, default=3.0)
    out.add_argument("--probe-seconds", type=float, default=1.0, help=argparse.SUPPRESS)
    out.add_argument("--idle-seconds", type=float, default=5.0)
    out.add_argument("--power-interval-ms", type=int, default=250)
    out.add_argument("--no-energy", action="store_false", dest="energy")
    return out


if __name__ == "__main__":
    print(json.dumps(run(parser().parse_args()), indent=2))
