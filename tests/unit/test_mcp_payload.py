"""Byte budget for what MCP retrieval tools return to a small-context client.

Measured on :8766 2026-10-02: list_silos 10 KB (one 40-entry exclude_patterns list
repeated per silo), an unscoped 6-chunk answer 20 KB (chunks_by_silo repeated
every chunk). See docs/plans/mcp-small-model-contract.md.
"""
from __future__ import annotations

import json

import mcp_contract

_PATTERNS = [f"pattern-{i}/" for i in range(40)]


def _roster(n: int = 9) -> dict:
    return {
        "db_path": "/db",
        "silo_count": n,
        "silos": [
            {
                "slug": f"silo-{i}",
                "display_name": f"Silo {i}",
                "path": f"/data/silo-{i}",
                "chunks_count": 10,
                "exclude_patterns": list(_PATTERNS) if i else ["only-here/"],
                "language_stats": {"by_ext": {".py": 4}},
            }
            for i in range(n)
        ],
    }


def test_roster_hoists_shared_exclude_patterns():
    out = mcp_contract.slim_roster(_roster())

    assert out["default_exclude_patterns"] == _PATTERNS
    rows = {row["slug"]: row for row in out["silos"]}
    assert "exclude_patterns" not in rows["silo-1"]
    assert rows["silo-0"]["exclude_patterns"] == ["only-here/"]  # differs, so kept
    assert all("language_stats" not in row for row in out["silos"])
    assert len(json.dumps(out)) < len(json.dumps(_roster())) / 3


def test_verbose_roster_is_untouched():
    assert mcp_contract.slim_roster(_roster(), verbose=True) == _roster()


def _chunk(source: str, **extra) -> dict:
    return {
        "rank": 1,
        "text": "Bulk ferment for four hours.",
        "score": 0.61,
        "confidence": "high",
        "section": "",
        "source": source,
        "silo": "recipes-1",
        "doc_type": "other",
        "mtime_iso": "2026-09-14",
        "page": None,
        "line_start": None,
        "chunk_index": 3,
        "record_type": None,
        "source_modality": None,
        "summary_status": None,
        "needs_vision_enrichment": None,
        "indexed_at": "2026-09-14T00:00:00+00:00",
        "_signals": {"vector_rank": 1, "lexical_rank": None, "rrf_score": 0.03},
        **extra,
    }


def test_slim_chunks_keeps_answer_fields_only():
    (slim,) = mcp_contract.slim_chunks([_chunk("/r/bread.txt")])

    assert slim == {
        "text": "Bulk ferment for four hours.",
        "score": 0.61,
        "source": "/r/bread.txt",
        "silo": "recipes-1",
        "doc_type": "other",
        "mtime_iso": "2026-09-14",
    }


def test_photo_metadata_once_per_source_and_image_fields_kept():
    photo = {"captured_at": "2012-08-08T11:05:07", "camera_model": "COOLPIX S4000"}
    chunks = [
        _chunk("/p/DSCN2763.jpg", source_modality="image", summary_status="disabled", photo_metadata=photo),
        _chunk("/p/DSCN2763.jpg", source_modality="image", summary_status="disabled", photo_metadata=photo),
    ]
    first, second = mcp_contract.slim_chunks(chunks)

    assert first["photo_metadata"] == photo
    assert "photo_metadata" not in second
    assert first["source_modality"] == "image" and first["summary_status"] == "disabled"


def test_silo_counts_replaces_the_chunk_copy():
    chunks = [_chunk("/a"), _chunk("/b"), {**_chunk("/c"), "silo": "notes-2"}]
    assert mcp_contract.silo_counts(chunks) == {"recipes-1": 2, "notes-2": 1}


def test_unscoped_query_does_not_repeat_chunks(monkeypatch, tmp_path):
    import mcp_server
    import query.retrieve_locked as retrieve_locked

    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    monkeypatch.setattr(mcp_server, "_release_chroma", lambda: None)
    monkeypatch.setattr(
        retrieve_locked,
        "execute_retrieve_chroma_phase",
        lambda **kw: {"query": kw["query"], "intent": kw["intent"], "chunks": [_chunk("/r/bread.txt")]},
    )

    res = mcp_server.query_personal_knowledge("sourdough")

    assert "chunks_by_silo" not in res
    assert res["silo_counts"] == {"recipes-1": 1}
    assert "_signals" not in res["chunks"][0]
