#!/usr/bin/env python3
"""
Standalone behavior-gap job runner.

Generates personal prompts from silo names + indexed file context, runs `pal ask`
for each prompt, and writes a Markdown report with clear separation per prompt.

This is intentionally decoupled from the `pal` app command surface.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SELF_ALIAS = "Self (pal/llmLibrarian repo)"
DEFAULT_TRACKER_PATH = "docs/personal_gap_prompt_ledger.md"
DEFAULT_METRICS_PATH = "docs/personal_gap_prompt_metrics.json"
SILO_ANSWER_BEHAVIOR_SUFFIX = (
    "Use content evidence. Do not answer with type counts; infer projects/tasks from evidence and state uncertainty when needed."
)
RANDOM_OWNERSHIP_SUFFIX = (
    "Distinguish my authored work from reference/course/vendor material, and label uncertainty when evidence is weak."
)
TRACKER_COLUMNS = [
    "PromptID",
    "Issue",
    "Hypothesis",
    "ChangeID",
    "Expected",
    "ObservedBefore",
    "ObservedAfter",
    "Status",
    "ScoreBefore",
    "ScoreAfter",
    "Delta",
    "Trend",
    "RunRef",
]
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_HEDGE_RE = re.compile(
    r"\b("
    r"based on the provided context|"
    r"it appears that|"
    r"without more information|"
    r"without additional information|"
    r"likely|"
    r"possibly|"
    r"uncertain|"
    r"unclear|"
    r"difficult to determine"
    r")\b",
    re.IGNORECASE,
)

TEXT_EXTENSIONS = {
    ".md",
    ".txt",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".json",
    ".yaml",
    ".yml",
    ".sh",
    ".sql",
    ".toml",
    ".csv",
    ".xml",
    ".html",
    ".css",
    ".java",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
}

STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "your",
    "into",
    "what",
    "when",
    "where",
    "which",
    "work",
    "project",
    "projects",
    "notes",
    "note",
    "readme",
    "index",
    "main",
    "test",
    "tests",
    "final",
    "draft",
    "copy",
    "new",
    "old",
    "tmp",
    "temp",
    "data",
    "file",
    "files",
}

RANDOM_PROMPT_POOL = [
    "Across all silos, summarize what I was coding each year from 2020 to 2025 with evidence by year.",
    "Which projects did I revisit across time, and what changed between earlier and later iterations?",
    "What unfinished work appears across silos, and what evidence suggests likely blockers?",
    "What topics did I learn deeply versus only touch briefly, based on repeated evidence?",
    "Which codebases appear production-like versus class/homework-style, and what evidence supports each label?",
    "What strongest signals describe my long-term interests across silos?",
    "List candidate tech-stack shifts over time with evidence and confidence labels.",
    "Where do collaboration patterns appear versus solo experimentation, and what points to each?",
    "What recurring pain points or failure patterns appear across silos with concrete examples?",
    "Which periods look like coding bursts, and what was I working on during each period?",
    "What practical tools or scripts did I return to repeatedly across projects?",
    "What tasks did I automate for myself repeatedly across silos?",
    "Which projects look abandoned, and what likely next step was pending?",
    "Where do notes/documentation and implementation disagree about what I was building?",
    "What personal work habits are inferable from naming patterns and repeated artifacts?",
    "Which silos look polished versus exploratory, and what evidence supports the split?",
    "What are the top five recurring themes across all silos with supporting examples?",
    "Where did I use LLM-oriented workflows versus traditional coding workflows?",
    "What was I building that maps to school, work, and personal contexts respectively?",
    "Predict my most likely next project direction from portfolio evidence and uncertainty.",
]


@dataclass(frozen=True)
class SiloContext:
    slug: str
    display_name: str
    path: str
    files_indexed: int
    chunks_count: int
    sample_files: list[str]
    top_extensions: list[str]
    keywords: list[str]
    content_hints: list[str]


@dataclass(frozen=True)
class PromptSpec:
    section: str
    title: str
    prompt: str
    scope: str  # "silo" | "unified"
    silo_slug: str | None = None
    silo_display: str | None = None


@dataclass(frozen=True)
class PromptResult:
    spec: PromptSpec
    command: list[str]
    return_code: int
    stdout: str
    stderr: str


def _registry_path(db_path: Path) -> Path:
    return db_path / "llmli_registry.json" if db_path.is_dir() else db_path.parent / "llmli_registry.json"


def _manifest_path(db_path: Path) -> Path:
    return db_path / "llmli_file_manifest.json" if db_path.is_dir() else db_path.parent / "llmli_file_manifest.json"


def _read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return data if isinstance(data, dict) else default


def _ext_label(name: str) -> str:
    ext = Path(name).suffix.lower()
    return ext[1:] if ext.startswith(".") else (ext or "noext")


def _extract_keywords(values: list[str], max_items: int = 6) -> list[str]:
    counts: dict[str, int] = {}
    for value in values:
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}", value.lower()):
            if token in STOPWORDS:
                continue
            counts[token] = counts.get(token, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [name for name, _ in ordered[:max_items]]


def _sample_content_hints(paths: list[str], max_hints: int = 3) -> list[str]:
    hints: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw)
        if path.suffix.lower() not in TEXT_EXTENSIONS or not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    cleaned = " ".join(line.strip().split())
                    if not cleaned:
                        continue
                    if len(cleaned) > 120:
                        cleaned = cleaned[:117] + "..."
                    if cleaned.lower() in seen:
                        break
                    seen.add(cleaned.lower())
                    hints.append(cleaned)
                    break
        except Exception:
            continue
        if len(hints) >= max_hints:
            break
    return hints


def load_silo_contexts(db_path: Path, sample_file_cap: int = 40, self_alias: str = DEFAULT_SELF_ALIAS) -> list[SiloContext]:
    registry = _read_json(_registry_path(db_path), {})
    manifest = _read_json(_manifest_path(db_path), {"silos": {}})
    manifest_silos = manifest.get("silos") if isinstance(manifest.get("silos"), dict) else {}
    contexts: list[SiloContext] = []
    for slug in sorted(registry.keys()):
        entry = registry.get(slug) or {}
        silo_manifest = manifest_silos.get(slug) if isinstance(manifest_silos, dict) else {}
        files_dict = silo_manifest.get("files") if isinstance(silo_manifest, dict) else {}
        file_paths = sorted(files_dict.keys()) if isinstance(files_dict, dict) else []
        sample_paths = file_paths[:sample_file_cap]
        file_names = [Path(p).name for p in sample_paths]

        ext_counts: dict[str, int] = {}
        for fp in file_paths:
            label = _ext_label(fp)
            ext_counts[label] = ext_counts.get(label, 0) + 1
        top_exts = [name for name, _ in sorted(ext_counts.items(), key=lambda item: (-item[1], item[0]))[:4]]

        keywords = _extract_keywords(file_names + [str(entry.get("display_name") or ""), str(entry.get("path") or "")])
        content_hints = _sample_content_hints(sample_paths)
        display_name = self_alias if str(slug) == "__self__" else str(entry.get("display_name") or slug)
        contexts.append(
            SiloContext(
                slug=str(slug),
                display_name=display_name,
                path=str(entry.get("path") or ""),
                files_indexed=int(entry.get("files_indexed") or 0),
                chunks_count=int(entry.get("chunks_count") or 0),
                sample_files=file_names[:5],
                top_extensions=top_exts,
                keywords=keywords,
                content_hints=content_hints,
            )
        )
    return contexts


def _format_silo_cues(ctx: SiloContext) -> str:
    parts: list[str] = []
    if ctx.sample_files:
        parts.append("sample files: " + ", ".join(ctx.sample_files[:3]))
    if ctx.top_extensions:
        parts.append("top extensions: " + ", ".join(ctx.top_extensions[:3]))
    if ctx.keywords:
        parts.append("keywords: " + ", ".join(ctx.keywords[:4]))
    if ctx.content_hints:
        parts.append("content hints: " + " | ".join(ctx.content_hints[:2]))
    return "; ".join(parts)


def _format_silo_prompt_cues(ctx: SiloContext) -> str:
    parts: list[str] = []
    if ctx.sample_files:
        parts.append("anchors: " + ", ".join(ctx.sample_files[:3]))
    if ctx.top_extensions:
        parts.append("dominant formats: " + ", ".join(ctx.top_extensions[:3]))
    if ctx.keywords:
        parts.append("themes: " + ", ".join(ctx.keywords[:4]))
    if ctx.content_hints:
        parts.append("snippets: " + " | ".join(ctx.content_hints[:2]))
    return "; ".join(parts)


def build_silo_prompts(ctx: SiloContext) -> list[PromptSpec]:
    cues = _format_silo_prompt_cues(ctx)
    base = f"Context anchors for {ctx.display_name}: {cues}."
    return [
        PromptSpec(
            section=f"Silo: {ctx.display_name} ({ctx.slug})",
            title="What was I building?",
            prompt=(
                f"{base} What was I actually building in this silo? "
                "Answer with specific projects/tasks and cite concrete evidence. "
                + SILO_ANSWER_BEHAVIOR_SUFFIX
            ),
            scope="silo",
            silo_slug=ctx.slug,
            silo_display=ctx.display_name,
        ),
        PromptSpec(
            section=f"Silo: {ctx.display_name} ({ctx.slug})",
            title="What was I coding?",
            prompt=(
                f"{base} What was I coding here (languages, frameworks, scripts, automation), "
                "and what was each used for? "
                + SILO_ANSWER_BEHAVIOR_SUFFIX
            ),
            scope="silo",
            silo_slug=ctx.slug,
            silo_display=ctx.display_name,
        ),
        PromptSpec(
            section=f"Silo: {ctx.display_name} ({ctx.slug})",
            title="Timeline and shifts",
            prompt=(
                f"{base} Summarize my timeline in this silo: what came first, what changed later, "
                "and where direction shifted. "
                + SILO_ANSWER_BEHAVIOR_SUFFIX
            ),
            scope="silo",
            silo_slug=ctx.slug,
            silo_display=ctx.display_name,
        ),
        PromptSpec(
            section=f"Silo: {ctx.display_name} ({ctx.slug})",
            title="Unfinished and gaps",
            prompt=(
                f"{base} What looks unfinished or half-complete in this silo, and what likely next steps were pending? "
                + SILO_ANSWER_BEHAVIOR_SUFFIX
            ),
            scope="silo",
            silo_slug=ctx.slug,
            silo_display=ctx.display_name,
        ),
        PromptSpec(
            section=f"Silo: {ctx.display_name} ({ctx.slug})",
            title="Behavioral blind spots",
            prompt=(
                f"{base} If you were auditing for behavior gaps, where would this silo likely fail "
                "(routing, time reasoning, retrieval grounding, or answer framing)? "
                + SILO_ANSWER_BEHAVIOR_SUFFIX
            ),
            scope="silo",
            silo_slug=ctx.slug,
            silo_display=ctx.display_name,
        ),
    ]


def clamp_random_count(value: int) -> int:
    return max(10, min(20, int(value)))


def build_random_prompts(count: int, rng: random.Random) -> list[PromptSpec]:
    n = clamp_random_count(count)
    selected = rng.sample(RANDOM_PROMPT_POOL, k=min(n, len(RANDOM_PROMPT_POOL)))
    prompts: list[PromptSpec] = []
    for idx, text in enumerate(selected, start=1):
        prompts.append(
            PromptSpec(
                section="Random Cross-Silo Prompts",
                title=f"Random {idx}",
                prompt=f"{text} {RANDOM_OWNERSHIP_SUFFIX}",
                scope="unified",
            )
        )
    return prompts


def select_contexts_for_pack(contexts: list[SiloContext], pack: str = "full") -> list[SiloContext]:
    if (pack or "full").lower() != "focus":
        return list(contexts)
    selected: list[SiloContext] = []
    for ctx in contexts:
        is_self = ctx.slug == "__self__"
        is_linear_algebra = (
            ctx.slug.startswith("become-a-linear-algebra-master")
            or ctx.display_name.strip().lower() == "become a linear algebra master"
        )
        if is_self or is_linear_algebra:
            selected.append(ctx)
    return selected


def build_prompt_plan(contexts: list[SiloContext], random_count: int, seed: int, pack: str = "full") -> list[PromptSpec]:
    plan: list[PromptSpec] = []
    selected = select_contexts_for_pack(contexts, pack=pack)
    for ctx in selected:
        plan.extend(build_silo_prompts(ctx))
    plan.extend(build_random_prompts(random_count, random.Random(seed)))
    return plan


def _resolve_repo_root(script_path: Path) -> Path:
    return script_path.resolve().parents[1]


def resolve_pal_command(script_path: Path, pal_cmd: str | None) -> list[str]:
    if pal_cmd:
        return [pal_cmd]
    root = _resolve_repo_root(script_path)
    return [sys.executable, str(root / "pal.py")]


def build_pal_ask_command(base_pal_cmd: list[str], spec: PromptSpec) -> list[str]:
    cmd = list(base_pal_cmd)
    cmd.append("ask")
    if spec.scope == "silo" and spec.silo_slug:
        cmd.extend(["--in", spec.silo_slug])
    else:
        cmd.append("--unified")
    cmd.append(spec.prompt)
    return cmd


def run_prompt(spec: PromptSpec, base_pal_cmd: list[str], cwd: Path, timeout_sec: int) -> PromptResult:
    cmd = build_pal_ask_command(base_pal_cmd, spec)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_sec,
        )
        return PromptResult(
            spec=spec,
            command=cmd,
            return_code=int(proc.returncode),
            stdout=(proc.stdout or "").strip(),
            stderr=(proc.stderr or "").strip(),
        )
    except subprocess.TimeoutExpired as exc:
        return PromptResult(
            spec=spec,
            command=cmd,
            return_code=124,
            stdout=(exc.stdout or "").strip() if isinstance(exc.stdout, str) else "",
            stderr=f"Timed out after {timeout_sec}s",
        )


def _markdown_header(db_path: Path, out_path: Path, seed: int, prompt_count: int) -> list[str]:
    created = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    return [
        "# Personal Behavior Gap Report",
        "",
        f"- Generated (UTC): `{created}`",
        f"- DB path: `{db_path}`",
        f"- Output file: `{out_path}`",
        f"- Prompt seed: `{seed}`",
        f"- Total prompts: `{prompt_count}`",
        "",
    ]


def write_markdown_report(
    out_path: Path,
    db_path: Path,
    seed: int,
    contexts: list[SiloContext],
    results: list[PromptResult],
) -> None:
    lines: list[str] = _markdown_header(db_path, out_path, seed, len(results))
    lines.append("## Silo Snapshot")
    lines.append("")
    if not contexts:
        lines.append("_No silos found in registry._")
    for ctx in contexts:
        cues = _format_silo_cues(ctx)
        lines.append(
            f"- `{ctx.display_name}` (`{ctx.slug}`) | files={ctx.files_indexed} chunks={ctx.chunks_count} | {cues}"
        )
    lines.append("")

    grouped: dict[str, list[PromptResult]] = {}
    for result in results:
        grouped.setdefault(result.spec.section, []).append(result)

    for section in sorted(grouped.keys()):
        lines.append(f"## {section}")
        lines.append("")
        for idx, result in enumerate(grouped[section], start=1):
            lines.append(f"### Prompt {idx}: {result.spec.title}")
            lines.append("")
            lines.append(f"- Scope: `{result.spec.scope}`")
            if result.spec.silo_slug:
                lines.append(f"- Silo: `{result.spec.silo_display}` (`{result.spec.silo_slug}`)")
            lines.append(f"- Exit code: `{result.return_code}`")
            lines.append(f"- Command: ``{' '.join(result.command)}``")
            lines.append("")
            lines.append("**Prompt**")
            lines.append("")
            lines.append("```text")
            lines.append(result.spec.prompt)
            lines.append("```")
            lines.append("")
            lines.append("**Answer**")
            lines.append("")
            lines.append("```text")
            lines.append(result.stdout if result.stdout else "[no stdout]")
            lines.append("```")
            if result.stderr:
                lines.append("")
                lines.append("**stderr**")
                lines.append("")
                lines.append("```text")
                lines.append(result.stderr)
                lines.append("```")
            lines.append("")
            lines.append("---")
            lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _short_result_summary(stdout: str, stderr: str, max_len: int = 180) -> str:
    cleaned = _strip_ansi((stdout or "").strip())
    for raw in cleaned.splitlines():
        line = raw.strip()
        if line:
            return line if len(line) <= max_len else line[: max_len - 3] + "..."
    err_clean = _strip_ansi((stderr or "").strip())
    if err_clean:
        line = err_clean.splitlines()[0].strip()
        return line if len(line) <= max_len else line[: max_len - 3] + "..."
    return "[no output]"


def _extract_source_count(stdout: str) -> int:
    text = _strip_ansi(stdout)
    if "Sources:" not in text:
        return 0
    _, tail = text.split("Sources:", 1)
    count = 0
    for line in tail.splitlines():
        if line.strip().startswith("•"):
            count += 1
    return count


def _count_list_format_issues(stdout: str) -> int:
    text = _strip_ansi(stdout)
    issues = 0
    for line in text.splitlines():
        if len(re.findall(r"(?<!\w)\d+\.\s+", line)) >= 2:
            issues += 1
    return issues


def _has_ownership_conflict(stdout: str) -> bool:
    text = _strip_ansi(stdout).lower()
    authored = bool(re.search(r"\b(authored|written by you|likely authored)\b", text))
    other = bool(re.search(r"\b(written by someone else|someone else)\b", text))
    return authored and other


def _compute_quality_metrics(stdout: str) -> dict[str, int | bool]:
    text = _strip_ansi(stdout)
    hedge_count = len(_HEDGE_RE.findall(text))
    has_concentration_note = "Evidence concentration note:" in text
    list_format_issues = _count_list_format_issues(text)
    ownership_conflict_flag = _has_ownership_conflict(text)
    source_count = _extract_source_count(text)
    score = 100
    score -= min(30, hedge_count * 6)
    score -= (8 if has_concentration_note else 0)
    score -= list_format_issues * 10
    score -= (20 if ownership_conflict_flag else 0)
    score += min(10, source_count * 2)
    quality_score = max(0, min(100, int(score)))
    return {
        "hedge_count": hedge_count,
        "has_concentration_note": has_concentration_note,
        "list_format_issues": list_format_issues,
        "ownership_conflict_flag": ownership_conflict_flag,
        "source_count": source_count,
        "quality_score": quality_score,
    }


def _slug_token(value: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", value or "")
    if not words:
        return "Unknown"
    return "".join(w.capitalize() for w in words)


def _prompt_id(spec: PromptSpec, section_index: int) -> str:
    if spec.section == "Random Cross-Silo Prompts":
        m = re.search(r"(\d+)$", spec.title or "")
        if m:
            return f"Random-{int(m.group(1)):02d}"
        return f"Random-{section_index:02d}"
    label = spec.silo_display or spec.silo_slug or "Unknown"
    return f"Silo-{_slug_token(label)}-{section_index:02d}"


def _read_existing_tracker_rows(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if not line.startswith("|"):
            continue
        if line.startswith("| PromptID ") or line.startswith("| ---"):
            continue
        parts = [p.strip() for p in line.strip().split("|")[1:-1]]
        if len(parts) < 9:
            continue
        row: dict[str, str] = {}
        for i, col in enumerate(TRACKER_COLUMNS):
            row[col] = parts[i] if i < len(parts) else ""
        pid = row.get("PromptID", "")
        if pid:
            out[pid] = row
    return out


def _load_prompt_metrics(path: Path) -> dict[str, dict[str, int | bool | str]]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, int | bool | str]] = {}
    for pid, val in raw.items():
        if isinstance(pid, str) and isinstance(val, dict):
            out[pid] = dict(val)
    return out


def _save_prompt_metrics(path: Path, payload: dict[str, dict[str, int | bool | str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def update_prompt_ledger(
    tracker_path: Path,
    results: list[PromptResult],
    run_ref: Path,
    metrics_path: Path,
) -> None:
    existing = _read_existing_tracker_rows(tracker_path)
    previous_metrics = _load_prompt_metrics(metrics_path)
    grouped: dict[str, list[PromptResult]] = {}
    for result in results:
        grouped.setdefault(result.spec.section, []).append(result)

    rows: list[dict[str, str]] = []
    updated_metrics: dict[str, dict[str, int | bool | str]] = dict(previous_metrics)
    for section in sorted(grouped.keys()):
        for idx, result in enumerate(grouped[section], start=1):
            pid = _prompt_id(result.spec, idx)
            prev = existing.get(pid, {})
            previous_after = prev.get("ObservedAfter", "").strip()
            observed_before = prev.get("ObservedBefore", "").strip() or previous_after
            observed_after = _short_result_summary(result.stdout, result.stderr)
            current_metrics = _compute_quality_metrics(result.stdout)
            current_score = int(current_metrics.get("quality_score") or 0)
            prior_score_raw = (previous_metrics.get(pid) or {}).get("quality_score")
            score_before: str = ""
            delta_str: str = ""
            trend = "new"
            if isinstance(prior_score_raw, int):
                score_before = str(prior_score_raw)
                delta = int(current_score) - int(prior_score_raw)
                delta_str = str(delta)
                if delta > 0:
                    trend = "improved"
                elif delta < 0:
                    trend = "regressed"
                else:
                    trend = "steady"
            status = trend
            row = {
                "PromptID": pid,
                "Issue": prev.get("Issue", ""),
                "Hypothesis": prev.get("Hypothesis", ""),
                "ChangeID": prev.get("ChangeID", ""),
                "Expected": prev.get("Expected", ""),
                "ObservedBefore": observed_before,
                "ObservedAfter": observed_after,
                "Status": status,
                "ScoreBefore": score_before,
                "ScoreAfter": str(current_score),
                "Delta": delta_str,
                "Trend": trend,
                "RunRef": str(run_ref),
            }
            rows.append(row)
            updated_metrics[pid] = {
                **current_metrics,
                "quality_score": current_score,
                "run_ref": str(run_ref),
                "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }

    lines = [
        "# Personal Gap Prompt Ledger",
        "",
        "Prompt-level state tracking across incremental changes.",
        "",
        "| " + " | ".join(TRACKER_COLUMNS) + " |",
        "| " + " | ".join(["---"] * len(TRACKER_COLUMNS)) + " |",
    ]
    for row in rows:
        vals = [row.get(col, "").replace("\n", " ").replace("|", "/") for col in TRACKER_COLUMNS]
        lines.append("| " + " | ".join(vals) + " |")
    tracker_path.parent.mkdir(parents=True, exist_ok=True)
    tracker_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    _save_prompt_metrics(metrics_path, updated_metrics)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run silo-personalized prompt job through pal ask and write markdown report.")
    parser.add_argument(
        "--db",
        default=os.environ.get("LLMLIBRARIAN_DB", "./my_brain_db"),
        help="llmLibrarian DB path (default: env LLMLIBRARIAN_DB or ./my_brain_db)",
    )
    parser.add_argument(
        "--out",
        default="behavior_gap_report.md",
        help="Markdown output path (default: behavior_gap_report.md)",
    )
    parser.add_argument(
        "--random-count",
        type=int,
        default=12,
        help="Number of random cross-silo prompts (clamped to 10..20; default 12)",
    )
    parser.add_argument(
        "--pack",
        choices=["focus", "full"],
        default="focus",
        help="Prompt pack mode: focus (random + sentinel silos) or full (all silos).",
    )
    parser.add_argument("--seed", type=int, default=20260214, help="Deterministic seed for random prompts.")
    parser.add_argument("--timeout-sec", type=int, default=180, help="Timeout per prompt execution.")
    parser.add_argument("--pal-cmd", help="Optional pal executable to use (default: current repo pal.py).")
    parser.add_argument("--tracker", default=DEFAULT_TRACKER_PATH, help="Prompt ledger markdown output path.")
    parser.add_argument("--metrics", default=DEFAULT_METRICS_PATH, help="Prompt metrics JSON sidecar path.")
    parser.add_argument("--self-alias", default=DEFAULT_SELF_ALIAS, help="Display alias for __self__ silo.")
    parser.add_argument("--dry-run", action="store_true", help="Generate report scaffolding without running pal ask.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    script_path = Path(__file__).resolve()
    repo_root = _resolve_repo_root(script_path)
    db_path = Path(args.db).expanduser()
    if not db_path.is_absolute():
        db_path = (repo_root / db_path).resolve()
    out_path = Path(args.out).expanduser()
    if not out_path.is_absolute():
        out_path = (repo_root / out_path).resolve()

    contexts_all = load_silo_contexts(db_path, self_alias=args.self_alias)
    contexts = select_contexts_for_pack(contexts_all, pack=args.pack)
    if not contexts:
        print(f"No silos found in registry at {_registry_path(db_path)}", file=sys.stderr)
        return 1
    plan = build_prompt_plan(contexts, random_count=args.random_count, seed=args.seed, pack=args.pack)
    base_pal_cmd = resolve_pal_command(script_path, args.pal_cmd)

    results: list[PromptResult] = []
    for spec in plan:
        if args.dry_run:
            cmd = build_pal_ask_command(base_pal_cmd, spec)
            results.append(PromptResult(spec=spec, command=cmd, return_code=0, stdout="[dry-run]", stderr=""))
            continue
        result = run_prompt(spec, base_pal_cmd=base_pal_cmd, cwd=repo_root, timeout_sec=args.timeout_sec)
        results.append(result)
        print(f"[{len(results)}/{len(plan)}] {spec.title} -> rc={result.return_code}", file=sys.stderr)

    write_markdown_report(out_path, db_path=db_path, seed=args.seed, contexts=contexts, results=results)
    tracker_path = Path(args.tracker).expanduser()
    if not tracker_path.is_absolute():
        tracker_path = (repo_root / tracker_path).resolve()
    metrics_path = Path(args.metrics).expanduser()
    if not metrics_path.is_absolute():
        metrics_path = (repo_root / metrics_path).resolve()
    update_prompt_ledger(tracker_path, results=results, run_ref=out_path, metrics_path=metrics_path)
    print(f"Wrote report: {out_path}")
    print(f"Wrote tracker: {tracker_path}")
    print(f"Wrote metrics: {metrics_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
