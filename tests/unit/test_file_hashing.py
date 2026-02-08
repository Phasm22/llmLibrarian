from ingest import get_file_hash


def test_get_file_hash_changes_with_content(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("hello", encoding="utf-8")
    h1 = get_file_hash(p)
    p.write_text("hello world", encoding="utf-8")
    h2 = get_file_hash(p)
    assert h1
    assert h2
    assert h1 != h2
