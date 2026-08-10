"""Coverage for the --fast host-tuning profile.

Every accelerator branch is driven from any host: the suite runs on macOS
(MPS) and Linux CI (CUDA/CPU), so without stubbing neither platform would
ever cover the other's path.
"""

from __future__ import annotations

import pytest

import perf_profile


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in perf_profile._ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


def _stub(monkeypatch, *, mps=False, cuda=False, ram=64.0):
    monkeypatch.setattr(perf_profile, "_mps_available", lambda: mps)
    monkeypatch.setattr(perf_profile, "_cuda_available", lambda: cuda)
    monkeypatch.setattr(perf_profile, "_total_ram_gb", lambda: ram)


def test_prefers_mps_when_available(monkeypatch):
    _stub(monkeypatch, mps=True, cuda=False)
    env = perf_profile.detect_fast_profile()["env"]
    assert env["LLMLIBRARIAN_EMBEDDING_DEVICE"] == "mps"
    assert env["LLMLIBRARIAN_EMBEDDING_BATCH_SIZE"] == "128"


def test_uses_cuda_when_no_mps(monkeypatch):
    """A Linux GPU host must not be pinned to CPU.

    --fast sets LLMLIBRARIAN_EMBEDDING_DEVICE explicitly, which overrides
    embeddings._pick_device; picking "cpu" here would make --fast slower
    than passing no flag at all.
    """
    _stub(monkeypatch, mps=False, cuda=True)
    env = perf_profile.detect_fast_profile()["env"]
    assert env["LLMLIBRARIAN_EMBEDDING_DEVICE"] == "cuda"
    assert env["LLMLIBRARIAN_EMBEDDING_BATCH_SIZE"] == "128"


def test_mps_wins_over_cuda(monkeypatch):
    _stub(monkeypatch, mps=True, cuda=True)
    assert perf_profile.detect_fast_profile()["env"]["LLMLIBRARIAN_EMBEDDING_DEVICE"] == "mps"


def test_falls_back_to_cpu_with_small_batch(monkeypatch):
    """CPU peaks at small batches; the accelerator tuning would regress it."""
    _stub(monkeypatch, mps=False, cuda=False)
    env = perf_profile.detect_fast_profile()["env"]
    assert env["LLMLIBRARIAN_EMBEDDING_DEVICE"] == "cpu"
    assert env["LLMLIBRARIAN_EMBEDDING_BATCH_SIZE"] == "32"


def test_low_ram_host_does_not_take_mps(monkeypatch):
    _stub(monkeypatch, mps=True, cuda=False, ram=8.0)
    env = perf_profile.detect_fast_profile()["env"]
    assert env["LLMLIBRARIAN_EMBEDDING_DEVICE"] == "cpu"
    assert env["LLMLIBRARIAN_ADD_BATCH_SIZE"] == "256"


@pytest.mark.parametrize("ram,expected", [(64.0, "512"), (24.0, "384"), (8.0, "256")])
def test_add_batch_scales_with_ram(monkeypatch, ram, expected):
    _stub(monkeypatch, mps=False, cuda=False, ram=ram)
    assert perf_profile.detect_fast_profile()["env"]["LLMLIBRARIAN_ADD_BATCH_SIZE"] == expected


def test_apply_never_overrides_explicit_env(monkeypatch):
    """The documented contract: what the user pinned always wins."""
    _stub(monkeypatch, mps=True, cuda=False)
    monkeypatch.setenv("LLMLIBRARIAN_EMBEDDING_DEVICE", "cpu")

    profile = perf_profile.apply_fast_profile(verbose=False)

    import os

    assert os.environ["LLMLIBRARIAN_EMBEDDING_DEVICE"] == "cpu"
    assert "LLMLIBRARIAN_EMBEDDING_DEVICE" not in profile["applied"]
    # The unpinned knobs are still filled in.
    assert os.environ["LLMLIBRARIAN_ADD_BATCH_SIZE"] == "512"


def test_apply_exports_for_child_processes(monkeypatch):
    """pal shells out to llmli, so the profile has to land in os.environ."""
    _stub(monkeypatch, mps=False, cuda=True)

    perf_profile.apply_fast_profile(verbose=False)

    import os

    assert os.environ["LLMLIBRARIAN_EMBEDDING_DEVICE"] == "cuda"
    assert os.environ["LLMLIBRARIAN_EMBEDDING_WORKERS"] == "2"


def test_ram_detection_degrades_to_default(monkeypatch):
    """Neither sysctl nor sysconf available (or refusing) must not crash."""
    monkeypatch.setattr(perf_profile.os, "sysconf", lambda _n: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(perf_profile.sys, "platform", "linux")
    assert perf_profile._total_ram_gb() == 8.0


def test_accelerator_probe_survives_missing_torch(monkeypatch):
    """CI installs torch-free; the probes must report False, not explode."""
    monkeypatch.setitem(__import__("sys").modules, "torch", None)
    assert perf_profile._mps_available() is False
    assert perf_profile._cuda_available() is False
