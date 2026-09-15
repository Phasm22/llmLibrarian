"""Host-aware ingest tuning for --fast.

One flag instead of four env knobs: detect what this host can do and set
the ingest tunables accordingly. Explicitly-set environment variables
always win — --fast only fills in what the user hasn't pinned.

Heuristics are grounded in measurements on an Apple M4 Pro
(10P+4E cores, 48 GB, macOS 15) with all-mpnet-base-v2:

  cpu  batch=32   77.8 docs/s   <- CPU peaks at SMALL batches
  cpu  batch=256  66.4 docs/s
  mps  batch=128  205.2 docs/s  <- MPS ~2.6x CPU, flat across 32-256
  mps  batch=256  198.5 docs/s

MPS and CPU embeddings agree to ~2.5e-7 max abs diff (cosine 1.000000),
so mixing devices across incremental updates is safe.
"""

from __future__ import annotations

import os
import subprocess
import sys

_ENV_KEYS = (
    "LLMLIBRARIAN_EMBEDDING_DEVICE",
    "LLMLIBRARIAN_EMBEDDING_BATCH_SIZE",
    "LLMLIBRARIAN_EMBEDDING_WORKERS",
    "LLMLIBRARIAN_ADD_BATCH_SIZE",
)


def _total_ram_gb() -> float:
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
            ).stdout.strip()
            return int(out) / (1024**3)
        except Exception:
            return 8.0
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024**3)
    except (ValueError, OSError):
        return 8.0


def _mps_available() -> bool:
    try:
        import torch

        return bool(torch.backends.mps.is_available())
    except Exception:
        return False


def _cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def detect_fast_profile() -> dict:
    """Return {'env': {...}, 'summary': str} for this host.

    Accelerator order matches embeddings._pick_device: MPS > CUDA > CPU.
    Keeping CUDA here is not an optimization but a correctness requirement —
    --fast *pins* LLMLIBRARIAN_EMBEDDING_DEVICE, which short-circuits that
    autodetection, so omitting CUDA would make --fast pin a Linux GPU box to
    the CPU and run slower than passing no flag at all.
    """
    ram_gb = _total_ram_gb()
    use_mps = _mps_available() and ram_gb >= 16

    if use_mps:
        device, embed_batch = "mps", 128
    elif _cuda_available():
        # Batch 128 mirrors the MPS tuning: both are throughput-bound
        # accelerators where the launch overhead that makes small batches
        # win on CPU no longer dominates.
        device, embed_batch = "cuda", 128
    else:
        device, embed_batch = "cpu", 32

    # Embedding workers are threads sharing one model; 2 overlaps
    # tokenization/IO with compute. More just contend (CPU) or queue (GPU).
    embed_workers = 2

    if ram_gb >= 32:
        add_batch = 512
    elif ram_gb >= 16:
        add_batch = 384
    else:
        add_batch = 256

    env = {
        "LLMLIBRARIAN_EMBEDDING_DEVICE": device,
        "LLMLIBRARIAN_EMBEDDING_BATCH_SIZE": str(embed_batch),
        "LLMLIBRARIAN_EMBEDDING_WORKERS": str(embed_workers),
        "LLMLIBRARIAN_ADD_BATCH_SIZE": str(add_batch),
    }
    summary = (
        f"fast profile: device={device} embed_batch={embed_batch} "
        f"embed_workers={embed_workers} add_batch={add_batch} "
        f"(ram={ram_gb:.0f}GB, accel={device if device != 'cpu' else 'none'})"
    )
    return {"env": env, "summary": summary}


def apply_fast_profile(verbose: bool = True) -> dict:
    """Detect and export the profile; explicit env vars are never overridden.

    Returns the detected profile dict. Child processes (llmli spawned by
    pal, worker subprocesses) inherit the exported values.
    """
    profile = detect_fast_profile()
    applied: dict[str, str] = {}
    for key, value in profile["env"].items():
        if not os.environ.get(key, "").strip():
            os.environ[key] = value
            applied[key] = value
    profile["applied"] = applied
    if verbose:
        skipped = [k for k in _ENV_KEYS if k not in applied]
        note = f" (respecting preset: {', '.join(skipped)})" if skipped else ""
        print(f"[llmLibrarian] {profile['summary']}{note}", file=sys.stderr)
    return profile
