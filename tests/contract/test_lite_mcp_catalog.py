"""Contract: lite MCP catalog stays small and covers the small-model task surface."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _lite_tool_payload() -> dict:
    script = r"""
import asyncio, json
import mcp_server

async def main():
    tools = await mcp_server.mcp.list_tools()
    payload = [
        {
            "name": t.name,
            "description": t.description or "",
            "parameters": getattr(t, "parameters", None) or {},
        }
        for t in tools
    ]
    print(json.dumps({
        "names": sorted(t.name for t in tools),
        "schema_bytes": len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
        "retrieve_params": next(
            (p.get("parameters", {}).get("properties", {}) for p in payload if p["name"] == "retrieve_knowledge"),
            {},
        ),
    }))

asyncio.run(main())
"""
    env = os.environ.copy()
    env["LLMLIBRARIAN_MCP_PROFILE"] = "lite"
    env["LLMLIBRARIAN_ENV_BOOTSTRAPPED"] = "1"
    env.setdefault("LLMLIBRARIAN_DB", str(ROOT / "my_brain_db"))
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_lite_catalog_is_five_tools_and_stays_small():
    data = _lite_tool_payload()
    assert data["names"] == [
        "ask_image",
        "find_files",
        "read_document",
        "retrieve_knowledge",
        "silo_roster",
    ]
    # Soft budget: a few KB of schema (not the ~28 KB full catalog).
    assert data["schema_bytes"] < 12_000, data["schema_bytes"]
    props = data["retrieve_params"]
    for key in (
        "main_protein",
        "category",
        "course",
        "tags",
        "min_servings",
        "max_servings",
        "max_total_time",
        "section",
        "doc_type",
        "era",
        "source",
    ):
        assert key in props, key


def test_lite_harness_tasks_pass(tmp_path):
    """Empirical full-vs-lite harness; marks gaps as failures when lite regresses."""
    from tests.harness.lite_mcp_compare import _bind_mcp, run_tasks, summarize
    from tests.harness.lite_mcp_fixtures import build_index

    index = build_index(tmp_path)
    mcp = _bind_mcp(index.db_path)
    results = run_tasks(index, mcp)
    report = summarize(results, [])
    gaps = report["lite_gaps_vs_full"]
    # multi_angle is informational (sequential workaround OK); everything else must pass.
    hard_gaps = [g for g in gaps if g != "multi_angle_recipe_survey"]
    assert not hard_gaps, json.dumps(report["tasks"], indent=2)
