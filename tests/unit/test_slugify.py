from state import slugify, resolve_silo_prefix


def test_slugify_appends_hash():
    slug = slugify("Become a Linear Algebra Master", "/tmp/foo")
    assert slug.startswith("become-a-linear-algebra-master-")
    assert len(slug.rsplit("-", 1)[-1]) == 8


def test_slugify_differs_by_path():
    slug_a = slugify("Project", "/tmp/a")
    slug_b = slugify("Project", "/tmp/b")
    assert slug_a != slug_b


def test_slugify_with_spaces():
    slug = slugify("Become a Linear Algebra Master", "/Users/tjm4/Desktop/Become a Linear Algebra Master")
    assert "become-a-linear-algebra-master" in slug


def test_resolve_silo_prefix():
    # Simulate registry with two slugs; only one should match prefix.
    db_path = "/tmp/llmli_db"
    # This test is light: just ensure function handles no registry gracefully.
    assert resolve_silo_prefix(db_path, "become-a") is None
