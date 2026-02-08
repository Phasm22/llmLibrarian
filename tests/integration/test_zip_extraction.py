import zipfile

from ingest import process_zip_to_chunks


def test_zip_extraction_skips_traversal(tmp_path):
    zip_path = tmp_path / "sample.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("../evil.txt", "nope")
        z.writestr("good.txt", "hello")

    chunks = process_zip_to_chunks(
        zip_path,
        include=["*.txt"],
        exclude=[],
        max_archive_bytes=10 * 1024 * 1024,
        max_file_bytes=1024 * 1024,
        max_files_per_zip=10,
        max_extracted_per_zip=10 * 1024 * 1024,
    )
    docs = [doc for _cid, doc, _meta in chunks]
    assert any("hello" in doc for doc in docs)
    assert not any("nope" in doc for doc in docs)
