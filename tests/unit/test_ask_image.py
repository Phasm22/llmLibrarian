"""ask_image answers detail questions the stored summary cannot.

The ingest summary is 1-2 sentences written before anyone asks anything, so a
follow-up about one region of a photo has to re-read the pixels. Resolution is
manifest-only on purpose: the tool reads bytes off disk, so accepting an
arbitrary path would turn a retrieval tool into local file read.
"""

from __future__ import annotations

import pytest

import mcp_server


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
    monkeypatch.setattr("silo_audit.load_manifest", lambda _db: data)
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
    monkeypatch.setattr("silo_audit.load_manifest", lambda _db: data)
    path, candidates, err = mcp_server._resolve_indexed_image("IMG.jpeg", None)
    assert path is None and len(candidates) == 2 and "multiple" in err


def test_silo_scopes_resolution(monkeypatch, tmp_path):
    a, b = tmp_path / "a" / "IMG.jpeg", tmp_path / "b" / "IMG.jpeg"
    data = {"silos": {
        "s1": {"files": {str(a): {}}},
        "s2": {"files": {str(b): {}}},
    }}
    monkeypatch.setattr("silo_audit.load_manifest", lambda _db: data)
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
