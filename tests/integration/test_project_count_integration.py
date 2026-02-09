from pathlib import Path

from ingest import run_add
from query.core import run_ask
from state import resolve_silo_by_path


def test_project_count_reports_unique_buckets(tmp_path):
    root = tmp_path / "stuff"
    (root / "alpha").mkdir(parents=True)
    (root / "beta").mkdir(parents=True)
    (root / "alpha" / "main.py").write_text("print('alpha')", encoding="utf-8")
    (root / "beta" / "app.ts").write_text("console.log('beta')", encoding="utf-8")
    (root / "root_script.py").write_text("print('root')", encoding="utf-8")

    db_path = tmp_path / "db"
    files_indexed, failures = run_add(root, db_path=db_path, allow_cloud=True)
    assert failures == 0
    assert files_indexed == 3
    slug = resolve_silo_by_path(db_path, root)
    assert slug is not None

    out = run_ask(
        archetype_id=None,
        query="how many coding projects have I done in this folder",
        db_path=db_path,
        silo=slug,
        no_color=True,
        use_reranker=False,
    )
    assert "Found 3 coding project folders" in out
    assert "alpha/main.py" in out or "alpha" in out
    assert "beta/app.ts" in out or "beta" in out
