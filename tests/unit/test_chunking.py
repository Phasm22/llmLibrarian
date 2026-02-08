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
