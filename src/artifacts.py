from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chroma_client import get_client, release, writer_client
from chroma_lock import chroma_shared_lock
from constants import LLMLI_COLLECTION
from state import get_silo_artifact_compile, set_silo_artifact_compile, update_silo

_MONEY_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s*(?:billion|million|thousand|bn|mm|m|k)?", re.IGNORECASE)


def artifacts_enabled_for_silo(slug: str, source_path: str | Path) -> bool:
    """
    Gate artifact compilation to explicit opt-in silos.

    LLMLIBRARIAN_ARTIFACT_SILOS:
      - "*" => enable for all silos
      - comma-separated slug/display tokens => enable for matching silo
    """
    raw = os.environ.get("LLMLIBRARIAN_ARTIFACT_SILOS", "").strip()
    if not raw:
        return False
    allow = {part.strip() for part in raw.split(",") if part.strip()}
    if "*" in allow:
        return True
    slug_lower = slug.lower()
    path_name = Path(source_path).name.lower()
    return slug in allow or slug_lower in {a.lower() for a in allow} or path_name in {a.lower() for a in allow}


def _load_parent_rows(db_path: str, parent_slug: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    _PAGE = 200
    try:
        with chroma_shared_lock(db_path):
            coll = get_client(db_path).get_or_create_collection(name=LLMLI_COLLECTION)
            offset = 0
            while True:
                result = coll.get(
                    where={"silo": parent_slug},
                    include=["documents", "metadatas", "ids"],
                    limit=_PAGE,
                    offset=offset,
                )
                docs = result.get("documents") or []
                metas = result.get("metadatas") or []
                ids = result.get("ids") or []
                for i, doc in enumerate(docs):
                    rows.append(
                        {
                            "id": ids[i] if i < len(ids) else "",
                            "doc": str(doc or ""),
                            "meta": metas[i] if i < len(metas) and isinstance(metas[i], dict) else {},
                        }
                    )
                if len(docs) < _PAGE:
                    break
                offset += _PAGE
    finally:
        release()
    return rows


def _row_sort_key(row: dict[str, Any]) -> tuple[str, int, int, str]:
    meta = row.get("meta") or {}
    source = str(meta.get("source") or "")
    line = int(meta.get("line_start") or 0)
    page = int(meta.get("page") or 0)
    return (source, line, page, str(row.get("id") or ""))


def _fingerprint_rows(rows: list[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for row in sorted(rows, key=_row_sort_key):
        meta = row.get("meta") or {}
        source = str(meta.get("source") or "")
        line = str(meta.get("line_start") or "")
        page = str(meta.get("page") or "")
        doc = " ".join(str(row.get("doc") or "").split())
        h.update(f"{source}|{line}|{page}|{doc}\n".encode("utf-8"))
    return h.hexdigest()


def _classify_artifact_kind(text: str) -> str | None:
    low = text.lower()
    if "risk" in low or "uncertain" in low:
        return "risk"
    if "segment" in low or "business unit" in low:
        return "segment"
    if "guidance" in low or "outlook" in low or "expects" in low:
        return "guidance"
    if any(tok in low for tok in ("revenue", "income", "cash", "eps", "margin", "operating")):
        return "metric"
    return None


def _extract_artifact_rows(
    parent_slug: str,
    artifact_slug: str,
    rows: list[dict[str, Any]],
    *,
    max_facts: int,
    max_input_chars: int,
) -> tuple[list[tuple[str, str, dict[str, Any]]], dict[str, int], bool]:
    out: list[tuple[str, str, dict[str, Any]]] = []
    kind_counts: Counter[str] = Counter()
    consumed = 0
    truncated = False

    for row in sorted(rows, key=_row_sort_key):
        doc = str(row.get("doc") or "").strip()
        if not doc:
            continue
        consumed += len(doc)
        if consumed > max_input_chars:
            truncated = True
            break
        kind = _classify_artifact_kind(doc)
        if kind is None:
            continue
        meta = dict(row.get("meta") or {})
        source = str(meta.get("source") or "")
        line = int(meta.get("line_start") or 0)
        page = int(meta.get("page") or 0)
        money = _MONEY_RE.search(doc)
        value_hint = money.group(0) if money else ""
        snippet = " ".join(doc.split())[:280]
        stable_key = hashlib.sha256(f"{source}|{line}|{page}|{value_hint}|{snippet}".encode("utf-8")).hexdigest()
        chunk_id = hashlib.sha256(f"{artifact_slug}|{kind}|{stable_key}".encode("utf-8")).hexdigest()[:20]
        artifact_doc = f"{kind.upper()}: {snippet}"
        artifact_meta = {
            "silo": artifact_slug,
            "parent_silo": parent_slug,
            "artifact_kind": kind,
            "source": source,
            "line_start": line if line > 0 else None,
            "page": page if page > 0 else None,
            "doc_type": "artifact",
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }
        out.append((chunk_id, artifact_doc, artifact_meta))
        kind_counts[kind] += 1
        if len(out) >= max_facts:
            truncated = True
            break
    return out, dict(kind_counts), truncated


def _write_artifact_rows(db_path: str, artifact_slug: str, rows: list[tuple[str, str, dict[str, Any]]]) -> int:
    with writer_client(str(Path(db_path).resolve())) as client:
        coll = client.get_or_create_collection(name=LLMLI_COLLECTION)
        coll.delete(where={"silo": artifact_slug})
        if rows:
            coll.add(
                ids=[r[0] for r in rows],
                documents=[r[1] for r in rows],
                metadatas=[r[2] for r in rows],
            )
    return len(rows)


def compile_artifacts_for_silo(
    *,
    db_path: str | Path,
    parent_slug: str,
    source_path: str | Path,
    display_name: str | None = None,
) -> dict[str, Any]:
    """
    Compile artifacts for one parent silo.

    Scheduling policy: call only *after* run_add returns (option b).
    """
    db = str(Path(db_path).resolve())
    source = str(Path(source_path).resolve())
    if not artifacts_enabled_for_silo(parent_slug, source):
        return {"status": "disabled", "parent_silo": parent_slug}

    max_facts = max(1, int(os.environ.get("LLMLIBRARIAN_ARTIFACT_MAX_FACTS", "80") or "80"))
    max_input_chars = max(2000, int(os.environ.get("LLMLIBRARIAN_ARTIFACT_MAX_INPUT_CHARS", "180000") or "180000"))
    artifact_slug = f"{parent_slug}-artifacts"
    rows = _load_parent_rows(db, parent_slug)
    fingerprint = _fingerprint_rows(rows)

    existing = get_silo_artifact_compile(db, parent_slug) or {}
    if existing.get("fingerprint") == fingerprint:
        return {
            "status": "unchanged",
            "parent_silo": parent_slug,
            "artifact_silo": artifact_slug,
            "fingerprint": fingerprint,
        }

    artifact_rows, kind_counts, truncated = _extract_artifact_rows(
        parent_slug,
        artifact_slug,
        rows,
        max_facts=max_facts,
        max_input_chars=max_input_chars,
    )
    written = _write_artifact_rows(db, artifact_slug, artifact_rows)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Keep artifact silo visible and queryable in the same registry model.
    update_silo(
        db,
        artifact_slug,
        source,
        files_indexed=max(1, written) if written else 0,
        chunks_count=written,
        updated_iso=now_iso,
        display_name=(f"{display_name or parent_slug} artifacts").strip(),
    )
    set_silo_artifact_compile(
        db,
        parent_slug,
        {
            "fingerprint": fingerprint,
            "at": now_iso,
            "kind_counts": kind_counts,
            "artifact_silo": artifact_slug,
            "status": "completed",
            "truncated": bool(truncated),
        },
    )
    return {
        "status": "completed",
        "parent_silo": parent_slug,
        "artifact_silo": artifact_slug,
        "fingerprint": fingerprint,
        "chunks_written": written,
        "kind_counts": kind_counts,
        "truncated": bool(truncated),
    }
