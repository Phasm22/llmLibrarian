"""Build the shared activity-ledger fixture used by both test suites.

Writes, under ``tests/fixtures/activity/``:

- ``db/`` — a tiny registry, file manifest, and ``llmli_activity.sqlite3``
- ``overview-7d.json`` — ``activity_ledger.overview(db, "7d", include_private=True)``
  at the fixed ``NOW`` below, in UTC

``macos/Tests/ActivityLedgerTests`` reads the same ledger with the Swift reader
and must reproduce the JSON; ``tests/unit/test_activity_fixture.py`` regenerates
it and fails when the committed copy is stale. Regenerate after changing the
overview contract:

    TZ=UTC uv run python tests/fixtures/activity/make_fixture.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))

NOW = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)
DAY = 86400.0
SILOS = (
    ("research-1", "Research", "/library/Research"),
    ("notes-2", "Project Notes", "/library/Project Notes"),
    ("tax-3", "Tax", "/library/Tax"),
    ("photos-4", "Photo Journal", "/library/Photo Journal"),
)


def _ago(days: float, hours: float = 0.0) -> float:
    return NOW.timestamp() - days * DAY - hours * 3600


def build(out: Path) -> dict:
    """Write the fixture DB into ``out`` and return the 7d overview."""
    os.environ["TZ"] = "UTC"
    time.tzset()
    os.environ["LLMLIBRARIAN_ACTIVITY"] = "1"

    import activity_ledger as al

    db = out / "db"
    if db.exists():
        for p in db.iterdir():
            p.unlink()
    db.mkdir(parents=True, exist_ok=True)
    registry = {
        slug: {"slug": slug, "display_name": name, "path": path, "files_indexed": 2, "chunks_count": 4,
               "updated": "2026-10-01T00:00:00", **({"private": True} if slug == "tax-3" else {})}
        for slug, name, path in SILOS
    }
    (db / "llmli_registry.json").write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")
    manifest = {"silos": {
        slug: {"path": path, "files": {f"{path}/{n}.md": {"mtime": 1.0, "size": 10, "hash": n} for n in ("a", "b")}}
        for slug, _, path in SILOS
    }}
    (db / "llmli_file_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    conn = al._connect(db)
    conn.execute("UPDATE meta SET value=? WHERE key IN ('files_since','searches_since','created_at')",
                 (str(_ago(90)),))
    # Pin last_prune far ahead so writing (or reopening) the fixture never prunes it.
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_prune', ?)", (str(NOW.timestamp() + 3650 * DAY),))
    r, n, t = "/library/Research", "/library/Project Notes", "/library/Tax"
    files = [
        (_ago(0, 2), "research-1", f"{r}/today.md", "added", "index"),
        (_ago(1), "research-1", f"{r}/a.md", "updated", "mcp"),
        (_ago(2), "research-1", f"{r}/b.md", "updated", "mcp"),
        (_ago(3), "research-1", f"{r}/gone.md", "removed", "mcp"),
        (_ago(1), "notes-2", f"{n}/plan.md", "added", "index"),
        (_ago(5), "notes-2", f"{n}/plan.md", "updated", "mcp"),
        (_ago(9), "notes-2", f"{n}/old.md", "added", "index"),          # previous week
        (_ago(10), "research-1", f"{r}/older.md", "added", "index"),    # previous week
        (_ago(2), "tax-3", f"{t}/w2.pdf", "added", "index"),
        (_ago(1), "research-1", f"{r}/a.md", "updated", "repair"),      # excluded
    ]
    conn.executemany("INSERT INTO file_events(ts, silo, path, kind, origin, size) VALUES (?,?,?,?,?,10)", files)
    conn.executemany(
        "INSERT INTO silo_events(ts, silo, kind, origin, files) VALUES (?,?,?,?,?)",
        [(_ago(4), "photos-4", "silo_added", "index", 2), (_ago(1), "photos-4", "silo_removed", "repair", 2)],
    )
    conn.executemany(
        "INSERT INTO failures(silo, path, error_class, error, first_seen, last_seen, occurrences, resolved_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        [
            ("research-1", f"{r}/scan1.pdf", "encrypted", "PdfReadError: encrypted", _ago(6), _ago(1), 3, None),
            ("research-1", f"{r}/scan2.pdf", "encrypted", "PdfReadError: encrypted", _ago(5), _ago(2), 1, None),
            ("notes-2", f"{n}/huge.csv", "too_large", "file too large", _ago(3), _ago(3), 1, None),
            ("notes-2", f"{n}/fixed.md", "timeout", "timed out", _ago(4), _ago(4), 1, _ago(2)),
        ],
    )
    conn.close()
    for days, q, silo, hits, client in [
        (0.1, "launch checklist", "notes-2", [("notes-2", f"{n}/plan.md")], "Claude Code"),
        (1, "offline search decision", "research-1", [("research-1", f"{r}/a.md")], "Claude Code"),
        (1.5, "hybrid retrieval", None, [("research-1", f"{r}/a.md"), ("notes-2", f"{n}/plan.md")], "Cursor"),
        (2, "hybrid retrieval", None, [("research-1", f"{r}/a.md")], "Cursor"),
        (3, "Quarterly   Budget", "notes-2", [], "Claude Code"),
        (4, "quarterly budget", "notes-2", [], "Codex"),
        (4.5, "w2 total", "tax-3", [], "llmli"),
        (5, "vacation photos", None, [], "Claude Code"),
        (8, "older question", "research-1", [("research-1", f"{r}/b.md")], "Claude Code"),
        (11, "older question", "research-1", [("research-1", f"{r}/b.md")], "Codex"),
    ]:
        al.record_search(
            db, tool="query_personal_knowledge", queries=[q], silo=silo,
            chunks=[{"silo": s, "source": p} for s, p in hits], client=client, now=_ago(days),
        )
    conn = al._connect(db)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    for extra in ("llmli_activity.sqlite3-wal", "llmli_activity.sqlite3-shm"):
        (db / extra).unlink(missing_ok=True)

    data = al.overview(db, "7d", now=NOW, include_private=True)
    # Machine-specific fields stay out of the shared contract.
    data.pop("ledger_path", None)
    for h in data["health"]:
        h.pop("path_exists", None)
    data["attention"]["falling_behind"] = []
    return data


def main() -> None:
    data = build(HERE)
    (HERE / "overview-7d.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    print(f"wrote {HERE / 'overview-7d.json'}")


if __name__ == "__main__":
    main()
