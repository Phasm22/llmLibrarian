import zipfile

from ingest import process_zip_to_chunks


def test_zip_symlink_entry_is_skipped(tmp_path):
    zip_path = tmp_path / "symlink.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        # Simulate a symlink entry using POSIX mode bits.
        zi = zipfile.ZipInfo("link.txt")
        zi.create_system = 3
        zi.external_attr = 0o120777 << 16
        z.writestr(zi, "secret")
        z.writestr("good.txt", "hello")

    chunks = process_zip_to_chunks(
        zip_path=zip_path,
        include=["*.txt"],
        exclude=[],
        max_archive_bytes=10 * 1024 * 1024,
        max_file_bytes=1024 * 1024,
        max_files_per_zip=20,
        max_extracted_per_zip=10 * 1024 * 1024,
    )

    docs = [doc for _cid, doc, _meta in chunks]
    assert any("hello" in doc for doc in docs)
    assert not any("secret" in doc for doc in docs)
