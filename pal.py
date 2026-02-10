#!/usr/bin/env python3
"""
pal — Agent CLI that orchestrates tools (e.g. llmli). Control-plane: route, registry.
Not a replacement for llmli; pal add/ask/ls/log delegate to llmli. Use pal tool llmli ... for passthrough.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import threading
from pathlib import Path

PAL_HOME = Path(os.environ.get("PAL_HOME", os.path.expanduser("~/.pal")))
REGISTRY_PATH = PAL_HOME / "registry.json"


def _ensure_pal_home() -> None:
    PAL_HOME.mkdir(parents=True, exist_ok=True)


def _read_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"sources": [], "tools": {"llmli": {"last_ok": None, "last_failures": None}}}
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sources": [], "tools": {"llmli": {"last_ok": None, "last_failures": None}}}


def _write_registry(data: dict) -> None:
    _ensure_pal_home()
    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _run_llmli(args: list[str]) -> int:
    """Run llmli as subprocess; return exit code."""
    root = Path(__file__).resolve().parent
    env = os.environ.copy()
    venv_llmli = root / ".venv" / "bin" / "llmli"
    if venv_llmli.exists():
        cmd = [str(venv_llmli)] + args
    else:
        cmd = [sys.executable, str(root / "cli.py")] + args
    r = subprocess.run(cmd, env=env)
    return r.returncode


def _ensure_src_on_path() -> None:
    root = Path(__file__).resolve().parent
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


try:  # optional dependency for watch mode
    from watchdog.observers import Observer  # type: ignore
    from watchdog.events import FileSystemEventHandler  # type: ignore
except Exception:  # pragma: no cover - handled by runtime error message
    Observer = None  # type: ignore[assignment]
    FileSystemEventHandler = object  # type: ignore[misc, assignment]


def _parse_env_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return None


def _get_git_root() -> Path | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    root = (r.stdout or "").strip()
    return Path(root).resolve() if root else None


def _pyproject_has_name(root: Path, expected: str) -> bool:
    path = root / "pyproject.toml"
    if not path.exists():
        return False
    in_project = False
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    in_project = line == "[project]"
                    continue
                if in_project and line.startswith("name"):
                    parts = line.split("=", 1)
                    if len(parts) != 2:
                        continue
                    name = parts[1].strip().strip('"').strip("'")
                    return name == expected
    except Exception:
        return False
    return False


def _is_dev_repo_at_root(root: Path) -> bool:
    checks = 0
    if (root / "pal.py").exists():
        checks += 1
    if (root / "cli.py").exists():
        checks += 1
    if _pyproject_has_name(root, "llmLibrarian"):
        checks += 1
    return checks >= 2


def is_dev_repo() -> bool:
    root = _get_git_root()
    if root is None:
        return False
    return _is_dev_repo_at_root(root)


def _should_require_self_silo() -> bool:
    env_val = os.environ.get("LLMLIBRARIAN_REQUIRE_SELF_SILO")
    parsed = _parse_env_bool(env_val)
    if env_val is None:
        return is_dev_repo()
    if parsed is None:
        print("Warning: invalid LLMLIBRARIAN_REQUIRE_SELF_SILO; treating as disabled.", file=sys.stderr)
        return False
    return parsed


def _llmli_registry_path(db_path: str | Path) -> Path:
    p = Path(db_path).resolve()
    if p.is_dir():
        return p / "llmli_registry.json"
    return p.parent / "llmli_registry.json"


def _read_llmli_registry(db_path: str | Path) -> dict:
    path = _llmli_registry_path(db_path)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _resolve_llmli_silo_by_path(registry: dict, path: Path) -> str | None:
    target = str(path.resolve())
    for slug, data in registry.items():
        raw = (data or {}).get("path")
        if not raw:
            continue
        try:
            candidate = str(Path(raw).resolve())
        except Exception:
            candidate = raw
        if candidate == target:
            return slug
    return None


def _llmli_updated_to_epoch(updated_iso: str | None) -> int | None:
    if not updated_iso:
        return None
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(updated_iso).timestamp())
    except Exception:
        return None


def _git_last_commit_ct(root: Path) -> int | None:
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--format=%ct"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    try:
        return int(out)
    except Exception:
        return None


def _git_is_dirty(root: Path) -> bool:
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return False
    if r.returncode != 0:
        return False
    return bool((r.stdout or "").strip())


def _upsert_self_source(reg: dict, repo_root: Path, last_index_mtime: int | None) -> dict:
    sources = reg.get("sources", []) or []
    repo_str = str(repo_root.resolve())
    filtered = [
        s
        for s in sources
        if not (
            (s.get("silo") == "__self__")
            or (s.get("path") == repo_str and s.get("name") == "self")
        )
    ]
    entry = {
        "path": repo_str,
        "tool": "llmli",
        "name": "self",
        "silo": "__self__",
    }
    if last_index_mtime is not None:
        entry["self_silo_last_index_mtime"] = int(last_index_mtime)
    filtered.append(entry)
    reg["sources"] = filtered
    return reg


def _get_self_index_mtime_from_registry(reg: dict) -> int | None:
    for s in reg.get("sources", []) or []:
        if s.get("silo") == "__self__" and s.get("path"):
            v = s.get("self_silo_last_index_mtime")
            try:
                return int(v)
            except Exception:
                return None
    return None


def _warn_self_silo_stale() -> None:
    print("Self-silo stale (repo changed since last index). Run `pal ensure-self`.", file=sys.stderr)


def _warn_self_silo_missing() -> None:
    print("Self-silo missing. Run `pal ensure-self`.", file=sys.stderr)


def _warn_self_silo_mismatch() -> None:
    print("Self-silo path mismatch. Run `pal ensure-self`.", file=sys.stderr)


def ensure_self_silo(force: bool = False) -> int:
    require_self = _should_require_self_silo()
    if not require_self:
        return 0
    repo_root = _get_git_root()
    if repo_root is None:
        print("Warning: unable to detect git root; skipping self-silo.", file=sys.stderr)
        return 0

    is_dev = _is_dev_repo_at_root(repo_root)
    db_path = os.environ.get("LLMLIBRARIAN_DB", "./my_brain_db")
    llmli_registry = _read_llmli_registry(db_path)
    repo_str = str(repo_root.resolve())
    existing_slug = _resolve_llmli_silo_by_path(llmli_registry, repo_root)
    self_entry = llmli_registry.get("__self__") if isinstance(llmli_registry, dict) else None
    self_path = (self_entry or {}).get("path")

    needs_reindex = False
    if self_entry and self_path and self_path != repo_str:
        needs_reindex = True
    if existing_slug and existing_slug != "__self__":
        needs_reindex = True

    if not self_entry or needs_reindex or self_path != repo_str:
        if not force:
            if not self_entry:
                _warn_self_silo_missing()
            else:
                _warn_self_silo_mismatch()
            return 0
        if self_entry and self_path and self_path != repo_str:
            _run_llmli(["rm", "__self__"])
        if existing_slug and existing_slug != "__self__":
            _run_llmli(["rm", existing_slug])
        llmli_args = ["add", "--silo", "__self__", "--display-name", "self"]
        if is_dev:
            llmli_args.append("--allow-cloud")
        llmli_args.append(repo_str)
        code = _run_llmli(llmli_args)
        if code != 0:
            print("Warning: failed to index self-silo.", file=sys.stderr)
            return code
        reg = _read_registry()
        reg = _upsert_self_source(reg, repo_root, int(time.time()))
        _write_registry(reg)
        return 0

    reg = _read_registry()
    last_index_mtime = _get_self_index_mtime_from_registry(reg)
    if last_index_mtime is None and self_entry:
        last_index_mtime = _llmli_updated_to_epoch(self_entry.get("updated"))
        reg = _upsert_self_source(reg, repo_root, last_index_mtime)
        _write_registry(reg)

    dirty = _git_is_dirty(repo_root)
    last_commit = _git_last_commit_ct(repo_root)
    stale = dirty or (
        last_index_mtime is not None and last_commit is not None and last_commit > last_index_mtime
    )
    if stale:
        _warn_self_silo_stale()
    return 0


class _SiloEventHandler(FileSystemEventHandler):
    def __init__(self, watcher: "SiloWatcher") -> None:
        super().__init__()
        self._watcher = watcher

    def on_modified(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._watcher.enqueue_update(event.src_path)

    def on_created(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._watcher.enqueue_update(event.src_path)

    def on_deleted(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._watcher.enqueue_delete(event.src_path)

    def on_moved(self, event) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        self._watcher.enqueue_delete(event.src_path)
        self._watcher.enqueue_update(event.dest_path)


class SiloWatcher:
    def __init__(
        self,
        root: Path,
        db_path: str | Path,
        interval: float = 10.0,
        debounce: float = 1.0,
        silo_slug: str = "__self__",
        allow_cloud: bool = False,
    ) -> None:
        if Observer is None:
            raise RuntimeError("watchdog is not installed. Install `watchdog` to use watch mode.")
        _ensure_src_on_path()
        from ingest import (
            update_single_file,
            remove_single_file,
            update_silo_counts,
            _read_file_manifest,
            _load_limits_config,
            collect_files,
            should_index,
            ADD_DEFAULT_INCLUDE,
            ADD_DEFAULT_EXCLUDE,
        )

        self.root = root.resolve()
        self.db_path = str(db_path)
        self.interval = float(interval)
        self.debounce = float(debounce)
        self.silo_slug = silo_slug
        self.allow_cloud = allow_cloud

        self._update_single_file = update_single_file
        self._remove_single_file = remove_single_file
        self._update_silo_counts = update_silo_counts
        self._read_manifest = _read_file_manifest
        self._load_limits_config = _load_limits_config
        self._collect_files = collect_files
        self._should_index = should_index
        self._include = ADD_DEFAULT_INCLUDE
        self._exclude = ADD_DEFAULT_EXCLUDE

        self._observer = Observer()
        self._handler = _SiloEventHandler(self)
        self._queue: dict[str, dict[str, float | str]] = {}
        self._queue_lock = threading.Lock()
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def _log(self, message: str) -> None:
        print(message)

    def enqueue_update(self, path: str) -> None:
        try:
            p = Path(path).resolve()
        except Exception:
            return
        try:
            if not p.is_relative_to(self.root):
                return
        except Exception:
            return
        if not self._should_index(str(p), self._include, self._exclude):
            return
        now = time.time()
        with self._queue_lock:
            self._queue[str(p)] = {"ts": now, "action": "update"}

    def enqueue_delete(self, path: str) -> None:
        try:
            p = Path(path).resolve()
        except Exception:
            return
        try:
            if not p.is_relative_to(self.root):
                return
        except Exception:
            return
        now = time.time()
        with self._queue_lock:
            existing = self._queue.get(str(p))
            if existing and existing.get("action") == "update":
                self._queue[str(p)] = {"ts": now, "action": "delete"}
            else:
                self._queue[str(p)] = {"ts": now, "action": "delete"}

    def _drain_due(self, now: float | None = None) -> int:
        if now is None:
            now = time.time()
        due: list[tuple[str, str]] = []
        with self._queue_lock:
            for path, meta in list(self._queue.items()):
                ts = float(meta.get("ts") or 0.0)
                action = str(meta.get("action") or "update")
                if now - ts >= self.debounce:
                    due.append((path, action))
                    del self._queue[path]
        if not due:
            return 0
        processed = 0
        for path, action in due:
            with self._lock:
                if action == "delete":
                    status, _ = self._remove_single_file(
                        path,
                        db_path=self.db_path,
                        silo_slug=self.silo_slug,
                        update_counts=True,
                    )
                    if status == "removed":
                        self._log(f"self: removed {path}")
                        processed += 1
                else:
                    status, _ = self._update_single_file(
                        path,
                        db_path=self.db_path,
                        silo_slug=self.silo_slug,
                        allow_cloud=self.allow_cloud,
                        follow_symlinks=False,
                        no_color=True,
                        update_counts=True,
                    )
                    if status == "updated":
                        self._log(f"self: updated {path}")
                        processed += 1
                    elif status == "removed":
                        self._log(f"self: removed {path}")
                        processed += 1
        return processed

    def _process_loop(self) -> None:
        while not self._stop.is_set():
            self._drain_due()
            time.sleep(0.2)

    def _reconcile_once(self) -> tuple[int, int, int]:
        max_file_bytes, max_depth, _max_archive_bytes, _max_files_per_zip, _max_extracted = self._load_limits_config()
        file_list = self._collect_files(
            self.root,
            self._include,
            self._exclude,
            max_depth,
            max_file_bytes,
            follow_symlinks=False,
        )
        current: dict[str, tuple[float, int]] = {}
        for p, _k in file_list:
            try:
                p_res = p.resolve()
                stat = p_res.stat()
                current[str(p_res)] = (stat.st_mtime, stat.st_size)
            except OSError:
                continue

        manifest = self._read_manifest(self.db_path)
        silo_manifest = (manifest.get("silos") or {}).get(self.silo_slug, {})
        manifest_files = (silo_manifest.get("files") or {}) if isinstance(silo_manifest, dict) else {}

        updated = 0
        removed = 0
        skipped = 0
        with self._lock:
            if isinstance(manifest_files, dict):
                for path_str in list(manifest_files.keys()):
                    if path_str not in current:
                        status, _ = self._remove_single_file(
                            path_str,
                            db_path=self.db_path,
                            silo_slug=self.silo_slug,
                            update_counts=False,
                        )
                        if status == "removed":
                            removed += 1
            for path_str, (mtime, size) in current.items():
                prev = manifest_files.get(path_str) if isinstance(manifest_files, dict) else None
                if prev and prev.get("mtime") == mtime and prev.get("size") == size:
                    continue
                status, _ = self._update_single_file(
                    path_str,
                    db_path=self.db_path,
                    silo_slug=self.silo_slug,
                    allow_cloud=self.allow_cloud,
                    follow_symlinks=False,
                    no_color=True,
                    update_counts=False,
                )
                if status == "updated":
                    updated += 1
                elif status == "removed":
                    removed += 1
                else:
                    skipped += 1
        if updated or removed:
            self._update_silo_counts(self.db_path, self.silo_slug)
        return (updated, removed, skipped)

    def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(self.interval)
            if self._stop.is_set():
                break
            updated, removed, skipped = self._reconcile_once()
            if updated or removed or skipped:
                self._log(f"Reconcile: +{updated} updated, -{removed} removed, {skipped} skipped")

    def run(self) -> None:
        self._log(f"Watching __self__ (interval={int(self.interval)}s, debounce={self.debounce}s). Ctrl+C to stop.")
        self._observer.schedule(self._handler, str(self.root), recursive=True)
        self._observer.start()
        worker = threading.Thread(target=self._process_loop, daemon=True)
        recon = threading.Thread(target=self._reconcile_loop, daemon=True)
        worker.start()
        recon.start()
        updated, removed, skipped = self._reconcile_once()
        if updated or removed or skipped:
            self._log(f"Reconcile: +{updated} updated, -{removed} removed, {skipped} skipped")
        try:
            while True:
                time.sleep(1.0)
        finally:
            self._stop.set()
            self._observer.stop()
            self._observer.join()


def cmd_add(args: argparse.Namespace) -> int:
    """Delegate to llmli add <path>; optionally record in pal registry. Cloud-sync paths blocked unless --allow-cloud."""
    path = Path(args.path).resolve()
    if not path.is_dir():
        print(f"Error: not a directory: {path}", file=sys.stderr)
        return 1
    llmli_args = ["add"]
    if getattr(args, "allow_cloud", False):
        llmli_args.append("--allow-cloud")
    llmli_args.append(str(path))
    code = _run_llmli(llmli_args)
    if code == 0:
        reg = _read_registry()
        sources = reg.get("sources", [])
        entry = {"path": str(path), "tool": "llmli", "name": path.name}
        if not any(s.get("path") == str(path) for s in sources):
            sources.append(entry)
            reg["sources"] = sources
            _write_registry(reg)
    return code


def cmd_ask(args: argparse.Namespace) -> int:
    """Delegate to llmli ask (unified by default). --in <silo> for scoped ask; --strict for conservative answers."""
    ensure_self_silo(force=False)
    llmli_args = ["ask"]
    if getattr(args, "unified", False):
        llmli_args.append("--unified")
    if getattr(args, "in_silo", None) and not getattr(args, "unified", False):
        llmli_args.extend(["--in", args.in_silo])
    if getattr(args, "strict", False):
        llmli_args.append("--strict")
    llmli_args.extend(args.query)
    return _run_llmli(llmli_args)


def cmd_ls(args: argparse.Namespace) -> int:
    """Aggregate view: delegate to llmli ls."""
    return _run_llmli(["ls"])


def cmd_log(args: argparse.Namespace) -> int:
    """Unified log view: delegate to llmli log --last."""
    return _run_llmli(["log", "--last"])


def cmd_capabilities(args: argparse.Namespace) -> int:
    """Delegate to llmli capabilities (supported file types and extractors)."""
    ensure_self_silo(force=False)
    return _run_llmli(["capabilities"])


def cmd_ensure_self(args: argparse.Namespace) -> int:
    """Ensure dev-mode self-silo exists; warn if stale."""
    return ensure_self_silo(force=True)


def cmd_inspect(args: argparse.Namespace) -> int:
    """Show silo details and top files by chunk count (llmli inspect)."""
    silo = getattr(args, "silo", None)
    if not silo:
        print("Error: inspect requires silo name. Example: pal inspect stuff", file=sys.stderr)
        return 1
    llmli_args = ["inspect", silo]
    top = getattr(args, "top", None)
    if top is not None:
        llmli_args.extend(["--top", str(top)])
    filt = getattr(args, "filter", None)
    if filt:
        llmli_args.extend(["--filter", filt])
    return _run_llmli(llmli_args)


def cmd_remove(args: argparse.Namespace) -> int:
    """Remove a silo (friendly alias for llmli remove)."""
    silo = getattr(args, "silo", None)
    if not silo:
        print("Error: remove requires silo name. Example: pal remove \"Tax\"", file=sys.stderr)
        return 1
    if isinstance(silo, list):
        name = " ".join(silo)
    else:
        name = str(silo)
    return _run_llmli(["remove", name])


def _pull_status_line(current: int, total: int, name: str, detail: str = "") -> None:
    """Overwrite a single terminal line with pull progress."""
    is_tty = sys.stderr.isatty()
    bar_width = 20
    filled = int(bar_width * current / total) if total else bar_width
    bar = "\u2588" * filled + "\u2591" * (bar_width - filled)
    suffix = f"  {detail}" if detail else ""
    line = f"\r  [{bar}] {current}/{total}  {name}{suffix}"
    if is_tty:
        # Clear line then write
        sys.stderr.write(f"\033[2K{line}")
        sys.stderr.flush()
    else:
        # Non-TTY: just print each update
        print(line.strip())


def _build_pull_env(status_file_path: str) -> dict[str, str]:
    """Minimize env passthrough for pull subprocesses while keeping runtime essentials."""
    allow_exact = {
        "PATH",
        "HOME",
        "SHELL",
        "TERM",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "VIRTUAL_ENV",
        "OLLAMA_HOST",
        "NO_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
    }
    out: dict[str, str] = {}
    for k, v in os.environ.items():
        if k in allow_exact or k.startswith("LLMLIBRARIAN_"):
            out[k] = v
    out["LLMLIBRARIAN_STATUS_FILE"] = status_file_path
    out["LLMLIBRARIAN_QUIET"] = "1"
    return out


def cmd_pull(args: argparse.Namespace) -> int:
    """Refresh all registered silos (incremental by default)."""
    if getattr(args, "watch", False):
        return cmd_pull_watch(args)
    reg = _read_registry()
    sources = reg.get("sources", [])
    if not sources:
        print("No registered silos. Use: pal add <path>", file=sys.stderr)
        return 1
    import tempfile
    is_tty = sys.stderr.isatty()
    total = len(sources)
    fail_count = 0
    updated_silos: list[str] = []
    failed_silos: list[str] = []
    for idx, src in enumerate(sources, 1):
        path = src.get("path")
        if not path:
            continue
        name = src.get("name") or Path(path).name
        _pull_status_line(idx, total, name)

        llmli_args = ["add"]
        if getattr(args, "full", False):
            llmli_args.append("--full")
        if getattr(args, "allow_cloud", False):
            llmli_args.append("--allow-cloud")
        if getattr(args, "follow_symlinks", False):
            llmli_args.append("--follow-symlinks")
        llmli_args.append(path)

        status_file = tempfile.NamedTemporaryFile(prefix="llmli_status_", delete=False)
        status_file_path = status_file.name
        status_file.close()
        env = _build_pull_env(status_file_path)
        root = Path(__file__).resolve().parent
        venv_llmli = root / ".venv" / "bin" / "llmli"
        if venv_llmli.exists():
            cmd = [str(venv_llmli)] + llmli_args
        else:
            cmd = [sys.executable, str(root / "cli.py")] + llmli_args
        r = subprocess.run(cmd, env=env, capture_output=True, text=True)
        code = r.returncode

        files_indexed = 0
        try:
            with open(status_file_path, "r", encoding="utf-8") as f:
                status = json.load(f)
            files_indexed = status.get("files_indexed") or 0
        except Exception:
            pass
        try:
            os.unlink(status_file_path)
        except Exception:
            pass

        if code != 0:
            fail_count += 1
            failed_silos.append(name)
            _pull_status_line(idx, total, name, "FAILED")
        elif files_indexed > 0:
            updated_silos.append(f"{name} ({files_indexed} files)")
            _pull_status_line(idx, total, name, f"+{files_indexed} files")

    # Clear progress line
    if is_tty:
        sys.stderr.write("\033[2K\r")
        sys.stderr.flush()

    # Summary
    if updated_silos:
        print(f"Updated: {', '.join(updated_silos)}")
    if failed_silos:
        print(f"Failed: {', '.join(failed_silos)}", file=sys.stderr)
    if not updated_silos and not failed_silos:
        print("All silos up to date.")
    return 0 if fail_count == 0 else 1


def cmd_pull_watch(args: argparse.Namespace) -> int:
    """Watch dev self-silo and apply incremental updates on change."""
    if Observer is None:
        print("Error: watchdog is not installed. Install `watchdog` to use --watch.", file=sys.stderr)
        return 1
    if not is_dev_repo():
        print("Error: --watch is only supported in the llmLibrarian repo.", file=sys.stderr)
        return 1
    rc = ensure_self_silo(force=True)
    if rc != 0:
        return rc
    repo_root = _get_git_root()
    if repo_root is None:
        print("Error: unable to detect git root for watch.", file=sys.stderr)
        return 1
    interval = float(getattr(args, "interval", 10) or 10)
    debounce = float(getattr(args, "debounce", 1) or 1)
    watcher = SiloWatcher(
        repo_root,
        os.environ.get("LLMLIBRARIAN_DB", "./my_brain_db"),
        interval=interval,
        debounce=debounce,
        silo_slug="__self__",
        allow_cloud=True,
    )
    try:
        watcher.run()
    except KeyboardInterrupt:
        return 0
    return 0


def cmd_silos(args: argparse.Namespace) -> int:
    """Report silo health (duplicates, overlaps, mismatches)."""
    root = Path(__file__).resolve().parent
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from silo_audit import load_registry, load_file_registry, load_manifest, find_duplicate_hashes, find_path_overlaps, find_count_mismatches, format_report

    db_path = os.environ.get("LLMLIBRARIAN_DB", "./my_brain_db")
    registry = load_registry(db_path)
    file_registry = load_file_registry(db_path)
    manifest = load_manifest(db_path)
    dupes = find_duplicate_hashes(file_registry)
    overlaps = find_path_overlaps(registry)
    mismatches = find_count_mismatches(registry, manifest)
    report = format_report(registry, dupes, overlaps, mismatches)
    print(report)
    return 0


def cmd_tool(args: argparse.Namespace) -> int:
    """Passthrough: pal tool <name> <args...> -> run underlying tool."""
    name = getattr(args, "tool_name", None)
    rest = list(getattr(args, "tool_args", []) or [])
    while rest and rest[0] == "--":
        rest.pop(0)
    if name == "llmli":
        return _run_llmli(rest)
    print(f"Error: unknown tool '{name}'. Use: pal tool llmli <args...>", file=sys.stderr)
    return 1


KNOWN_COMMANDS = frozenset({"add", "ask", "ls", "inspect", "log", "capabilities", "ensure-self", "pull", "silos", "remove", "tool"})


def main() -> int:
    # Default to ask when first arg is not a known subcommand: pal "who is X" or pal who is X → pal ask ...
    if len(sys.argv) >= 2 and sys.argv[1] not in KNOWN_COMMANDS and not sys.argv[1].startswith("-"):
        sys.argv.insert(1, "ask")

    # Natural "ask in <silo> ..." → treat as --in <silo> so silo scope isn't eaten as query words
    if len(sys.argv) >= 4 and sys.argv[1] == "ask" and sys.argv[2].lower() == "in":
        third = sys.argv[3]
        if not third.startswith("-") and third:
            sys.argv[2:4] = ["--in", third]

    parser = argparse.ArgumentParser(
        prog="pal",
        description="Agent CLI: add, ask, ls, log (delegate to llmli); pal <question> runs ask.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Index folder (llmli add); register in ~/.pal; cloud paths blocked unless --allow-cloud")
    p_add.add_argument("path", help="Folder path to index")
    p_add.add_argument("--allow-cloud", action="store_true", help="Allow OneDrive/iCloud/Dropbox/Google Drive")
    p_add.set_defaults(_run=cmd_add)

    p_ask = sub.add_parser("ask", help="Ask (llmli ask; use --unified to search all silos for comparison)")
    p_ask.add_argument("--in", dest="in_silo", metavar="SILO", help="Limit to one silo")
    p_ask.add_argument("--unified", action="store_true", help="Search across all silos (for compare/thematic questions)")
    p_ask.add_argument("--strict", action="store_true", help="Conservative: never conclude absence from partial evidence; say 'unknown' + sources when unsure")
    p_ask.add_argument("query", nargs="+", help="Question")
    p_ask.set_defaults(_run=cmd_ask)

    p_ls = sub.add_parser("ls", help="List silos (llmli ls)")
    p_ls.set_defaults(_run=cmd_ls)

    p_inspect = sub.add_parser("inspect", help="Silo details and top files by chunk count (llmli inspect)")
    p_inspect.add_argument("silo", help="Silo slug or display name")
    p_inspect.add_argument("--top", type=int, default=None, help="Show top N files (default: 20; pass to llmli)")
    p_inspect.add_argument("--filter", choices=["pdf", "docx", "code"], help="Show only pdf, docx, or code")
    p_inspect.set_defaults(_run=cmd_inspect)

    p_remove = sub.add_parser("remove", help="Remove silo (friendly alias for llmli remove)")
    p_remove.add_argument("silo", nargs="+", help="Silo slug, display name, or path")
    p_remove.set_defaults(_run=cmd_remove)

    p_log = sub.add_parser("log", help="Last failures (llmli log --last)")
    p_log.set_defaults(_run=cmd_log)

    p_capabilities = sub.add_parser("capabilities", help="Supported file types and extractors (llmli capabilities)")
    p_capabilities.set_defaults(_run=cmd_capabilities)

    p_ensure = sub.add_parser("ensure-self", help="Ensure dev-mode self-silo exists; warn if stale")
    p_ensure.set_defaults(_run=cmd_ensure_self)

    p_pull = sub.add_parser("pull", help="Refresh all registered silos (incremental by default)")
    p_pull.add_argument("--watch", action="store_true", help="Watch dev self-silo and update on change")
    p_pull.add_argument("--interval", type=float, default=10, help="Reconcile interval in seconds (watch mode)")
    p_pull.add_argument("--debounce", type=float, default=1, help="Debounce seconds before reindexing (watch mode)")
    p_pull.add_argument("--full", action="store_true", help="Full reindex (delete + add) instead of incremental")
    p_pull.add_argument("--allow-cloud", action="store_true", help="Allow OneDrive/iCloud/Dropbox/Google Drive")
    p_pull.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks inside folders")
    p_pull.set_defaults(_run=cmd_pull)

    p_silos = sub.add_parser("silos", help="Silo health report (duplicates, overlaps, mismatches)")
    p_silos.set_defaults(_run=cmd_silos)

    p_tool = sub.add_parser("tool", help="Passthrough to tool: pal tool llmli <args...>")
    p_tool.add_argument("tool_name", help="Tool name (e.g. llmli)")
    p_tool.add_argument("tool_args", nargs=argparse.REMAINDER, default=[], help="Arguments for the tool")
    p_tool.set_defaults(_run=cmd_tool)

    args = parser.parse_args()
    return args._run(args)


if __name__ == "__main__":
    sys.exit(main())
