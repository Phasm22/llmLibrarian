from ingest import run_add
from query.core import run_ask
from state import resolve_silo_by_path

TOP10_W2_REAL_LIFE_HARNESS = [
    {
        "query": "how much did i make in 2025",
        "expected_value": "60,000.00",
    },
    {
        "query": "how much did i make at acme in 2025",
        "expected_value": "50,000.00",
        "not_contains": "60,000.00",
    },
    {
        "query": "what were my w-2 wages in 2025",
        "expected_value": "60,000.00",
    },
    {
        "query": "how much federal income tax was withheld in 2025",
        "expected_value": "7,700.00",
    },
    {
        "query": "how much did i pay in taxes at acme in 2025",
        "expected_value": "6,500.00",
        "not_contains": "7,700.00",
    },
    {
        "query": "what were my payroll taxes in 2025",
        "expected_value": "4,590.00",
    },
    {
        "query": "what was my social security and medicare withholding in 2025",
        "expected_value": "4,590.00",
    },
    {
        "query": "how much state tax was withheld in 2025",
        "expected_value": "1,700.00",
    },
    {
        "query": "box 2 acme 2025",
        "expected_value": "6,500.00",
        "not_contains": "7,700.00",
    },
    {
        "query": "box 17 acme 2025",
        "expected_value": "1,400.00",
        "not_contains": "1,700.00",
    },
]


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


def test_tax_deterministic_top10_real_life_w2_harness(tmp_path):
    root = tmp_path / "Tax"
    (root / "2025").mkdir(parents=True)

    (root / "2025" / "acme-w2-2025.txt").write_text(
        (
            "Form W-2\n"
            "Employer: Acme\n"
            "Box 1: 50000.00\n"
            "Box 2: 6500.00\n"
            "Box 3: 50000.00\n"
            "Box 4: 3100.00\n"
            "Box 5: 50000.00\n"
            "Box 6: 725.00\n"
            "Box 17: 1400.00\n"
        ),
        encoding="utf-8",
    )
    (root / "2025" / "zenith-w2-2025.txt").write_text(
        (
            "Form W-2\n"
            "Employer: Zenith\n"
            "Box 1: 10000.00\n"
            "Box 2: 1200.00\n"
            "Box 3: 10000.00\n"
            "Box 4: 620.00\n"
            "Box 5: 10000.00\n"
            "Box 6: 145.00\n"
            "Box 17: 300.00\n"
        ),
        encoding="utf-8",
    )

    db_path = tmp_path / "db"
    files_indexed, failures = run_add(root, db_path=db_path, allow_cloud=True)
    assert files_indexed == 2
    assert failures == 0

    slug = resolve_silo_by_path(db_path, root)
    assert slug is not None

    fallback_markers = [
        "low confidence: query is weakly related to indexed content.",
        "unfortunately, i couldn't find",
        "you are looking for information related to",
        "caveat:",
    ]
    for case in TOP10_W2_REAL_LIFE_HARNESS:
        out = run_ask(
            archetype_id=None,
            query=case["query"],
            db_path=db_path,
            silo=slug,
            no_color=True,
            use_reranker=False,
        )
        # Contract: deterministic resolved value or deterministic abstain.
        first_line = out.splitlines()[0] if out else ""
        assert first_line
        assert "Metric: field_code=" not in out
        assert case["expected_value"] in out

        if case.get("not_contains"):
            assert case["not_contains"] not in out

        low = out.lower()
        for marker in fallback_markers:
            assert marker not in low
