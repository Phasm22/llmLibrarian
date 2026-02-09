from ingest import run_add
from query.core import run_ask
from state import resolve_silo_by_path


def test_tax_field_lookup_no_cross_year_guess(tmp_path):
    root = tmp_path / "Tax"
    (root / "2021").mkdir(parents=True)
    (root / "2024").mkdir(parents=True)
    (root / "2021" / "2021_TaxReturn.txt").write_text(
        "Form 1040\nline 9: 99,999.\n",
        encoding="utf-8",
    )
    (root / "2024" / "2024 Federal Income Tax Return.txt").write_text(
        "Form 1040\nline 8: 7,500.\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "db"
    files_indexed, failures = run_add(root, db_path=db_path, allow_cloud=True)
    assert files_indexed == 2
    assert failures == 0
    slug = resolve_silo_by_path(db_path, root)
    assert slug is not None

    out = run_ask(
        archetype_id=None,
        query="on 2024 form 1040 what is line 9 total income",
        db_path=db_path,
        silo=slug,
        no_color=True,
        use_reranker=False,
    )
    assert "I found 2024 tax documents, but I could not find Form 1040 line 9 in extractable text." in out
    assert "I'm not inferring from other years." in out
    assert "99,999" not in out


def test_tax_field_lookup_returns_exact_2024_value(tmp_path):
    root = tmp_path / "Tax"
    (root / "2021").mkdir(parents=True)
    (root / "2024").mkdir(parents=True)
    (root / "2021" / "2021_TaxReturn.txt").write_text(
        "Form 1040\nline 9: 99,999.\n",
        encoding="utf-8",
    )
    (root / "2024" / "2024 Federal Income Tax Return.txt").write_text(
        "Form 1040\nline 9: 7,522.\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "db"
    files_indexed, failures = run_add(root, db_path=db_path, allow_cloud=True)
    assert files_indexed == 2
    assert failures == 0
    slug = resolve_silo_by_path(db_path, root)
    assert slug is not None

    out = run_ask(
        archetype_id=None,
        query="on 2024 form 1040 what is line 9 total income",
        db_path=db_path,
        silo=slug,
        no_color=True,
        use_reranker=False,
    )
    assert "Form 1040 line 9 (2024): 7,522." in out
    assert "2024 Federal Income Tax Return.txt" in out
    assert "99,999" not in out
