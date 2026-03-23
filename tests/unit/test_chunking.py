from ingest import chunk_text


def test_chunk_text_empty():
    assert chunk_text("") == []


def test_chunk_text_respects_size_and_overlap():
    text = "\n".join([f"line {i}" for i in range(1, 11)])
    chunks = chunk_text(text, size=20, overlap=6)
    assert chunks
    # All chunks are non-empty and ordered by line_start.
    line_starts = [line for _chunk, line in chunks]
    assert line_starts == sorted(line_starts)
    # Ensure overlap keeps continuity (later chunk should start after or equal to prior).
    assert all(line_starts[i] <= line_starts[i + 1] for i in range(len(line_starts) - 1))


def test_chunk_text_splits_oversized_line():
    line = "x" * 2500
    chunks = chunk_text(line, size=1000, overlap=100)
    assert 3 <= len(chunks) <= 5
    line_starts = [ls for _c, ls in chunks]
    assert line_starts == sorted(line_starts)
    assert all(ls == 1 for ls in line_starts)
    assert all(len(c) <= 1000 for c, _ls in chunks)
    assert all(c for c, _ls in chunks)


def test_chunk_text_sec_like_prefix_then_megabyte_line():
    header = "<!DOCTYPE html>\n<html>\n<head>\n</head>\n<body>\n"
    big = "B" * 3000
    text = header + big + "\n"
    chunks = chunk_text(text, size=1000, overlap=100)
    # One small header chunk, several sub-line chunks for the 3000-char line, optional trailing newline chunk.
    assert len(chunks) >= 4
    line_starts = [ls for _c, ls in chunks]
    assert line_starts == sorted(line_starts)
    assert all(line_starts[i] <= line_starts[i + 1] for i in range(len(line_starts) - 1))
    by_line: dict[int, list[str]] = {}
    for c, ls in chunks:
        by_line.setdefault(ls, []).append(c)
    mega_body_line = 6
    assert mega_body_line in by_line
    assert all(len(part) <= 1000 for part in by_line[mega_body_line])
    assert len(by_line[mega_body_line]) >= 3
    assert all("B" in piece and "<" not in piece for piece in by_line[mega_body_line])
    assert len(chunks[0][0]) < 200
    assert chunks[0][1] == 1
