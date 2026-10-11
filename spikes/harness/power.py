"""macOS powermetrics sampling and parsing shared by spike benchmarks."""
from __future__ import annotations

import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any


POWER_RE = re.compile(r"^(CPU Power|GPU Power|ANE Power|Combined Power \(CPU \+ GPU \+ ANE\)):\s+(\d+) mW$", re.MULTILINE)
GPU_FREQ_RE = re.compile(r"^GPU HW active frequency:\s+(\d+) MHz$", re.MULTILINE)
GPU_ACTIVE_RE = re.compile(r"^GPU HW active residency:\s+([\d.]+)%", re.MULTILINE)
GPU_IDLE_RE = re.compile(r"^GPU idle residency:\s+([\d.]+)%", re.MULTILINE)


def parse_powermetrics(text: str) -> dict[str, Any]:
    """Parse per-sample primary rail power and GPU residency.

    The GPU power line appears twice in a sample. Splitting at ``GPU usage``
    makes the first value the three-rail block used for energy accounting.
    """
    samples = []
    for block in text.split("*** Sampled system activity")[1:]:
        primary = block.split("**** GPU usage ****", 1)[0]
        rails: dict[str, float] = {}
        for label, value in POWER_RE.findall(primary):
            key = {"CPU Power": "cpu_mw", "GPU Power": "gpu_mw", "ANE Power": "ane_mw"}.get(label, "combined_mw")
            rails[key] = float(value)
        freq = GPU_FREQ_RE.search(block)
        active = GPU_ACTIVE_RE.search(block)
        idle = GPU_IDLE_RE.search(block)
        if rails:
            samples.append({
                **rails,
                "gpu_frequency_mhz": float(freq.group(1)) if freq else None,
                "gpu_active_pct": float(active.group(1)) if active else None,
                "gpu_idle_pct": float(idle.group(1)) if idle else None,
            })
    if not samples:
        return {"samples": [], "mean": {}}
    numeric_keys = {key for sample in samples for key, value in sample.items() if value is not None}
    return {"samples": samples, "mean": {key: mean(sample[key] for sample in samples if sample.get(key) is not None) for key in numeric_keys}}


@dataclass
class PowerCapture:
    interval_ms: int = 250
    settle_seconds: float = 0.35

    def __post_init__(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(prefix="s0-powermetrics-", suffix=".txt", delete=False)
        self.path = Path(self._tmp.name)
        self._proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self._proc = subprocess.Popen(
            ["sudo", "-n", "/usr/bin/powermetrics", "--samplers", "cpu_power,gpu_power", "-i", str(self.interval_ms)],
            stdout=self._tmp, stderr=subprocess.STDOUT,
        )
        time.sleep(self.settle_seconds)

    def stop(self) -> dict[str, Any]:
        if self._proc is not None:
            self._proc.send_signal(signal.SIGINT)
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                self._proc.wait(timeout=2)
        self._tmp.close()
        result = parse_powermetrics(self.path.read_text(errors="replace"))
        self.path.unlink(missing_ok=True)
        return result


def measure_idle(seconds: float = 5.0, interval_ms: int = 250) -> dict[str, float]:
    capture = PowerCapture(interval_ms=interval_ms)
    capture.start()
    time.sleep(seconds)
    return capture.stop()["mean"]
