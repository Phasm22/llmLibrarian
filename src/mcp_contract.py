"""What an MCP response must tell a small model so it can act without looping.

Pure functions over retrieval results; no Chroma, no I/O. mcp_server.py calls
these on the way out of every retrieval tool.

Background: on 2026-10-02 a 3B-active model on Open-WebUI got 27 empty results
in a row. Each response said only "try rephrasing", so it rephrased, kept the
word that caused the empty result, and fanned out ten parallel calls. The rule
here: an empty result always names a next tool, and a repeated call is told so.
See docs/plans/mcp-small-model-contract.md.
"""
from __future__ import annotations

import re
from typing import Any

_YEAR = re.compile(r"\b(20\d{2}|19\d{2})\b")


def _find_files_args(query: str, silo: str | None) -> dict[str, Any]:
    args: dict[str, Any] = {}
    years = _YEAR.findall(query or "")
    if len(years) == 1:
        args["date_start"] = f"{years[0]}-01-01"
        args["date_end"] = f"{years[0]}-12-31"
    if silo:
        args["silos"] = [silo]
    return args


def deterministic_alternative(
    deterministic_intent: str | None,
    *,
    query: str,
    silo: str | None,
    profile: str = "full",
) -> dict[str, Any] | None:
    """The tool that answers the inventory-style reading of a question.

    Full profile only: lite has none of these tools, and a recommended_action must
    never name a tool the caller's catalog does not have.

    run_retrieve retrieves these questions as ordinary lookups (the router matches
    single words like "capabilities" or "inventory"); this names the tool for the
    case where the user really did mean "which file types" or "list my files".
    """
    if not deterministic_intent or profile != "full":
        return None
    if deterministic_intent == "CAPABILITIES":
        return {
            "tool": "capabilities",
            "args": {},
            "reason": (
                "If the user is asking which file types llmLibrarian itself can index, "
                "call capabilities(). Otherwise answer from the chunks."
            ),
        }
    if deterministic_intent in {"FILE_LIST", "STRUCTURE", "METADATA_ONLY", "TIMELINE"}:
        return {
            "tool": "find_files",
            "args": _find_files_args(query, silo),
            "reason": (
                "If the user wants a list of files by name or date rather than what the "
                "files say, call find_files. Otherwise answer from the chunks."
            ),
        }
    if deterministic_intent == "CODE_LANGUAGE":
        return {
            "tool": "list_silos",
            "args": {"verbose": True},
            "reason": (
                "If the user is asking which programming language they use most, each "
                "silo's language_stats (verbose=True) counts files by extension."
            ),
        }
    return None


def empty_result_action(
    result: dict[str, Any],
    *,
    query: str,
    silo: str | None,
    profile: str = "full",
) -> dict[str, Any] | None:
    """recommended_action for a response whose chunks list is empty.

    Returns None when the result already explains itself (an existing
    recommended_action, a busy/retry signal, an error the caller must surface).
    """
    if result.get("chunks") or result.get("recommended_action"):
        return None
    if result.get("busy") or result.get("error") or result.get("errors"):
        return None
    write_state = result.get("write_in_progress") or {}
    if isinstance(write_state, dict) and write_state.get("results_may_be_incomplete"):
        return {
            "tool": "retrieve_knowledge" if profile == "lite" else "query_personal_knowledge",
            "args": {"query": query, **({"silo": silo} if silo else {})},
            "reason": (
                "The index for this silo is being rebuilt, so an empty result is not "
                "evidence of absence. Retry the same call once the rebuild finishes."
            ),
        }
    alternative = deterministic_alternative(
        result.get("deterministic_intent"), query=query, silo=silo, profile=profile
    )
    if alternative:
        return alternative
    if profile == "lite":
        return {
            "tool": "silo_roster",
            "args": {},
            "reason": (
                f"Nothing in {silo!r} matched. Check silo_roster for a better-fitting "
                "silo; if none fits, tell the user the index has nothing on this. "
                "Do not repeat the same query."
            ),
        }
    if silo:
        return {
            "tool": "query_personal_knowledge",
            "args": {"query": query},
            "reason": (
                f"Nothing in {silo!r} matched. Retry once without silo to search every "
                "shared silo, or call find_files if the user named a file. Do not repeat "
                "the same scoped query."
            ),
        }
    return {
        "tool": "find_files",
        "args": _find_files_args(query, None),
        "reason": (
            "No indexed text matched. If the user named a file or a date, call "
            "find_files(name_glob=...). Otherwise tell the user the index has nothing "
            "on this; rephrasing the same words will return the same result."
        ),
    }


def apply_guidance(
    result: dict[str, Any],
    *,
    query: str,
    silo: str | None,
    profile: str = "full",
) -> dict[str, Any]:
    """Attach recommended_action (empty result) or alternative_tool (non-empty
    result that the router also read as an inventory question). Mutates and
    returns ``result``."""
    action = empty_result_action(result, query=query, silo=silo, profile=profile)
    if action:
        result["recommended_action"] = action
    elif result.get("chunks") and result.get("deterministic_intent"):
        alternative = deterministic_alternative(
            result["deterministic_intent"], query=query, silo=silo, profile=profile
        )
        if alternative:
            result["alternative_tool"] = alternative
    return result


# --- Payload size -------------------------------------------------------------
# Measured on :8766 2026-10-02: a 20-chunk photo answer was 19 KB, a 6-chunk
# unscoped answer 20 KB (chunks_by_silo repeated every chunk), list_silos 10 KB
# with one 40-entry exclude_patterns list repeated per silo. A 16k-context client
# pays for every byte on every turn.

# Per-chunk fields a model reads to answer and cite. Everything else (rank,
# per-chunk confidence, chunk_index, record_type, indexed_at, _signals) is
# pipeline state; explain_retrieval still returns the signals.
_CHUNK_FIELDS = ("text", "score", "source", "silo", "doc_type", "section", "page", "line_start", "mtime_iso", "query")
_IMAGE_FIELDS = ("source_modality", "summary_status")


def slim_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project retrieval chunks to what a model uses; drop empty fields.

    photo_metadata is kept once per source: every OCR region of a photo carried
    the same capture/camera/GPS block.
    """
    out: list[dict[str, Any]] = []
    photo_seen: set[str] = set()
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        slim = {k: chunk[k] for k in _CHUNK_FIELDS if chunk.get(k) not in (None, "")}
        if chunk.get("source_modality") == "image":
            for k in _IMAGE_FIELDS:
                if chunk.get(k):
                    slim[k] = chunk[k]
            if chunk.get("needs_vision_enrichment"):
                slim["needs_vision_enrichment"] = True
        photo = chunk.get("photo_metadata")
        source = str(chunk.get("source") or "")
        if isinstance(photo, dict) and photo and source not in photo_seen:
            slim["photo_metadata"] = photo
            photo_seen.add(source)
        out.append(slim)
    return out


def silo_counts(chunks: list[dict[str, Any]]) -> dict[str, int]:
    """Chunks per silo — the grouping chunks_by_silo gave, without the copy."""
    counts: dict[str, int] = {}
    for chunk in chunks:
        silo = str(chunk.get("silo") or "")
        counts[silo] = counts.get(silo, 0) + 1
    return counts


def slim_roster(result: dict[str, Any], *, verbose: bool = False) -> dict[str, Any]:
    """list_silos/session_context roster without per-silo repetition.

    exclude_patterns is hoisted to one default_exclude_patterns list and kept on a
    silo only where it differs; language_stats appears only when verbose.
    """
    silos = result.get("silos")
    if not isinstance(silos, list) or verbose:
        return result
    pattern_sets: dict[str, int] = {}
    for silo in silos:
        key = repr(silo.get("exclude_patterns"))
        pattern_sets[key] = pattern_sets.get(key, 0) + 1
    common_key = max(pattern_sets, key=pattern_sets.get) if pattern_sets else None
    common = next((s.get("exclude_patterns") for s in silos if repr(s.get("exclude_patterns")) == common_key), None)
    slimmed = []
    for silo in silos:
        row = {k: v for k, v in silo.items() if k not in ("language_stats", "exclude_patterns")}
        if repr(silo.get("exclude_patterns")) != common_key and silo.get("exclude_patterns") is not None:
            row["exclude_patterns"] = silo["exclude_patterns"]
        slimmed.append(row)
    out = {**result, "silos": slimmed}
    if common:
        out["default_exclude_patterns"] = common
    return out
