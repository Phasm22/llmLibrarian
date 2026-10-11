"""Build indexed fixture silos for lite-vs-full MCP gap comparison."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ingest import run_add
from state import resolve_silo_by_path, set_silo_private

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "lite_harness"


@dataclass(frozen=True)
class LiteHarnessIndex:
    db_path: Path
    recipes_slug: str
    journal_slug: str
    private_slug: str
    photos_slug: str
    recipes_root: Path
    journal_root: Path
    private_root: Path
    photos_root: Path


def materialize_sources(dest: Path) -> dict[str, Path]:
    """Copy fixture trees into ``dest`` so ingest paths are unique per run."""
    roots: dict[str, Path] = {}
    for name in ("recipes", "journal", "private_lab", "photos"):
        src = FIXTURE_ROOT / name
        target = dest / name
        shutil.copytree(src, target)
        roots[name] = target
    return roots


def build_index(tmp_root: Path) -> LiteHarnessIndex:
    """Index recipes, journal, private lab, and photos silos under ``tmp_root/db``."""
    sources = materialize_sources(tmp_root / "sources")
    db = tmp_root / "db"
    for root in sources.values():
        run_add(root, db_path=db, incremental=False, allow_cloud=True)

    recipes_slug = resolve_silo_by_path(str(db), sources["recipes"])
    journal_slug = resolve_silo_by_path(str(db), sources["journal"])
    private_slug = resolve_silo_by_path(str(db), sources["private_lab"])
    photos_slug = resolve_silo_by_path(str(db), sources["photos"])
    assert recipes_slug and journal_slug and private_slug and photos_slug

    set_silo_private(str(db), private_slug, True)
    return LiteHarnessIndex(
        db_path=db,
        recipes_slug=recipes_slug,
        journal_slug=journal_slug,
        private_slug=private_slug,
        photos_slug=photos_slug,
        recipes_root=sources["recipes"],
        journal_root=sources["journal"],
        private_root=sources["private_lab"],
        photos_root=sources["photos"],
    )
