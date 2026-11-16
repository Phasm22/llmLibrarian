from ingest import _chunk_hash, _clone_chunks_from_existing_silo


class _FakeCloneCollection:
    def __init__(self, documents, metadatas):
        self._documents = documents
        self._metadatas = metadatas

    def get(self, where=None, include=None):
        return {"documents": list(self._documents), "metadatas": list(self._metadatas)}


def test_clone_chunks_assigns_distinct_ids_for_duplicate_text():
    """Two chunks in the same file that share identical text (duplicate chunk_hash,
    e.g. a repeated table header or boilerplate line) must not collapse onto the
    same cloned chunk id -- that silently drops one of them when Chroma upserts.
    """
    source_path = "/docs/report.pdf"
    mtime = 1700000000.0
    duplicate_text = "Page header"
    docs = [duplicate_text, "unique body text", duplicate_text]
    metas = [
        {"source": source_path, "mtime": mtime, "chunk_hash": _chunk_hash(duplicate_text), "chunk_index": 0},
        {"source": source_path, "mtime": mtime, "chunk_hash": _chunk_hash("unique body text"), "chunk_index": 1},
        {"source": source_path, "mtime": mtime, "chunk_hash": _chunk_hash(duplicate_text), "chunk_index": 2},
    ]
    coll = _FakeCloneCollection(docs, metas)

    cloned = _clone_chunks_from_existing_silo(
        collection=coll,
        from_silo="source-silo",
        source_path=source_path,
        target_silo="target-silo",
    )

    ids = [cid for cid, _doc, _meta in cloned]
    assert len(ids) == 3
    assert len(set(ids)) == 3, "duplicate-content chunks collided onto the same clone id"


def test_clone_chunks_id_is_stable_across_repeated_calls():
    """Re-cloning the same file (e.g. a later `add` run) must reproduce the same
    ids for its chunks, so repeated clones upsert in place instead of drifting.
    """
    source_path = "/docs/report.pdf"
    mtime = 1700000000.0
    docs = ["first chunk", "second chunk"]
    metas = [
        {"source": source_path, "mtime": mtime, "chunk_hash": _chunk_hash("first chunk"), "chunk_index": 0},
        {"source": source_path, "mtime": mtime, "chunk_hash": _chunk_hash("second chunk"), "chunk_index": 1},
    ]
    coll = _FakeCloneCollection(docs, metas)

    first = _clone_chunks_from_existing_silo(
        collection=coll, from_silo="source-silo", source_path=source_path, target_silo="target-silo",
    )
    second = _clone_chunks_from_existing_silo(
        collection=coll, from_silo="source-silo", source_path=source_path, target_silo="target-silo",
    )

    assert [cid for cid, _doc, _meta in first] == [cid for cid, _doc, _meta in second]
