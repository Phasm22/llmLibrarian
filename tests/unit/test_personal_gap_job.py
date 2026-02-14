from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_job_module():
    root = Path(__file__).resolve().parents[2]
    module_path = root / "jobs" / "personal_gap_job.py"
    spec = importlib.util.spec_from_file_location("personal_gap_job", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def test_clamp_random_count_bounds():
    mod = _load_job_module()
    assert mod.clamp_random_count(1) == 10
    assert mod.clamp_random_count(12) == 12
    assert mod.clamp_random_count(30) == 20


def test_build_prompt_plan_counts():
    mod = _load_job_module()
    ctx = mod.SiloContext(
        slug="demo",
        display_name="Demo",
        path="/tmp/demo",
        files_indexed=10,
        chunks_count=20,
        sample_files=["a.py", "b.md"],
        top_extensions=["py", "md"],
        keywords=["demo", "pipeline"],
        content_hints=["import os"],
    )
    plan = mod.build_prompt_plan([ctx], random_count=12, seed=7)
    assert len(plan) == 17  # 5 per silo + 12 random
    silo_scoped = [p for p in plan if p.scope == "silo"]
    random_scoped = [p for p in plan if p.scope == "unified"]
    assert len(silo_scoped) == 5
    assert len(random_scoped) == 12


def test_build_prompt_plan_focus_pack_uses_sentinels_only():
    mod = _load_job_module()
    linear = mod.SiloContext(
        slug="become-a-linear-algebra-master-753e7529",
        display_name="Become a Linear Algebra Master",
        path="/tmp/la",
        files_indexed=10,
        chunks_count=20,
        sample_files=["a.pdf"],
        top_extensions=["pdf"],
        keywords=["linear"],
        content_hints=["matrix"],
    )
    self_ctx = mod.SiloContext(
        slug="__self__",
        display_name="Self (pal/llmLibrarian repo)",
        path="/tmp/self",
        files_indexed=5,
        chunks_count=12,
        sample_files=["pal.py"],
        top_extensions=["py"],
        keywords=["pal"],
        content_hints=["help"],
    )
    other = mod.SiloContext(
        slug="openclaw-aa2df362",
        display_name="openclaw",
        path="/tmp/openclaw",
        files_indexed=100,
        chunks_count=200,
        sample_files=["README.md"],
        top_extensions=["ts"],
        keywords=["openclaw"],
        content_hints=["whatsapp"],
    )
    plan = mod.build_prompt_plan([linear, self_ctx, other], random_count=12, seed=7, pack="focus")
    silo_specs = [p for p in plan if p.scope == "silo"]
    slugs = {p.silo_slug for p in silo_specs}
    assert slugs == {"become-a-linear-algebra-master-753e7529", "__self__"}
    assert len(plan) == 22  # 2 sentinel silos * 5 + 12 random


def test_build_silo_prompts_avoid_structure_route_trigger_terms():
    mod = _load_job_module()
    ctx = mod.SiloContext(
        slug="demo",
        display_name="Demo",
        path="/tmp/demo",
        files_indexed=10,
        chunks_count=20,
        sample_files=["a.py", "b.md"],
        top_extensions=["py", "md"],
        keywords=["demo", "pipeline"],
        content_hints=["import os"],
    )
    prompts = mod.build_silo_prompts(ctx)
    banned = ("sample files", "top extensions", "inventory", "structure")
    for spec in prompts:
        q = spec.prompt.lower()
        assert all(token not in q for token in banned)
        assert "do not answer with type counts" in q


def test_build_pal_ask_command_uses_scope():
    mod = _load_job_module()
    base = ["pal"]
    silo_prompt = mod.PromptSpec(
        section="Silo: Demo",
        title="Silo Q",
        prompt="what was i coding?",
        scope="silo",
        silo_slug="demo",
        silo_display="Demo",
    )
    unified_prompt = mod.PromptSpec(
        section="Random Cross-Silo Prompts",
        title="Random Q",
        prompt="what changed over time?",
        scope="unified",
    )
    silo_cmd = mod.build_pal_ask_command(base, silo_prompt)
    unified_cmd = mod.build_pal_ask_command(base, unified_prompt)
    assert silo_cmd == ["pal", "ask", "--in", "demo", "what was i coding?"]
    assert unified_cmd == ["pal", "ask", "--unified", "what changed over time?"]


def test_write_markdown_report_has_clear_prompt_separation(tmp_path: Path):
    mod = _load_job_module()
    out = tmp_path / "report.md"
    ctx = mod.SiloContext(
        slug="demo",
        display_name="Demo",
        path="/tmp/demo",
        files_indexed=2,
        chunks_count=8,
        sample_files=["a.py", "b.md"],
        top_extensions=["py", "md"],
        keywords=["demo"],
        content_hints=["print('hello')"],
    )
    prompt_a = mod.PromptSpec(
        section="Silo: Demo (demo)",
        title="Prompt A",
        prompt="Question A?",
        scope="silo",
        silo_slug="demo",
        silo_display="Demo",
    )
    prompt_b = mod.PromptSpec(
        section="Random Cross-Silo Prompts",
        title="Prompt B",
        prompt="Question B?",
        scope="unified",
    )
    results = [
        mod.PromptResult(spec=prompt_a, command=["pal", "ask", "--in", "demo", "Question A?"], return_code=0, stdout="Answer A", stderr=""),
        mod.PromptResult(spec=prompt_b, command=["pal", "ask", "--unified", "Question B?"], return_code=1, stdout="", stderr="Error B"),
    ]
    mod.write_markdown_report(out, db_path=tmp_path / "my_brain_db", seed=5, contexts=[ctx], results=results)

    text = out.read_text(encoding="utf-8")
    assert "# Personal Behavior Gap Report" in text
    assert "## Silo: Demo (demo)" in text
    assert "## Random Cross-Silo Prompts" in text
    assert "**Prompt**" in text
    assert "**Answer**" in text
    assert "---" in text
    assert "Error B" in text


def test_load_silo_contexts_applies_self_alias(tmp_path: Path):
    mod = _load_job_module()
    db = tmp_path / "db"
    db.mkdir(parents=True, exist_ok=True)
    (db / "llmli_registry.json").write_text(
        (
            '{'
            '"__self__":{"slug":"__self__","display_name":"llmLibrarian","path":"/tmp/self","files_indexed":1,"chunks_count":2},'
            '"tax":{"slug":"tax","display_name":"Tax","path":"/tmp/tax","files_indexed":1,"chunks_count":2}'
            '}'
        ),
        encoding="utf-8",
    )
    (db / "llmli_file_manifest.json").write_text('{"silos":{}}', encoding="utf-8")
    contexts = mod.load_silo_contexts(db, self_alias="Self Alias")
    by_slug = {c.slug: c for c in contexts}
    assert by_slug["__self__"].display_name == "Self Alias"
    assert by_slug["tax"].display_name == "Tax"


def test_update_prompt_ledger_creates_and_updates_rows(tmp_path: Path):
    mod = _load_job_module()
    tracker = tmp_path / "ledger.md"
    metrics = tmp_path / "metrics.json"
    run1 = tmp_path / "run1.md"
    run2 = tmp_path / "run2.md"
    spec = mod.PromptSpec(
        section="Random Cross-Silo Prompts",
        title="Random 1",
        prompt="Question?",
        scope="unified",
    )
    first = [mod.PromptResult(spec=spec, command=["pal", "ask", "--unified", "Question?"], return_code=0, stdout="First answer", stderr="")]
    second = [mod.PromptResult(spec=spec, command=["pal", "ask", "--unified", "Question?"], return_code=0, stdout="Second answer", stderr="")]
    mod.update_prompt_ledger(tracker, first, run1, metrics)
    mod.update_prompt_ledger(tracker, second, run2, metrics)
    text = tracker.read_text(encoding="utf-8")
    assert "| Random-01 |" in text
    assert "First answer" in text
    assert "Second answer" in text
    assert str(run2) in text
    assert "ScoreAfter" in text
    assert "Trend" in text
    metric_text = metrics.read_text(encoding="utf-8")
    assert "Random-01" in metric_text


def test_compute_quality_metrics_extracts_deterministic_signals():
    mod = _load_job_module()
    out = (
        "Low confidence: query is weakly related to indexed content.\n\n"
        "Based on the provided context, it appears that this is uncertain.\n"
        "1. First item 2. Second item\n"
        "Authored by you, but written by someone else.\n\n"
        "Sources:\n"
        "  • a.py (line 1) · 0.31\n"
        "  • b.py (line 2) · 0.30\n"
    )
    metrics = mod._compute_quality_metrics(out)
    assert metrics["hedge_count"] >= 2
    assert metrics["has_concentration_note"] is False
    assert metrics["list_format_issues"] == 1
    assert metrics["ownership_conflict_flag"] is True
    assert metrics["source_count"] == 2
    assert isinstance(metrics["quality_score"], int)


def test_update_prompt_ledger_trend_regressed_and_improved(tmp_path: Path):
    mod = _load_job_module()
    tracker = tmp_path / "ledger.md"
    metrics = tmp_path / "metrics.json"
    run1 = tmp_path / "run1.md"
    run2 = tmp_path / "run2.md"
    spec_a = mod.PromptSpec(
        section="Random Cross-Silo Prompts",
        title="Random 2",
        prompt="Question?",
        scope="unified",
    )
    spec_b = mod.PromptSpec(
        section="Random Cross-Silo Prompts",
        title="Random 3",
        prompt="Question?",
        scope="unified",
    )
    baseline = [
        mod.PromptResult(spec=spec_a, command=["pal", "ask", "--unified", "Question?"], return_code=0, stdout="Based on the provided context, it appears that unclear.", stderr=""),
        mod.PromptResult(spec=spec_b, command=["pal", "ask", "--unified", "Question?"], return_code=0, stdout="Direct findings.\n\nSources:\n  • a.py", stderr=""),
    ]
    second = [
        mod.PromptResult(spec=spec_a, command=["pal", "ask", "--unified", "Question?"], return_code=0, stdout="Direct findings.\n\nSources:\n  • a.py", stderr=""),
        mod.PromptResult(spec=spec_b, command=["pal", "ask", "--unified", "Question?"], return_code=0, stdout="Based on the provided context, it appears that unclear.", stderr=""),
    ]

    mod.update_prompt_ledger(tracker, baseline, run1, metrics)
    mod.update_prompt_ledger(tracker, second, run2, metrics)
    text = tracker.read_text(encoding="utf-8")
    assert "| Random-02 |" in text
    assert "| Random-03 |" in text
    assert "improved" in text
    assert "regressed" in text
