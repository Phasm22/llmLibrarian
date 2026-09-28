import sys
from types import SimpleNamespace

import cli


class _FakeCollection:
    def __init__(self, raise_on_delete: bool = False):
        self.raise_on_delete = raise_on_delete
        self.deleted_where = None
        self.all_deleted_where: list[dict] = []

    def delete(self, where):
        if self.raise_on_delete:
            raise RuntimeError("delete failed")
        self.deleted_where = where
        self.all_deleted_where.append(where)


class _FakeClient:
    def __init__(self, collection):
        self.collection = collection

    def get_or_create_collection(self, **_kwargs):
        return self.collection


def _patch_chromadb(monkeypatch, collection):
    import chroma_client
    monkeypatch.setattr(chroma_client, "get_client", lambda db_path: _FakeClient(collection))


def test_cmd_rm_requires_silo(capsys):
    rc = cli.cmd_rm(SimpleNamespace(silo=None, db=None))
    assert rc == 1
    assert "remove requires silo name" in capsys.readouterr().err


def test_cmd_rm_removes_registered_silo(monkeypatch, capsys):
    deleted = {}
    fake_coll = _FakeCollection()
    _patch_chromadb(monkeypatch, fake_coll)
    monkeypatch.setattr("state.remove_silo", lambda _db, _name: "tax-1234abcd")
    monkeypatch.setattr("state.slugify", lambda raw: f"slug-{raw}")
    monkeypatch.setattr("state.resolve_silo_by_path", lambda _db, _raw: None)
    monkeypatch.setattr("state.resolve_silo_prefix", lambda _db, _raw: None)
    monkeypatch.setattr("state.remove_manifest_silo", lambda _db, slug: deleted.setdefault("manifest", slug))

    rc = cli.cmd_rm(SimpleNamespace(silo="Tax", db="/tmp/db"))
    assert rc == 0
    assert fake_coll.deleted_where == {"silo": "tax-1234abcd"}
    assert deleted["manifest"] == "tax-1234abcd"
    assert "Removed silo: tax-1234abcd" in capsys.readouterr().out


def test_cmd_rm_cleans_orphan_slug_when_not_in_registry(monkeypatch, capsys):
    """Orphan recovery: the literal name is tried first, the slugified form too.

    This used to assert that only `slugify(raw)` was used. That was the bug: a
    real orphan is an already-hashed slug, and re-slugifying it appends a second
    hash ("alpha-deb12c9c" -> "alpha-deb12c9c-9b411e47"), so the delete matched
    nothing while still reporting success. Both candidates are attempted now;
    a `where` that matches nothing is a no-op.
    """
    deleted = []
    fake_coll = _FakeCollection()
    _patch_chromadb(monkeypatch, fake_coll)
    monkeypatch.setattr("state.remove_silo", lambda _db, _name: None)
    monkeypatch.setattr("state.slugify", lambda raw: "derived-slug")
    monkeypatch.setattr("state.resolve_silo_by_path", lambda _db, _raw: None)
    monkeypatch.setattr("state.resolve_silo_prefix", lambda _db, _raw: None)
    monkeypatch.setattr("state.remove_manifest_silo", lambda _db, slug: deleted.append(slug))

    rc = cli.cmd_rm(SimpleNamespace(silo="unknown", db="/tmp/db"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "unknown" in out
    attempted = [w["silo"] for w in fake_coll.all_deleted_where]
    assert attempted[0] == "unknown", "the literal slug must be tried first"
    assert "derived-slug" in attempted, "the slugified form is still tried"
    assert deleted[0] == "unknown"
    assert "derived-slug" in deleted


def test_cmd_rm_path_with_spaces_resolves_to_registered_slug(monkeypatch, tmp_path):
    fake_coll = _FakeCollection()
    _patch_chromadb(monkeypatch, fake_coll)
    observed = {}
    monkeypatch.setattr("state.resolve_silo_by_path", lambda _db, _raw: "resolved-by-path")
    monkeypatch.setattr("state.resolve_silo_prefix", lambda _db, _raw: None)
    monkeypatch.setattr("state.slugify", lambda raw: f"slug-{raw}")
    monkeypatch.setattr("state.remove_silo", lambda _db, name: observed.setdefault("remove_arg", name) or "resolved-by-path")
    monkeypatch.setattr("state.remove_manifest_silo", lambda _db, _slug: None)

    path_with_spaces = tmp_path / "Become a Linear Algebra Master"
    path_with_spaces.mkdir()
    # argparse can pass list fragments when user forgets quoting.
    args = SimpleNamespace(silo=str(path_with_spaces).split(" "), db="/tmp/db")
    rc = cli.cmd_rm(args)
    assert rc == 0
    assert observed["remove_arg"] == "resolved-by-path"
    assert fake_coll.deleted_where == {"silo": "resolved-by-path"}


def test_cmd_rm_continues_when_db_delete_fails(monkeypatch, capsys):
    fake_coll = _FakeCollection(raise_on_delete=True)
    _patch_chromadb(monkeypatch, fake_coll)
    deleted = {}
    monkeypatch.setattr("state.remove_silo", lambda _db, _name: "tax-1234abcd")
    monkeypatch.setattr("state.slugify", lambda raw: f"slug-{raw}")
    monkeypatch.setattr("state.resolve_silo_by_path", lambda _db, _raw: None)
    monkeypatch.setattr("state.resolve_silo_prefix", lambda _db, _raw: None)
    monkeypatch.setattr("state.remove_manifest_silo", lambda _db, slug: deleted.setdefault("manifest", slug))

    rc = cli.cmd_rm(SimpleNamespace(silo="Tax", db="/tmp/db"))
    err = capsys.readouterr().err
    assert rc == 0
    assert "could not delete chunks from DB" in err
    assert deleted["manifest"] == "tax-1234abcd"
