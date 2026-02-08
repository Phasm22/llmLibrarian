from state import slugify


def test_slugify_appends_hash():
    slug = slugify("Become a Linear Algebra Master", "/tmp/foo")
    assert slug.startswith("become-a-linear-algebra-master-")
    assert len(slug.rsplit("-", 1)[-1]) == 8


def test_slugify_differs_by_path():
    slug_a = slugify("Project", "/tmp/a")
    slug_b = slugify("Project", "/tmp/b")
    assert slug_a != slug_b
