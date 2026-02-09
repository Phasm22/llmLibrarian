from pathlib import Path

from ingest import collect_files


def _collect(root: Path, **kwargs):
    return collect_files(
        root=root,
        include=kwargs.get("include", ["*.txt", "*.pdf", "*.docx", "*.xlsx", "*.pptx", "*.zip"]),
        exclude=kwargs.get("exclude", []),
        max_depth=kwargs.get("max_depth", 10),
        max_file_bytes=kwargs.get("max_file_bytes", 10 * 1024 * 1024),
        follow_symlinks=kwargs.get("follow_symlinks", False),
    )


def test_collect_files_detects_expected_kinds(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "c.docx").write_bytes(b"x")
    (tmp_path / "d.xlsx").write_bytes(b"x")
    (tmp_path / "e.pptx").write_bytes(b"x")
    (tmp_path / "f.zip").write_bytes(b"PK\x03\x04")
    out = _collect(tmp_path)
    kinds = {kind for _path, kind in out}
    assert {"code", "pdf", "docx", "xlsx", "pptx", "zip"}.issubset(kinds)


def test_collect_files_applies_include_patterns(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    (tmp_path / "b.md").write_text("y", encoding="utf-8")
    out = collect_files(tmp_path, include=["*.md"], exclude=[], max_depth=10, max_file_bytes=1024)
    assert len(out) == 1
    assert out[0][0].name == "b.md"


def test_collect_files_applies_exclude_patterns(tmp_path):
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
    (tmp_path / "skip.txt").write_text("x", encoding="utf-8")
    out = collect_files(
        tmp_path,
        include=["*.txt"],
        exclude=["skip.txt"],
        max_depth=10,
        max_file_bytes=1024,
    )
    names = {p.name for p, _k in out}
    assert "keep.txt" in names
    assert "skip.txt" not in names


def test_collect_files_respects_max_depth(tmp_path):
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "deep.txt").write_text("x", encoding="utf-8")
    out = collect_files(tmp_path, include=["*.txt"], exclude=[], max_depth=1, max_file_bytes=1024)
    assert out == []


def test_collect_files_skips_files_over_size_limit(tmp_path):
    small = tmp_path / "small.txt"
    large = tmp_path / "large.txt"
    small.write_text("ok", encoding="utf-8")
    large.write_bytes(b"x" * 4096)
    out = collect_files(tmp_path, include=["*.txt"], exclude=[], max_depth=10, max_file_bytes=100)
    names = {p.name for p, _k in out}
    assert "small.txt" in names
    assert "large.txt" not in names


def test_collect_files_skips_hidden_files(tmp_path):
    (tmp_path / ".secret.txt").write_text("hidden", encoding="utf-8")
    out = collect_files(tmp_path, include=["*.txt"], exclude=[], max_depth=10, max_file_bytes=1024)
    assert out == []


def test_collect_files_skips_symlink_when_follow_symlinks_false(tmp_path):
    target = tmp_path / "target.txt"
    link = tmp_path / "link.txt"
    target.write_text("x", encoding="utf-8")
    link.symlink_to(target)
    out = collect_files(tmp_path, include=["*.txt"], exclude=[], max_depth=10, max_file_bytes=1024, follow_symlinks=False)
    names = {p.name for p, _k in out}
    assert "target.txt" in names
    assert "link.txt" not in names


def test_collect_files_includes_symlink_when_follow_symlinks_true(tmp_path):
    target = tmp_path / "target.txt"
    link = tmp_path / "link.txt"
    target.write_text("x", encoding="utf-8")
    link.symlink_to(target)
    out = collect_files(tmp_path, include=["*.txt"], exclude=[], max_depth=10, max_file_bytes=1024, follow_symlinks=True)
    names = {p.name for p, _k in out}
    assert "target.txt" in names
    assert "link.txt" in names


def test_collect_files_skips_excluded_directory(tmp_path):
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    (node_modules / "a.txt").write_text("x", encoding="utf-8")
    out = collect_files(tmp_path, include=["*.txt"], exclude=["node_modules/"], max_depth=10, max_file_bytes=1024)
    assert out == []


def test_collect_files_handles_permission_error(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    def raise_permission(self):
        raise PermissionError("no access")

    monkeypatch.setattr(Path, "iterdir", raise_permission)
    out = collect_files(root, include=["*.txt"], exclude=[], max_depth=10, max_file_bytes=1024)
    assert out == []


def test_collect_files_handles_os_error(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    def raise_oserror(self):
        raise OSError("boom")

    monkeypatch.setattr(Path, "iterdir", raise_oserror)
    out = collect_files(root, include=["*.txt"], exclude=[], max_depth=10, max_file_bytes=1024)
    assert out == []
