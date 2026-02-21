from ingest import run_add
from query.core import run_ask
from state import resolve_silo_by_path


def test_tax_deterministic_box_lookup_and_no_cross_year_leakage(tmp_path):
    root = tmp_path / "Tax"
    (root / "2024").mkdir(parents=True)
    (root / "2025").mkdir(parents=True)

    (root / "2024" / "deloitte-w2-2024.txt").write_text(
        "Form W-2\nEmployer: Deloitte\nBox 2: 4000.00\n",
        encoding="utf-8",
    )
    (root / "2025" / "Federal_Colorado_deloitte2025.txt").write_text(
        "Form W-2\nEmployer: Deloitte\nBox 2: 4723.31\nBox 1: 35676.33\n",
        encoding="utf-8",
    )
    (root / "2025" / "ymca-w2-2025.txt").write_text(
        "Form W-2\nEmployer: YMCA\nBox 1: 4626.76\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "db"
    files_indexed, failures = run_add(root, db_path=db_path, allow_cloud=True)
    assert files_indexed == 3
    assert failures == 0

    slug = resolve_silo_by_path(db_path, root)
    assert slug is not None

    out = run_ask(
        archetype_id=None,
        query="box 2 deloitte 2025",
        db_path=db_path,
        silo=slug,
        no_color=True,
        use_reranker=False,
    )
    assert "4,723.31" in out
    assert "field_code=w2_box_2_federal_income_tax_withheld" in out
    assert "Federal_Colorado_deloitte2025.txt" in out

    taxes_paid = run_ask(
        archetype_id=None,
        query="how much did i pay in taxes at deloitte in 2025",
        db_path=db_path,
        silo=slug,
        no_color=True,
        use_reranker=False,
    )
    assert "4,723.31" in taxes_paid
    assert "Interpreting 'taxes paid' as federal income tax withheld (W-2 Box 2)." in taxes_paid

    out_2024 = run_ask(
        archetype_id=None,
        query="box 2 deloitte 2024",
        db_path=db_path,
        silo=slug,
        no_color=True,
        use_reranker=False,
    )
    assert "4,000.00" in out_2024
    assert "4723.31" not in out_2024
