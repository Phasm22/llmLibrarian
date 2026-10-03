"""ask_image answers detail questions the stored summary cannot.

The ingest summary is 1-2 sentences written before anyone asks anything, so a
follow-up about one region of a photo has to re-read the pixels. Resolution is
manifest-only on purpose: the tool reads bytes off disk, so accepting an
arbitrary path would turn a retrieval tool into local file read.
"""

from __future__ import annotations

import pytest

import mcp_server


@pytest.fixture(autouse=True)
def _no_private_silos(monkeypatch):
    monkeypatch.setattr("state.private_silo_slugs", lambda _db: [])


@pytest.fixture
def manifest(monkeypatch, tmp_path):
    img = tmp_path / "IMG_9383.jpeg"
    img.write_bytes(b"\xff\xd8\xff-not-a-real-jpeg")
    other = tmp_path / "notes.md"
    other.write_text("text")
    data = {
        "silos": {
            "photos-1": {"path": str(tmp_path), "files": {str(img): {}, str(other): {}}},
        }
    }
    monkeypatch.setattr("file_registry._read_file_manifest", lambda _db: data)
    return img


def test_resolves_indexed_image_by_bare_filename(manifest):
    path, candidates, err = mcp_server._resolve_indexed_image("IMG_9383.jpeg", None)
    assert err is None and path == str(manifest)


def test_rejects_unindexed_path(manifest):
    path, _c, err = mcp_server._resolve_indexed_image("/etc/passwd", None)
    assert path is None and "no indexed image" in err


def test_rejects_indexed_non_image(manifest):
    path, _c, err = mcp_server._resolve_indexed_image("notes.md", None)
    assert path is None and err


def test_ambiguous_filename_returns_candidates(monkeypatch, tmp_path):
    a, b = tmp_path / "a" / "IMG.jpeg", tmp_path / "b" / "IMG.jpeg"
    data = {"silos": {
        "s1": {"files": {str(a): {}}},
        "s2": {"files": {str(b): {}}},
    }}
    monkeypatch.setattr("file_registry._read_file_manifest", lambda _db: data)
    path, candidates, err = mcp_server._resolve_indexed_image("IMG.jpeg", None)
    assert path is None and len(candidates) == 2 and "multiple" in err


def test_silo_scopes_resolution(monkeypatch, tmp_path):
    a, b = tmp_path / "a" / "IMG.jpeg", tmp_path / "b" / "IMG.jpeg"
    data = {"silos": {
        "s1": {"files": {str(a): {}}},
        "s2": {"files": {str(b): {}}},
    }}
    monkeypatch.setattr("file_registry._read_file_manifest", lambda _db: data)
    path, _c, err = mcp_server._resolve_indexed_image("IMG.jpeg", "s2")
    assert err is None and path == str(b)


def test_ask_image_returns_answer(manifest, monkeypatch):
    monkeypatch.setattr(
        "processors.answer_image_question",
        lambda b, p, q: (f"answer to {q}", "gemma4:12b"),
    )
    out = mcp_server.ask_image(file="IMG_9383.jpeg", question="what is bottom left?")
    assert out["status"] == "ok"
    assert out["answer"] == "answer to what is bottom left?"
    assert out["vision_model"] == "gemma4:12b"


def test_ask_image_surfaces_vision_failure(manifest, monkeypatch):
    def _boom(b, p, q):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("processors.answer_image_question", _boom)
    out = mcp_server.ask_image(file="IMG_9383.jpeg", question="q")
    assert out["status"] == "error" and "model unavailable" in out["error"]


def test_ask_image_rejects_unindexed(manifest):
    out = mcp_server.ask_image(file="/etc/passwd", question="q")
    assert out["status"] == "error" and "no indexed image" in out["error"]


def test_ask_image_requires_a_question(manifest):
    out = mcp_server.ask_image(file="IMG_9383.jpeg")
    assert out["status"] == "error" and "question" in out["error"]


def test_several_files_go_to_one_vision_call(monkeypatch, tmp_path):
    """2026-10-02: the caller asked about the third of three candidate photos and
    gave up. Several images now go in one call (one model load, not three)."""
    imgs = [tmp_path / f"DSCN27{n}.jpg" for n in (63, 69, 71)]
    for img in imgs:
        img.write_bytes(b"\xff\xd8\xff")
    data = {"silos": {"car-1": {"files": {str(i): {} for i in imgs}}}}
    monkeypatch.setattr("file_registry._read_file_manifest", lambda _db: data)
    calls = []
    monkeypatch.setattr(
        "processors.answer_images_question",
        lambda images, q: calls.append([p for _b, p in images]) or ("BMW, per DSCN2763.jpg", "gemma4:12b"),
    )

    out = mcp_server.ask_image(question="what car is this?", files=[i.name for i in imgs])

    assert out["status"] == "ok" and out["answer"].startswith("BMW")
    assert calls == [[str(i) for i in imgs]]
    assert out["source_files"] == [str(i) for i in imgs]


def test_no_file_locates_best_matches(monkeypatch, tmp_path):
    imgs = [tmp_path / "a.jpg", tmp_path / "b.jpg"]
    for img in imgs:
        img.write_bytes(b"\xff\xd8\xff")
    data = {"silos": {"car-1": {"files": {str(i): {} for i in imgs}}}}
    monkeypatch.setattr("file_registry._read_file_manifest", lambda _db: data)
    db = tmp_path / "db"
    db.mkdir()
    monkeypatch.setattr(mcp_server, "_DB_PATH", str(db))
    monkeypatch.setattr(mcp_server, "_release_chroma", lambda: None)
    import query.retrieve_locked as retrieve_locked

    monkeypatch.setattr(
        retrieve_locked,
        "execute_retrieve_chroma_phase",
        lambda **kw: {"chunks": [
            {"source": str(imgs[1]), "text": "t"},
            {"source": "/notes/car.md", "text": "t"},
            {"source": str(imgs[1]), "text": "t2"},
            {"source": str(imgs[0]), "text": "t"},
        ]},
    )
    seen = []
    monkeypatch.setattr("processors.answer_images_question", lambda images, q: seen.append([p for _b, p in images]) or ("ok", "m"))

    out = mcp_server.ask_image(question="what car is this?", silo="car-1")

    assert out["status"] == "ok" and out["located_by"]
    assert seen == [[str(imgs[1]), str(imgs[0])]]  # best match first, deduped, non-images skipped
