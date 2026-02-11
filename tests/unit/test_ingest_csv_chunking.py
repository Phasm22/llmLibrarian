import zipfile
from pathlib import Path

from ingest import process_one_file, process_zip_to_chunks


def test_process_one_file_csv_emits_row_chunks(tmp_path: Path):
    p = tmp_path / "rankings.csv"
    p.write_text(
        '"Rank","Restaurant","Sales"\n'
        '"1","Carmine\'s (Times Square)",39080335\n'
        '"2","The Boathouse Orlando",35218364\n',
        encoding="utf-8",
    )

    chunks = process_one_file(p, "code")
    assert len(chunks) == 2
    docs = [c[1] for c in chunks]
    metas = [c[2] for c in chunks]
    assert docs[0].startswith("CSV row 1:")
    assert "Rank=1" in docs[0]
    assert "Restaurant=Carmine's (Times Square)" in docs[0]
    assert metas[0]["row_number"] == 1
    assert metas[0]["line_start"] == 2


def test_process_zip_to_chunks_csv_uses_row_chunking(tmp_path: Path):
    zpath = tmp_path / "bundle.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(
            "data/Top250.csv",
            '"Rank","Restaurant"\n'
            '"1","Aster Grill"\n'
            '"2","Blue Harbor"\n',
        )

    chunks = process_zip_to_chunks(
        zip_path=zpath,
        include=["*.csv"],
        exclude=[],
        max_archive_bytes=10 * 1024 * 1024,
        max_file_bytes=10 * 1024 * 1024,
        max_files_per_zip=100,
        max_extracted_per_zip=10 * 1024 * 1024,
    )
    assert len(chunks) == 2
    docs = [c[1] for c in chunks]
    assert docs[0].startswith("CSV row 1:")
    assert "Rank=1" in docs[0]
    assert "Restaurant=Aster Grill" in docs[0]
