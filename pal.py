#!/usr/bin/env python3
"""
pal — Agent CLI that orchestrates tools (e.g. llmli). Control-plane: route, registry.
Not a replacement for llmli; pal add/ask/ls/log delegate to llmli. Use pal tool llmli ... for passthrough.
"""
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
import threading
import getpass
import re
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import typer

from pal_registry import read_pal_registry, write_pal_registry

PAL_HOME = Path(os.environ.get("PAL_HOME", os.path.expanduser("~/.pal")))
REGISTRY_PATH = PAL_HOME / "registry.json"
WATCH_LOCKS_DIR = PAL_HOME / "watch_locks"

_PAL_ROOT = Path(__file__).resolve().parent
_DEFAULT_DB = str(_PAL_ROOT / "my_brain_db")


def _iter_editable_roots(site_root: Path) -> list[Path]:
    roots: list[Path] = []
    for pth_path in sorted(site_root.glob("*llmlibrarian*.pth")):
        try:
            for raw_line in pth_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or line.startswith("import "):
                    continue
                candidate = Path(line).expanduser()
                if candidate.exists():
                    roots.append(candidate.resolve())
        except Exception:
            continue
    return roots


def _bootstrap_src_path() -> None:
    root = Path(__file__).resolve().parent
    candidates: list[Path] = []

    cwd = Path.cwd().resolve()
    cwd_src = cwd / "src"
    if cwd_src.is_dir():
        candidates.append(cwd_src)

    root_src = root / "src"
    if (root_src / "state.py").exists():
        candidates.append(root_src)
    if (root / "state.py").exists():
        candidates.append(root)

    for editable_root in _iter_editable_roots(root):
        editable_src = editable_root / "src"
        if (editable_src / "state.py").exists():
            candidates.append(editable_src.resolve())
        elif (editable_root / "state.py").exists():
            candidates.append(editable_root.resolve())

    seen: set[str] = set()
    for candidate in candidates:
        candidate_str = str(candidate)
        if candidate_str in seen:
            continue
        seen.add(candidate_str)
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)


_bootstrap_src_path()

import jobs_runtime as jobsrt


def _ensure_pal_home() -> None:
    PAL_HOME.mkdir(parents=True, exist_ok=True)


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _watch_lock_path(db_path: str | Path, silo_slug: str) -> Path:
    db_resolved = str(Path(db_path).resolve())
    key = f"{db_resolved}::{silo_slug}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    safe_slug = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(silo_slug))
    return WATCH_LOCKS_DIR / f"{safe_slug}-{digest}.pid"


def _iter_watch_locks() -> list[Path]:
    if not WATCH_LOCKS_DIR.exists():
        return []
    return sorted(WATCH_LOCKS_DIR.glob("*.pid"))


def _read_watch_lock(lock_path: Path) -> dict | None:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    try:
        return {"pid": int(raw)}
    except Exception:
        return None


def _read_watch_lock_pid(lock_path: Path) -> int | None:
    data = _read_watch_lock(lock_path)
    if not data:
        return None
    value = data.get("pid")
    try:
        return int(value)
    except Exception:
        return None


def _current_uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        try:
            return int(getuid())
        except Exception:
            return None
    return None


def _safe_user_name() -> str:
    try:
        return str(getpass.getuser())
    except Exception:
        return "unknown"


def _detect_pal_version() -> str:
    root = Path(__file__).resolve().parent
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return "unknown"
    if r.returncode != 0:
        return "unknown"
    out = (r.stdout or "").strip()
    return out or "unknown"


def _acquire_silo_pid_lock(
    db_path: str | Path,
    silo_slug: str,
    root_path: Path | None = None,
) -> tuple[Path | None, str | None]:
    lock_path = _watch_lock_path(db_path, silo_slug)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    for _ in range(3):
        if lock_path.exists():
            pid = _read_watch_lock_pid(lock_path)
            if pid is not None and _pid_is_running(pid):
                return None, f"Error: watcher already running for silo '{silo_slug}' (pid {pid})."
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                return None, f"Error: unable to clear stale watcher lock for silo '{silo_slug}'."
        payload = {
            "version": 2,
            "pid": os.getpid(),
            "uid": _current_uid(),
            "user": _safe_user_name(),
            "silo": str(silo_slug),
            "root_path": str(root_path.resolve()) if root_path else None,
            "db_path": str(Path(db_path).resolve()),
            "started_at": int(time.time()),
            "argv_hash": hashlib.sha1(" ".join(sys.argv).encode("utf-8")).hexdigest()[:12],
            "pal_version": _detect_pal_version(),
        }
        try:
            fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        except Exception:
            return None, f"Error: unable to create watcher lock for silo '{silo_slug}'."
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except Exception:
            try:
                lock_path.unlink()
            except Exception:
                pass
            return None, f"Error: unable to write watcher lock for silo '{silo_slug}'."
        return lock_path, None
    pid = _read_watch_lock_pid(lock_path)
    if pid is not None and _pid_is_running(pid):
        return None, f"Error: watcher already running for silo '{silo_slug}' (pid {pid})."
    return None, f"Error: unable to acquire watcher lock for silo '{silo_slug}'."


def _release_silo_pid_lock(lock_path: Path | None) -> None:
    if lock_path is None:
        return
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _process_ps_value(pid: int, field: str) -> str | None:
    if pid <= 0:
        return None
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", f"{field}="],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out = (r.stdout or "").strip()
    return out or None


def _process_command_signature(pid: int) -> str | None:
    return _process_ps_value(pid, "command")


def _is_watch_process_command(command: str | None) -> bool:
    if not command:
        return False
    lowered = command.lower()
    has_watch = "--watch" in lowered
    has_pull = bool(re.search(r"\bpull\b", lowered))
    has_pal = bool(re.search(r"\bpal(\.py)?\b", lowered))
    return has_watch and has_pull and has_pal


def _build_watch_status_record(lock_path: Path) -> dict:
    data = _read_watch_lock(lock_path) or {}
    pid_val = data.get("pid")
    pid: int | None = None
    try:
        if pid_val is not None:
            pid = int(pid_val)
    except Exception:
        pid = None

    running = bool(pid is not None and _pid_is_running(pid))
    command = _process_command_signature(pid) if (pid is not None and running) else None
    process_uid: int | None = None
    process_uid_raw = _process_ps_value(pid, "uid") if (pid is not None and running) else None
    if process_uid_raw is not None:
        try:
            process_uid = int(process_uid_raw)
        except Exception:
            process_uid = None
    stat = _process_ps_value(pid, "stat") if (pid is not None and running) else None
    etime = _process_ps_value(pid, "etime") if (pid is not None and running) else None
    lock_uid_raw = data.get("uid")
    lock_uid: int | None = None
    if lock_uid_raw is not None:
        try:
            lock_uid = int(lock_uid_raw)
        except Exception:
            lock_uid = None
    started_at_raw = data.get("started_at")
    started_at: int | None = None
    if started_at_raw is not None:
        try:
            started_at = int(started_at_raw)
        except Exception:
            started_at = None
    now = int(time.time())
    age_seconds: int | None = (now - started_at) if started_at is not None else None
    legacy_lock = not isinstance(data.get("version"), int) or lock_uid is None
    stale = not running
    suspended = bool(stat and stat.startswith("T"))
    if stale:
        state = "stale"
    elif suspended:
        state = "suspended"
    else:
        state = "running"
    return {
        "lock_path": str(lock_path),
        "legacy_lock": legacy_lock,
        "stale": stale,
        "state": state,
        "running": running,
        "pid": pid,
        "uid": lock_uid,
        "user": data.get("user"),
        "process_uid": process_uid,
        "silo": data.get("silo"),
        "root_path": data.get("root_path"),
        "db_path": data.get("db_path"),
        "started_at": started_at,
        "age_seconds": age_seconds,
        "argv_hash": data.get("argv_hash"),
        "pal_version": data.get("pal_version"),
        "command": command,
        "signature_ok": _is_watch_process_command(command) if command else None,
        "etime": etime,
    }


def _status_records(db_path: str | Path, optional_path: Path | None = None) -> tuple[list[dict], str | None]:
    records = [_build_watch_status_record(lock_path) for lock_path in _iter_watch_locks()]
    if optional_path is None:
        return records, None
    registry = _read_llmli_registry(db_path)
    slug = _resolve_llmli_silo_by_path(registry, optional_path.resolve())
    if not slug:
        return [], f"Error: no indexed silo found for path: {optional_path}"
    filtered = [r for r in records if str(r.get("silo") or "") == slug]
    return filtered, None


def _resolve_stop_target(target: str, records: list[dict]) -> tuple[dict | None, str | None]:
    raw = (target or "").strip()
    if not raw:
        return None, "Error: --stop requires a non-empty target."
    matches: list[dict] = []
    if raw.isdigit():
        pid = int(raw)
        matches = [r for r in records if r.get("pid") == pid]
    else:
        lowered = raw.lower()
        normalized_path = None
        try:
            normalized_path = str(Path(raw).expanduser().resolve())
        except Exception:
            normalized_path = None
        by_slug = [r for r in records if str(r.get("silo") or "").lower() == lowered]
        if by_slug:
            matches = by_slug
        else:
            by_display: list[dict] = []
            for r in records:
                silo = str(r.get("silo") or "")
                display = silo
                db_path = r.get("db_path")
                if db_path:
                    reg = _read_llmli_registry(str(db_path))
                    entry = reg.get(silo) if isinstance(reg, dict) else None
                    if isinstance(entry, dict):
                        display = str(entry.get("display_name") or display)
                if display.lower() == lowered:
                    by_display.append(r)
            if by_display:
                matches = by_display
            elif normalized_path:
                matches = [r for r in records if str((r.get("root_path") or "")).lower() == normalized_path.lower()]
    if not matches:
        return None, f"Error: no watcher target found for '{target}'."
    if len(matches) > 1:
        candidates = ", ".join(
            f"{m.get('silo')} (pid {m.get('pid')})"
            for m in matches
        )
        return None, f"Error: ambiguous watcher target '{target}'. Matches: {candidates}"
    return matches[0], None


def _wait_for_pid_exit(pid: int, timeout: float) -> bool:
    deadline = time.time() + max(timeout, 0.0)
    while time.time() < deadline:
        if not _pid_is_running(pid):
            return True
        time.sleep(0.1)
    return not _pid_is_running(pid)


def _stop_watch_process(record: dict, timeout: float = 3.0) -> dict:
    lock_path = Path(str(record.get("lock_path")))
    pid = record.get("pid")
    out = {
        "silo": record.get("silo"),
        "pid": pid,
        "lock_path": str(lock_path),
        "status": "unknown",
        "forced": False,
        "lock_removed": False,
    }
    try:
        pid_i = int(pid)
    except Exception:
        _release_silo_pid_lock(lock_path)
        out.update({"status": "stale_lock_removed", "lock_removed": True, "message": "Lock had no valid PID."})
        return out

    if not _pid_is_running(pid_i):
        _release_silo_pid_lock(lock_path)
        out.update({"status": "stale_lock_removed", "lock_removed": True, "message": "Process already exited."})
        return out

    current_uid = _current_uid()
    lock_uid = record.get("uid")
    if current_uid is not None and lock_uid is not None:
        try:
            if int(lock_uid) != int(current_uid):
                out.update({"status": "refused_uid_mismatch", "message": "Refusing to signal watcher owned by a different user."})
                return out
        except Exception:
            pass

    command = _process_command_signature(pid_i)
    if not _is_watch_process_command(command):
        out.update({
            "status": "refused_signature_mismatch",
            "message": "Refusing to stop process: command signature does not match `pal pull --watch`.",
            "suspicious": True,
        })
        return out

    try:
        os.kill(pid_i, signal.SIGTERM)
    except ProcessLookupError:
        _release_silo_pid_lock(lock_path)
        out.update({"status": "stale_lock_removed", "lock_removed": True, "message": "Process already exited."})
        return out
    except PermissionError:
        out.update({"status": "refused_permission", "message": "Permission denied while signaling target PID."})
        return out
    except Exception as exc:
        out.update({"status": "signal_error", "message": f"Failed to signal watcher: {exc}"})
        return out

    if _wait_for_pid_exit(pid_i, timeout):
        _release_silo_pid_lock(lock_path)
        out.update({"status": "stopped", "lock_removed": True, "message": "Watcher stopped with SIGTERM."})
        return out

    try:
        os.kill(pid_i, signal.SIGKILL)
        out["forced"] = True
    except ProcessLookupError:
        _release_silo_pid_lock(lock_path)
        out.update({"status": "stale_lock_removed", "lock_removed": True, "message": "Process exited during stop."})
        return out
    except PermissionError:
        out.update({"status": "refused_permission", "message": "Permission denied while force-stopping target PID."})
        return out
    except Exception as exc:
        out.update({"status": "signal_error", "message": f"Failed to force-stop watcher: {exc}"})
        return out

    if _wait_for_pid_exit(pid_i, 1.0):
        _release_silo_pid_lock(lock_path)
        out.update({"status": "stopped", "lock_removed": True, "message": "Watcher stopped with SIGKILL."})
    else:
        out.update({"status": "still_running", "message": "Watcher did not exit after SIGTERM/SIGKILL."})
    return out


def _prune_stale_locks(records: list[dict]) -> dict:
    removed = 0
    failed: list[str] = []
    for record in records:
        if not record.get("stale"):
            continue
        lock_path = Path(str(record.get("lock_path")))
        try:
            lock_path.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except Exception:
            failed.append(str(lock_path))
    return {"removed": removed, "failed": failed}


def _render_watch_status(records: list[dict]) -> None:
    if not records:
        print("No watcher locks.")
        return
    print("Silo                      PID     State      Legacy  Stale  Root")
    print("--------------------------------------------------------------------------")
    for r in records:
        silo = str(r.get("silo") or "-")
        pid = str(r.get("pid") or "-")
        state = str(r.get("state") or "-")
        legacy = "yes" if r.get("legacy_lock") else "no"
        stale = "yes" if r.get("stale") else "no"
        root = str(r.get("root_path") or "-")
        print(f"{silo:24} {pid:7} {state:10} {legacy:6} {stale:5}  {root}")


def _fmt_int(n: int) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def _fmt_bytes_iec(n: int) -> str:
    """Human-readable size using IEC (1024) steps."""
    try:
        x = float(int(n))
    except Exception:
        return str(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if x < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(x):,} B"
            return f"{x:.2f} {unit}"
        x /= 1024.0
    return f"{x:.2f} TiB"


def _status_action_for_mismatch(slug: str, registry_files: int, manifest_files: int, path: str | None) -> str:
    if slug == "__adversarial_eval__" and manifest_files == 0:
        return "remove transient eval silo: pal remove __adversarial_eval__"
    if path:
        return f"re-index this source: pal pull --full \"{path}\""
    return "re-index all sources: pal pull --full"


def _render_health_summary(
    registry: list[dict],
    dupes: list[dict],
    overlaps: list[dict],
    mismatches: list[dict],
) -> str:
    total_silos = len(registry)
    total_chunks = sum(int(s.get("chunks_count", 0) or 0) for s in registry)
    issue_count = len(dupes) + len(overlaps) + len(mismatches)
    state = "Healthy" if issue_count == 0 else "Needs Attention"

    lines: list[str] = [
        "Health Summary",
        f"  State: {state}",
        f"  Silos: {_fmt_int(total_silos)}",
        f"  Chunks: {_fmt_int(total_chunks)}",
        f"  Duplicate groups: {_fmt_int(len(dupes))}",
        f"  Path overlaps: {_fmt_int(len(overlaps))}",
        f"  Count mismatches: {_fmt_int(len(mismatches))}",
    ]

    if issue_count == 0:
        lines.extend(["", "No action needed."])
    return "\n".join(lines)


def _read_registry() -> dict:
    return read_pal_registry(REGISTRY_PATH)


def _write_registry(data: dict) -> None:
    _ensure_pal_home()
    write_pal_registry(REGISTRY_PATH, data)


def _remove_source_path(path: str | Path) -> bool:
    try:
        target = str(Path(path).expanduser().resolve())
    except Exception:
        target = str(path)
    reg = _read_registry()
    bookmarks = reg.get("bookmarks", []) or []
    kept = []
    removed = False
    for source in bookmarks:
        raw = str((source or {}).get("path") or "")
        if not raw:
            continue
        try:
            candidate = str(Path(raw).expanduser().resolve())
        except Exception:
            candidate = raw
        if candidate == target:
            removed = True
            continue
        kept.append(source)
    if removed:
        reg["bookmarks"] = kept
        _write_registry(reg)
    return removed


def _resolve_registry_source_for_remove(target: str, db_path: str | Path) -> str | None:
    raw = (target or "").strip()
    if not raw:
        return None
    registry = _read_llmli_registry(db_path)
    if raw in registry:
        entry = registry.get(raw) if isinstance(registry, dict) else None
        if isinstance(entry, dict) and entry.get("path"):
            return str(entry.get("path"))
    lowered = raw.lower()
    for slug, entry in registry.items():
        if not isinstance(entry, dict):
            continue
        display_name = str(entry.get("display_name") or slug)
        if display_name.lower() == lowered and entry.get("path"):
            return str(entry.get("path"))
    try:
        candidate = Path(raw).expanduser().resolve()
    except Exception:
        candidate = None
    if candidate is not None:
        return str(candidate)
    return None


def _resolve_llmli_paths() -> tuple[Path, Path]:
    """
    Resolve which cli.py and src directory to run against.
    Prefer current working directory when it looks like a checkout.
    """
    root = Path(__file__).resolve().parent
    cwd = Path.cwd().resolve()
    cwd_cli = cwd / "cli.py"
    cwd_src = cwd / "src"
    if cwd_cli.exists() and cwd_src.is_dir():
        return cwd_cli, cwd_src
    for editable_root in _iter_editable_roots(root):
        editable_cli = editable_root / "cli.py"
        editable_src = editable_root / "src"
        if editable_cli.exists() and editable_src.is_dir():
            return editable_cli, editable_src
    if (root / "state.py").exists():
        return root / "cli.py", root
    return root / "cli.py", root / "src"


def _prepend_pythonpath(env: dict[str, str], src_dir: Path) -> None:
    """Prepend src path for subprocess imports while preserving existing PYTHONPATH."""
    existing = env.get("PYTHONPATH")
    if existing:
        env["PYTHONPATH"] = str(src_dir) + os.pathsep + existing
    else:
        env["PYTHONPATH"] = str(src_dir)


@contextmanager
def _temporary_env(overrides: dict[str, str | None]):
    previous: dict[str, str | None] = {}
    try:
        for key, value in overrides.items():
            previous[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_llmli(args: list[str], extra_env: dict[str, str] | None = None) -> int:
    """Run llmli as subprocess; return exit code."""
    cli_path, src_path = _resolve_llmli_paths()
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    _prepend_pythonpath(env, src_path)
    cmd = [sys.executable, str(cli_path)] + args
    r = subprocess.run(cmd, env=env)
    return r.returncode


def _ensure_src_on_path() -> None:
    _cli, src = _resolve_llmli_paths()
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
    bookmarks = reg.get("bookmarks", []) or []
    repo_str = str(repo_root.resolve())
    filtered = [
        s
        for s in bookmarks
        if not (
            (s.get("silo") == "__self__")
            or (s.get("path") == repo_str and s.get("name") == "self")
        )
    ]
    entry = {
        "path": repo_str,
        "name": "self",
        "silo": "__self__",
    }
    filtered.append(entry)
    reg["bookmarks"] = filtered
    return reg


def _get_self_index_mtime_from_registry(reg: dict) -> int | None:
    for s in reg.get("bookmarks", []) or []:
        if s.get("silo") == "__self__" and s.get("path"):
            v = s.get("self_silo_last_index_mtime")
            try:
                return int(v)
            except Exception:
                return None
    return None


def _warn_self_silo_stale() -> None:
    print("Self-silo stale (repo changed since last index). Run `pal sync`.", file=sys.stderr, flush=True)


def _warn_self_silo_missing() -> None:
    print("Self-silo missing. Run `pal sync`.", file=sys.stderr, flush=True)


def _warn_self_silo_mismatch() -> None:
    print("Self-silo path mismatch. Run `pal sync`.", file=sys.stderr, flush=True)


def _self_silo_is_stale(repo_root: Path, self_entry: dict | None, reg: dict) -> bool:
    """Return True when self-silo appears stale for current repo state."""
    last_index_mtime = _get_self_index_mtime_from_registry(reg)
    if last_index_mtime is None and self_entry:
        last_index_mtime = _llmli_updated_to_epoch(self_entry.get("updated"))
    dirty = _git_is_dirty(repo_root)
    last_commit = _git_last_commit_ct(repo_root)
    return bool(
        dirty or (
            last_index_mtime is not None and last_commit is not None and last_commit > last_index_mtime
        )
    )


def ensure_self_silo(force: bool = False, emit_warning: bool = True) -> int:
    require_self = _should_require_self_silo()
    if not require_self:
        return 0
    repo_root = _get_git_root()
    if repo_root is None:
        print("Warning: unable to detect git root; skipping self-silo.", file=sys.stderr)
        return 0

    is_dev = _is_dev_repo_at_root(repo_root)
    db_path = os.environ.get("LLMLIBRARIAN_DB", _DEFAULT_DB)
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
            if emit_warning:
                if not self_entry:
                    _warn_self_silo_missing()
                else:
                    _warn_self_silo_mismatch()
            return 0
        _ensure_src_on_path()
        db_path = os.environ.get("LLMLIBRARIAN_DB", _DEFAULT_DB)
        if self_entry and self_path and self_path != repo_str:
            from operations import op_remove_silo
            op_remove_silo(db_path, "__self__")
        if existing_slug and existing_slug != "__self__":
            from operations import op_remove_silo
            op_remove_silo(db_path, existing_slug)
        try:
            from ingest import run_add
            files_ok, n_failures = run_add(
                path=repo_str,
                db_path=db_path,
                forced_silo_slug="__self__",
                display_name_override="self",
                allow_cloud=is_dev,
            )
            code = 0 if files_ok >= 0 else 1
        except Exception as e:
            print(f"Warning: failed to index self-silo: {e}", file=sys.stderr)
            code = 1
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

    stale = _self_silo_is_stale(repo_root, self_entry if isinstance(self_entry, dict) else None, reg)
    if stale:
        if emit_warning:
            _warn_self_silo_stale()
    return 0


def _normalize_natural_ask_scope(
    query_tokens: list[str],
    explicit_in_silo: str | None,
    db_path: str | Path,
) -> tuple[str | None, list[str], str | None]:
    """
    Normalize natural shorthand:
      pal ask in <silo> <question...>
    into explicit --in semantics for llmli delegation.
    """
    if explicit_in_silo:
        return explicit_in_silo, query_tokens, None
    if len(query_tokens) < 2:
        return None, query_tokens, None
    if str(query_tokens[0]).strip().lower() != "in":
        return None, query_tokens, None

    idx = 1
    if len(query_tokens) >= 3 and str(query_tokens[1]).strip().lower() == "my":
        idx = 2
    if len(query_tokens) <= idx:
        return None, query_tokens, "Malformed scope shorthand. Use: pal ask --in <silo> \"<question>\""

    first_token = str(query_tokens[idx]).strip()
    if not first_token:
        return None, query_tokens, "Malformed scope shorthand. Use: pal ask --in <silo> \"<question>\""
    if "--" in first_token:
        return None, query_tokens, (
            f"Malformed scope token '{first_token}'. "
            "Use a space before flags or use explicit scope: pal ask --in <silo> \"<question>\""
        )

    _ensure_src_on_path()
    from state import resolve_silo_to_slug, resolve_silo_prefix

    def _norm_scope_token(raw: str | None) -> str:
        s = (raw or "").strip().lower()
        s = re.sub(r"[^\w\s-]", "", s)
        s = re.sub(r"[-\s]+", "-", s).strip("-")
        return s

    def _strip_hash_suffix(slug: str | None) -> str | None:
        if not slug:
            return None
        m = re.match(r"^(.+)-[0-9a-f]{8}$", slug)
        return m.group(1) if m else slug

    def _resolve_candidate(raw_candidate: str) -> str | None:
        candidate = (raw_candidate or "").strip()
        if not candidate:
            return None

        resolved = resolve_silo_to_slug(db_path, candidate)
        if resolved:
            return resolved

        # Prefix matching in registry is case-sensitive; normalize before trying.
        candidate_norm = _norm_scope_token(candidate)
        if candidate_norm:
            resolved = resolve_silo_prefix(db_path, candidate_norm)
            if resolved:
                return resolved

        # Relaxed matching for natural language inputs:
        # - hyphen/space/case variants
        # - display names vs slugs
        registry = _read_llmli_registry(db_path)
        if not isinstance(registry, dict):
            return None
        matches: list[str] = []
        for slug, data in registry.items():
            display = ""
            if isinstance(data, dict):
                display = str(data.get("display_name") or "")
            aliases = {
                _norm_scope_token(slug),
                _norm_scope_token(display),
                _norm_scope_token(_strip_hash_suffix(slug)),
            }
            if candidate_norm in aliases:
                matches.append(slug)
        if len(matches) == 1:
            return matches[0]

        # Normalized prefix fallback when unique.
        if candidate_norm:
            prefix_matches: list[str] = []
            for slug, data in registry.items():
                display = ""
                if isinstance(data, dict):
                    display = str(data.get("display_name") or "")
                aliases = [
                    _norm_scope_token(slug),
                    _norm_scope_token(display),
                    _norm_scope_token(_strip_hash_suffix(slug)),
                ]
                if any(a.startswith(candidate_norm) for a in aliases if a):
                    prefix_matches.append(slug)
            uniq = list(dict.fromkeys(prefix_matches))
            if len(uniq) == 1:
                return uniq[0]
        return None

    # Prefer the longest candidate that resolves, so multi-word silo names work:
    # pal ask in Job Related Stuff "..."
    for end in range(len(query_tokens) - 1, idx, -1):
        candidate = " ".join(str(t) for t in query_tokens[idx:end]).strip()
        if not candidate:
            continue
        resolved = _resolve_candidate(candidate)
        if resolved:
            remainder = query_tokens[end:]
            if remainder:
                return resolved, remainder, None

    remainder = query_tokens[idx + 1 :]
    if not remainder:
        return None, query_tokens, "Missing question after scope. Use: pal ask --in <silo> \"<question>\""
    # Conservative fallback: if we cannot resolve deterministically, leave query unchanged.
    return None, query_tokens, None


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
        label: str = "this folder",
        startup_message: str | None = None,
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
        self.label = label
        self.startup_message = startup_message
        watch_env = {
            "LLMLIBRARIAN_PROCESSOR_LOG_LEVEL": "ERROR",
            "LLMLIBRARIAN_INGEST_LOG_LEVEL": "FATAL",
            "LLMLIBRARIAN_SUPPRESS_RECOVERABLE_WARNINGS": "1",
        }

        def _quiet_update_single_file(*args, **kwargs):
            with _temporary_env(watch_env):
                return update_single_file(*args, **kwargs)

        def _quiet_remove_single_file(*args, **kwargs):
            with _temporary_env(watch_env):
                return remove_single_file(*args, **kwargs)

        self._update_single_file = _quiet_update_single_file
        self._remove_single_file = _quiet_remove_single_file
        self._update_silo_counts = update_silo_counts
        self._read_manifest = _read_file_manifest
        self._load_limits_config = _load_limits_config
        self._collect_files = collect_files
        self._should_index = should_index
        self._include = ADD_DEFAULT_INCLUDE
        self._exclude = ADD_DEFAULT_EXCLUDE

        self._observer = Observer()
        self._handler = _SiloEventHandler(self)
        self._queue: dict[str, dict[str, object]] = {}
        self._queue_lock = threading.Lock()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._logger = self._build_logger()

    def _build_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"pal.watch.{self.silo_slug}.{os.getpid()}")
        if logger.handlers:
            return logger
        logger.setLevel(logging.INFO)
        logger.propagate = False
        log_dir = jobsrt.watch_log_dir(PAL_HOME)
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            jobsrt.watch_log_path(PAL_HOME, self.silo_slug),
            maxBytes=1_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        return logger

    def _log(self, message: str) -> None:
        self._logger.info(message)
        print(message)

    def stop(self) -> None:
        self._stop.set()

    def _retry_delay(self, attempts: int) -> float:
        schedule = [30.0, 60.0, 120.0, 300.0]
        idx = min(max(attempts - 1, 0), len(schedule) - 1)
        return schedule[idx]

    def _queue_action(self, path_str: str, action: str, delay: float | None = None, attempts: int = 0) -> None:
        due_at = time.time() + (self.debounce if delay is None else max(delay, 0.0))
        with self._queue_lock:
            self._queue[path_str] = {
                "due_at": due_at,
                "action": action,
                "attempts": attempts,
            }

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
        self._queue_action(str(p), "update")

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
        self._queue_action(str(p), "delete")

    def _drain_due(self, now: float | None = None) -> int:
        if now is None:
            now = time.time()
        due: list[tuple[str, str, int]] = []
        with self._queue_lock:
            for path, meta in list(self._queue.items()):
                due_at = float(meta.get("due_at") or 0.0)
                action = str(meta.get("action") or "update")
                attempts = int(meta.get("attempts") or 0)
                if now >= due_at:
                    due.append((path, action, attempts))
                    del self._queue[path]
        if not due:
            return 0
        updated = 0
        removed = 0
        skipped = 0
        for path, action, attempts in due:
            try:
                with self._lock:
                    if action == "delete":
                        status, _ = self._remove_single_file(
                            path,
                            db_path=self.db_path,
                            silo_slug=self.silo_slug,
                            update_counts=True,
                        )
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
            except Exception as exc:
                status = "error"
                self._log(f"{self.label}: error processing {path}: {exc}")
            if status == "error":
                next_attempt = attempts + 1
                delay = self._retry_delay(next_attempt)
                self._queue_action(path, action, delay=delay, attempts=next_attempt)
                self._log(f"{self.label}: retrying {Path(path).name} in {int(delay)}s")
                continue
            if status == "updated":
                updated += 1
            elif status == "removed":
                removed += 1
            else:
                skipped += 1
        processed = updated + removed
        if processed or skipped:
            self._log(f"{self.label}: +{updated} updated, -{removed} removed, {skipped} skipped")
        return processed

    def _process_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._drain_due()
            except Exception as exc:
                self._log(f"{self.label}: queue loop error: {exc}")
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

        queued_updates = 0
        queued_removes = 0
        skipped = 0
        if isinstance(manifest_files, dict):
            for path_str in list(manifest_files.keys()):
                if path_str not in current:
                    self._queue_action(path_str, "delete")
                    queued_removes += 1
        for path_str, (mtime, size) in current.items():
            prev = manifest_files.get(path_str) if isinstance(manifest_files, dict) else None
            if prev and prev.get("mtime") == mtime and prev.get("size") == size:
                continue
            self._queue_action(path_str, "update")
            queued_updates += 1
        return (queued_updates, queued_removes, skipped)

    def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(self.interval)
            if self._stop.is_set():
                break
            queued_updates, queued_removes, skipped = self._reconcile_once()
            if queued_updates or queued_removes or skipped:
                self._log(
                    f"Check for missed changes: +{queued_updates} queued, -{queued_removes} queued, {skipped} skipped"
                )

    def run(self) -> None:
        if self.startup_message:
            self._log(self.startup_message)
        else:
            self._log(
                f"Watching {self.label} (check every {int(self.interval)}s, wait {self.debounce}s after edits). Ctrl+C to stop."
            )
        self._observer.schedule(self._handler, str(self.root), recursive=True)
        self._observer.start()
        worker = threading.Thread(target=self._process_loop, daemon=True)
        recon = threading.Thread(target=self._reconcile_loop, daemon=True)
        worker.start()
        recon.start()
        queued_updates, queued_removes, skipped = self._reconcile_once()
        if queued_updates or queued_removes or skipped:
            self._log(
                f"Check for missed changes: +{queued_updates} queued, -{queued_removes} queued, {skipped} skipped"
            )
        try:
            while not self._stop.is_set():
                time.sleep(1.0)
        finally:
            self._stop.set()
            self._observer.stop()
            self._observer.join()


def _run_watcher(watcher: SiloWatcher, db_path: str | Path, silo_slug: str) -> int:
    root_path = getattr(watcher, "root", None)
    lock_path, lock_error = _acquire_silo_pid_lock(db_path, silo_slug, root_path=root_path)
    if lock_error:
        print(lock_error, file=sys.stderr)
        return 1
    previous_handlers: dict[int, object] = {}

    def _request_stop(_signum, _frame) -> None:
        watcher.stop()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            previous_handlers[sig] = signal.signal(sig, _request_stop)
        except Exception:
            continue
    try:
        watcher.run()
    except KeyboardInterrupt:
        watcher.stop()
    finally:
        for sig, previous in previous_handlers.items():
            try:
                signal.signal(sig, previous)
            except Exception:
                pass
        _release_silo_pid_lock(lock_path)
    return 0


def _record_source_path(path: Path) -> None:
    reg = _read_registry()
    bookmarks = reg.get("bookmarks", [])
    entry = {"path": str(path), "name": path.name}
    replaced = False
    for idx, src in enumerate(bookmarks):
        if src.get("path") == str(path):
            bookmarks[idx] = entry
            replaced = True
            break
    if not replaced:
        bookmarks.append(entry)
    reg["bookmarks"] = bookmarks
    _write_registry(reg)


def _set_silo_prompt_for_path(path: Path, prompt: str | None, clear_prompt: bool = False) -> bool:
    """Persist (or clear) prompt override for the silo mapped to the given path."""
    _ensure_src_on_path()
    from state import set_silo_prompt_override

    db_path = os.environ.get("LLMLIBRARIAN_DB", _DEFAULT_DB)
    llmli_registry = _read_llmli_registry(db_path)
    slug = _resolve_llmli_silo_by_path(llmli_registry, path)
    if slug is None:
        return False
    target_prompt = None if clear_prompt else prompt
    return set_silo_prompt_override(db_path, slug, target_prompt)


def _pull_path_mode(
    path_input: str | Path,
    allow_cloud: bool = False,
    follow_symlinks: bool = False,
    full: bool = False,
    prompt: str | None = None,
    clear_prompt: bool = False,
    image_vision: bool | None = None,
    workers: int | None = None,
    embedding_workers: int | None = None,
    extra_env: dict[str, str] | None = None,
) -> int:
    path = Path(path_input).resolve()
    if not path.is_dir():
        print(f"Error: not a directory: {path}", file=sys.stderr)
        return 1

    _ensure_src_on_path()
    # Apply suppression env vars before calling run_add directly so log noise is
    # controlled the same way the subprocess approach did.
    suppress_env = {
        "LLMLIBRARIAN_INGEST_LOG_LEVEL": "FATAL",
        "LLMLIBRARIAN_PROCESSOR_LOG_LEVEL": "ERROR",
    }
    merged_env = {**(extra_env or {}), **suppress_env}
    _prev_env: dict[str, str | None] = {}
    for k, v in merged_env.items():
        _prev_env[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        from ingest import run_add
        db_path = os.environ.get("LLMLIBRARIAN_DB", _DEFAULT_DB)
        files_ok, n_failures = run_add(
            path=path,
            db_path=db_path,
            allow_cloud=allow_cloud,
            follow_symlinks=follow_symlinks,
            incremental=not full,
            image_vision_enabled=image_vision,
            workers=workers,
            embedding_workers=embedding_workers,
        )
        code = 0 if n_failures == 0 or files_ok > 0 else 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        code = 1
    finally:
        for k, prev in _prev_env.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev

    if code == 0:
        _record_source_path(path)
        if prompt is not None or clear_prompt:
            if not _set_silo_prompt_for_path(path, prompt, clear_prompt=clear_prompt):
                print("Error: unable to resolve silo for prompt override.", file=sys.stderr)
                return 1
        _sync_daemon_if_installed()
    return code


def _pull_watch_path_mode(
    path_input: str | Path,
    interval: float,
    debounce: float,
    allow_cloud: bool,
    follow_symlinks: bool,
    prompt: str | None = None,
    clear_prompt: bool = False,
    image_vision: bool | None = None,
    workers: int | None = None,
    embedding_workers: int | None = None,
) -> int:
    if Observer is None:
        print("Error: watchdog is not installed. Install `watchdog` to use --watch.", file=sys.stderr)
        return 1
    path = Path(path_input).resolve()
    watch_env = {
        "LLMLIBRARIAN_PROCESSOR_LOG_LEVEL": "ERROR",
        "LLMLIBRARIAN_INGEST_LOG_LEVEL": "FATAL",
        "LLMLIBRARIAN_SUPPRESS_RECOVERABLE_WARNINGS": "1",
    }
    previous_env = {key: os.environ.get(key) for key in watch_env}
    try:
        os.environ.update(watch_env)
        rc = _pull_path_mode(
            path,
            allow_cloud=allow_cloud,
            follow_symlinks=follow_symlinks,
            full=False,
            prompt=prompt,
            clear_prompt=clear_prompt,
            image_vision=image_vision,
            workers=workers,
            embedding_workers=embedding_workers,
            extra_env=watch_env,
        )
        if rc != 0:
            return rc
        db_path = os.environ.get("LLMLIBRARIAN_DB", _DEFAULT_DB)
        llmli_registry = _read_llmli_registry(db_path)
        slug = _resolve_llmli_silo_by_path(llmli_registry, path)
        if slug is None:
            print("Error: unable to resolve silo slug for watched folder.", file=sys.stderr)
            return 1
        watcher = SiloWatcher(
            path,
            db_path,
            interval=interval,
            debounce=debounce,
            silo_slug=slug,
            allow_cloud=allow_cloud,
            label="this folder",
        )
        return _run_watcher(watcher, db_path, slug)
    finally:
        for key, previous in previous_env.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def _argv_requests_watch_pull(argv: list[str]) -> bool:
    args = [str(a).strip() for a in argv if str(a).strip()]
    if not args or args[0] != "pull":
        return False
    return "--watch" in args


def _apply_watch_process_env() -> None:
    os.environ["LLMLIBRARIAN_PROCESSOR_LOG_LEVEL"] = "ERROR"
    os.environ["LLMLIBRARIAN_INGEST_LOG_LEVEL"] = "FATAL"
    os.environ["LLMLIBRARIAN_SUPPRESS_RECOVERABLE_WARNINGS"] = "1"


def _pull_watch_status_mode(path_input: str | None, json_output: bool, prune_stale: bool) -> int:
    db_path = os.environ.get("LLMLIBRARIAN_DB", _DEFAULT_DB)
    opt_path = Path(path_input).resolve() if path_input else None
    records, err = _status_records(db_path, opt_path)
    if err:
        print(err, file=sys.stderr)
        return 1
    prune_result = None
    if prune_stale:
        prune_result = _prune_stale_locks(records)
        records, _ = _status_records(db_path, opt_path)
    if json_output:
        payload: dict[str, object] = {"records": records}
        if prune_result is not None:
            payload["prune"] = prune_result
        print(json.dumps(payload, indent=2))
        return 0
    _render_watch_status(records)
    if prune_result is not None:
        failed = prune_result.get("failed") or []
        print(f"Pruned stale locks: removed={prune_result.get('removed', 0)} failed={len(failed)}")
        for path in failed:
            print(f"  failed: {path}", file=sys.stderr)
    return 0


def _pull_watch_stop_mode(target: str, json_output: bool, timeout: float = 3.0) -> int:
    db_path = os.environ.get("LLMLIBRARIAN_DB", _DEFAULT_DB)
    records, _ = _status_records(db_path, None)
    record, err = _resolve_stop_target(target, records)
    if err:
        if json_output:
            print(json.dumps({"error": err}, indent=2))
        else:
            print(err, file=sys.stderr)
        return 1
    result = _stop_watch_process(record or {}, timeout=timeout)
    ok = str(result.get("status")) in {"stopped", "stale_lock_removed"}
    if json_output:
        print(json.dumps(result, indent=2))
    else:
        msg = str(result.get("message") or "")
        line = f"{result.get('silo')} (pid {result.get('pid')}): {result.get('status')}"
        if msg:
            line = f"{line} - {msg}"
        stream = sys.stdout if ok else sys.stderr
        print(line, file=stream)
    return 0 if ok else 1


def _pull_status_line(current: int, total: int, name: str, detail: str = "") -> None:
    """Overwrite a single terminal line with pull progress."""
    is_tty = sys.stderr.isatty()
    bar_width = 20
    filled = int(bar_width * current / total) if total else bar_width
    bar = "\u2588" * filled + "\u2591" * (bar_width - filled)
    suffix = f"  {detail}" if detail else ""
    line = f"\r  [{bar}] {current}/{total}  {name}{suffix}"
    if is_tty:
        sys.stderr.write(f"\033[2K{line}")
        sys.stderr.flush()
    else:
        print(line.strip())


def _build_pull_env(status_file_path: str) -> dict[str, str]:
    """Minimize env passthrough for pull subprocesses while keeping runtime essentials."""
    allow_exact = {
        "PATH", "HOME", "SHELL", "TERM", "LANG", "LC_ALL", "TMPDIR",
        "VIRTUAL_ENV", "OLLAMA_HOST", "NO_PROXY", "HTTP_PROXY", "HTTPS_PROXY",
    }
    out: dict[str, str] = {}
    for k, v in os.environ.items():
        if k in allow_exact or k.startswith("LLMLIBRARIAN_"):
            out[k] = v
    _cli, src = _resolve_llmli_paths()
    _prepend_pythonpath(out, src)
    out["LLMLIBRARIAN_STATUS_FILE"] = status_file_path
    out["LLMLIBRARIAN_QUIET"] = "1"
    return out


def pull_all_sources(
    full: bool = False,
    allow_cloud: bool = False,
    follow_symlinks: bool = False,
    image_vision: bool | None = None,
    workers: int | None = None,
    embedding_workers: int | None = None,
) -> int:
    """Pull all registered sources. Returns exit code."""
    reg = _read_registry()
    bookmarks = reg.get("bookmarks", [])
    if not bookmarks:
        print("No registered folders. Use: pal pull <path>", file=sys.stderr)
        return 1
    import tempfile
    is_tty = sys.stderr.isatty()
    total = len(bookmarks)
    fail_count = 0
    updated_silos: list[str] = []
    failed_silos: list[str] = []
    for idx, src in enumerate(bookmarks, 1):
        path = src.get("path")
        if not path:
            continue
        name = src.get("name") or Path(path).name
        _pull_status_line(idx, total, name)

        llmli_args = ["add"]
        if full:
            llmli_args.append("--full")
        if allow_cloud:
            llmli_args.append("--allow-cloud")
        if follow_symlinks:
            llmli_args.append("--follow-symlinks")
        if image_vision:
            llmli_args.append("--image-vision")
        if workers is not None:
            llmli_args.extend(["--workers", str(workers)])
        if embedding_workers is not None:
            llmli_args.extend(["--embedding-workers", str(embedding_workers)])
        llmli_args.append(path)

        status_file = tempfile.NamedTemporaryFile(prefix="llmli_status_", delete=False)
        status_file_path = status_file.name
        status_file.close()
        env = _build_pull_env(status_file_path)
        cli_path, _src = _resolve_llmli_paths()
        cmd = [sys.executable, str(cli_path)] + llmli_args
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

    if is_tty:
        sys.stderr.write("\033[2K\r")
        sys.stderr.flush()

    if updated_silos:
        print(f"Updated: {', '.join(updated_silos)}")
    if failed_silos:
        print(f"Failed: {', '.join(failed_silos)}", file=sys.stderr)
    if not updated_silos and not failed_silos:
        print("All silos up to date.")
    return 0 if fail_count == 0 else 1


def _daemon_workdir() -> str:
    cwd = Path.cwd().resolve()
    if (cwd / "pal.py").exists() and (cwd / "cli.py").exists():
        return str(cwd)
    return str(Path(__file__).resolve().parent)


def _daemon_runtime_metadata(manager: str | None = None) -> dict[str, object]:
    detected_manager = manager or jobsrt.supported_service_manager()
    if not detected_manager:
        raise RuntimeError(f"Unsupported platform for daemon services: {sys.platform}")
    db_path = Path(os.environ.get("LLMLIBRARIAN_DB", _DEFAULT_DB)).resolve()
    return {
        "version": 1,
        "manager": detected_manager,
        "python_executable": str(Path(sys.executable).resolve()),
        "pal_path": str(Path(__file__).resolve()),
        "workdir": _daemon_workdir(),
        "db_path": str(db_path),
        "pal_home": str(PAL_HOME.resolve()),
        "installed_at": int(time.time()),
    }


def _daemon_metadata() -> dict[str, object] | None:
    return jobsrt.read_daemon_metadata(PAL_HOME)


def _daemon_is_installed() -> bool:
    return _daemon_metadata() is not None


def _daemon_env(db_path: str | Path) -> dict[str, str]:
    env = os.environ.copy()
    env["LLMLIBRARIAN_DB"] = str(Path(db_path).resolve())
    env["PAL_HOME"] = str(PAL_HOME.resolve())
    return env


def _derive_watch_jobs_for_daemon(manager: str, db_path: str | Path | None = None) -> tuple[list[jobsrt.JobSpec], list[str]]:
    target_db = db_path or os.environ.get("LLMLIBRARIAN_DB", _DEFAULT_DB)
    return jobsrt.derive_watch_jobs(
        _read_registry(),
        _read_llmli_registry(target_db),
        pal_home=PAL_HOME,
        db_path=target_db,
        manager=manager,
    )


def _sync_daemon_services(emit_output: bool = True) -> int:
    existing = _daemon_metadata()
    if not existing:
        if emit_output:
            print("Daemon is not installed. Use: pal daemon install", file=sys.stderr)
        return 1
    manager_name = str(existing.get("manager") or "")
    refreshed = _daemon_runtime_metadata(manager_name)
    jobsrt.write_daemon_metadata(PAL_HOME, refreshed)
    db_path = str(refreshed["db_path"])
    jobs, warnings = _derive_watch_jobs_for_daemon(manager_name, db_path=db_path)
    manager = jobsrt.PlatformManager(manager_name)
    result = manager.sync(
        jobs,
        python_executable=str(refreshed["python_executable"]),
        pal_path=str(refreshed["pal_path"]),
        workdir=str(refreshed["workdir"]),
        env=_daemon_env(db_path),
    )
    if emit_output:
        print(f"Daemon manager: {manager_name}")
        print(f"Jobs: {len(jobs)}")
        if warnings:
            print(f"  ({len(warnings)} sources skipped — not indexed or missing)")
        print(f"Services written: {len(result.get('written') or [])}")
        print(f"Services removed: {len(result.get('removed') or [])}")
        errors = result.get("errors") or []
        if errors:
            print("Errors:", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
    return 0 if not (result.get("errors") or []) else 1


def _sync_daemon_if_installed() -> None:
    if not _daemon_is_installed():
        return
    rc = _sync_daemon_services(emit_output=False)
    if rc != 0:
        print("Warning: daemon sync failed. Run `pal daemon sync`.", file=sys.stderr)


def _resolve_job_target(target: str, jobs: list[jobsrt.JobSpec]) -> tuple[jobsrt.JobSpec | None, str | None]:
    raw = (target or "").strip()
    if not raw:
        return None, "Error: missing daemon target."
    lowered = raw.lower()
    # Exact match pass
    for job in jobs:
        if job.slug.lower() == lowered or job.service_name.lower() == lowered:
            return job, None
        if Path(job.source_path).name.lower() == lowered:
            return job, None
    # Full path match
    try:
        normalized = str(Path(raw).expanduser().resolve())
    except Exception:
        normalized = raw
    matches = [job for job in jobs if str(Path(job.source_path).resolve()) == normalized]
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, f"Error: ambiguous daemon target '{target}'."
    # Prefix match pass (e.g. "much-thinks" matches "much-thinks-d3e14819")
    slug_try = lowered.replace(" ", "-")
    prefix_matches = [j for j in jobs if j.slug.lower().startswith(slug_try)]
    if len(prefix_matches) == 1:
        return prefix_matches[0], None
    if len(prefix_matches) > 1:
        return None, f"Error: ambiguous daemon target '{target}' (matches: {', '.join(j.slug for j in prefix_matches)})."
    return None, f"Error: no daemon job found for '{target}'."


def _tail_file(path: Path, lines: int = 100) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except Exception:
        return []


def _daemon_status_rows() -> tuple[dict[str, object] | None, list[jobsrt.JobSpec], list[str], list[dict]]:
    metadata = _daemon_metadata()
    if not metadata:
        return None, [], [], []
    manager_name = str(metadata.get("manager") or "")
    jobs, warnings = _derive_watch_jobs_for_daemon(manager_name, db_path=str(metadata.get("db_path") or _DEFAULT_DB))
    records, _ = _status_records(str(metadata.get("db_path") or _DEFAULT_DB), None)
    return metadata, jobs, warnings, records


# --- Shell completion helpers ---

def _complete_silo(incomplete: str) -> list[str]:
    """Return silo slugs/display-names that match the incomplete prefix."""
    try:
        _ensure_src_on_path()
        from state import list_silos
        db = os.environ.get("LLMLIBRARIAN_DB", _DEFAULT_DB)
        silos = list_silos(db)
        results = []
        for s in silos:
            slug = s.get("slug", "")
            display = s.get("display_name", "")
            if slug and slug.startswith(incomplete):
                results.append(slug)
            if display and display != slug and display.startswith(incomplete):
                results.append(display)
        return results
    except Exception:
        return []


def _complete_job_target(incomplete: str) -> list[str]:
    """Return daemon job slugs that match the incomplete prefix."""
    try:
        metadata = _daemon_metadata()
        if not metadata:
            return []
        manager_name = str(metadata.get("manager") or "")
        jobs, _ = _derive_watch_jobs_for_daemon(manager_name, db_path=str(metadata.get("db_path") or _DEFAULT_DB))
        return [j.slug for j in jobs if j.slug.startswith(incomplete)]
    except Exception:
        return []


# --- Typer CLI ---

app = typer.Typer(
    name="pal",
    help="Index folders, ask questions, stay in sync.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
daemon_app = typer.Typer(help="Install and manage background silo watchers.", add_completion=False, invoke_without_command=True)
jobs_app = typer.Typer(help="Inspect derived background jobs.", add_completion=False, invoke_without_command=True)
app.add_typer(daemon_app, name="daemon")
app.add_typer(jobs_app, name="jobs")


@daemon_app.callback()
def daemon_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _daemon_status_impl()


@jobs_app.callback()
def jobs_callback(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _jobs_ls_impl()


def _exit(rc: int) -> None:
    if rc != 0:
        raise typer.Exit(code=rc)


@app.command("pull", help="Index a folder (or refresh all).")
def pull_command(
    path: str | None = typer.Argument(None, metavar="PATH", help="Folder to index. Omit to refresh all."),
    watch: bool = typer.Option(False, "--watch", help="Stay running and sync changes live."),
    status: bool = typer.Option(False, "--status", help="Show watcher status (all, or only PATH when provided)."),
    stop: str | None = typer.Option(None, "--stop", metavar="TARGET", help="Stop watcher by pid, silo slug/display name, or watched path.", autocompletion=_complete_silo),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON for --status/--stop."),
    prune_stale: bool = typer.Option(False, "--prune-stale", help="Remove stale watcher locks (status mode only)."),
    full: bool = typer.Option(False, "--full", help="Full rebuild (delete + re-add)."),
    prompt: str | None = typer.Option(None, "--prompt", help="Custom system prompt override for this silo."),
    clear_prompt: bool = typer.Option(False, "--clear-prompt", help="Clear custom prompt override for this silo."),
    allow_cloud: bool = typer.Option(False, "--allow-cloud", help="Allow cloud-synced folders."),
    image_vision: bool = typer.Option(False, "--image-vision", help="Enable multimodal image summaries for this silo (default: off unless previously enabled)."),
    workers: int | None = typer.Option(None, "--workers", help="Override file/extraction worker count for this run."),
    embedding_workers: int | None = typer.Option(None, "--embedding-workers", help="Override embedding worker count for this run."),
    interval: float = typer.Option(10.0, "--interval", help="Reconcile interval (watch mode).", hidden=True),
    debounce: float = typer.Option(1.0, "--debounce", help="Debounce delay (watch mode).", hidden=True),
    follow_symlinks: bool = typer.Option(False, "--follow-symlinks", help="Follow symlinks.", hidden=True),
) -> None:
    image_vision_requested: bool | None = True if image_vision else None
    mode_count = int(bool(watch)) + int(bool(status)) + int(bool(stop))
    if mode_count > 1:
        print("Use only one operation mode: --watch, --status, or --stop.", file=sys.stderr)
        raise typer.Exit(code=2)

    if status:
        if prompt is not None or clear_prompt or full or watch or stop is not None or image_vision_requested is not None or workers is not None or embedding_workers is not None:
            print("--status cannot be combined with --watch/--stop/--full/--prompt/--clear-prompt/--image-vision/--workers.", file=sys.stderr)
            raise typer.Exit(code=2)
        _exit(_pull_watch_status_mode(path, json_output=json_output, prune_stale=prune_stale))
        return

    if stop is not None:
        if path:
            print("--stop does not accept PATH. Use: pal pull --stop <target>", file=sys.stderr)
            raise typer.Exit(code=2)
        if watch or prompt is not None or clear_prompt or full or prune_stale or image_vision_requested is not None or workers is not None or embedding_workers is not None:
            print("--stop cannot be combined with --watch/--full/--prompt/--clear-prompt/--prune-stale/--image-vision/--workers.", file=sys.stderr)
            raise typer.Exit(code=2)
        _exit(_pull_watch_stop_mode(stop, json_output=json_output))
        return

    if prune_stale:
        print("--prune-stale is only valid with --status.", file=sys.stderr)
        raise typer.Exit(code=2)
    if json_output:
        print("--json is only valid with --status or --stop.", file=sys.stderr)
        raise typer.Exit(code=2)

    if prompt is not None and clear_prompt:
        print("Use either --prompt or --clear-prompt, not both.", file=sys.stderr)
        raise typer.Exit(code=2)
    if (prompt is not None or clear_prompt) and not path:
        print("--prompt/--clear-prompt requires PATH. Use: pal pull <path> --prompt \"...\"", file=sys.stderr)
        raise typer.Exit(code=2)
    if prompt is not None and not prompt.strip():
        print("Blank --prompt is not allowed. Use --clear-prompt to remove an override.", file=sys.stderr)
        raise typer.Exit(code=2)
    if watch and not path:
        print("--watch requires PATH. Use: pal pull <path> --watch", file=sys.stderr)
        raise typer.Exit(code=2)
    if watch and path:
        _exit(
            _pull_watch_path_mode(
                path,
                interval,
                debounce,
                allow_cloud,
                follow_symlinks,
                prompt=prompt,
                clear_prompt=clear_prompt,
                image_vision=image_vision_requested,
                workers=workers,
                embedding_workers=embedding_workers,
            )
        )
        return
    if path:
        _exit(
            _pull_path_mode(
                path,
                allow_cloud=allow_cloud,
                follow_symlinks=follow_symlinks,
                full=full,
                prompt=prompt,
                clear_prompt=clear_prompt,
                image_vision=image_vision_requested,
                workers=workers,
                embedding_workers=embedding_workers,
            )
        )
        return
    _exit(
        pull_all_sources(
            full=full,
            allow_cloud=allow_cloud,
            follow_symlinks=follow_symlinks,
            image_vision=image_vision_requested,
            workers=workers,
            embedding_workers=embedding_workers,
        )
    )


@app.command("ask", help="Ask a question across your indexed data.")
def ask_command(
    query: list[str] = typer.Argument(..., metavar="QUESTION"),
    in_silo: str | None = typer.Option(None, "--in", help="Query only this silo.", autocompletion=_complete_silo),
    unified: bool = typer.Option(False, "--unified", help="Combine results from all silos."),
    strict: bool = typer.Option(False, "--strict", help="Only answer when evidence is strong; say unknown otherwise."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Answer only (quiet output for scripts)."),
    explain: bool = typer.Option(False, "--explain", help="Print deterministic catalog diagnostics to stderr when applicable."),
    force: bool = typer.Option(False, "--force", help="Allow deterministic catalog queries on stale scope."),
) -> None:
    db_path = os.environ.get("LLMLIBRARIAN_DB", _DEFAULT_DB)
    in_silo_norm, query_norm, scope_err = _normalize_natural_ask_scope(query, in_silo, db_path)
    if scope_err:
        print(scope_err, file=sys.stderr)
        raise typer.Exit(code=2)
    if in_silo_norm:
        in_silo = in_silo_norm
        query = query_norm

    ensure_self_silo(force=False, emit_warning=False)
    # Only surface self-silo staleness when the query explicitly targets __self__.
    # This avoids noisy warnings while asking other silos during active development.
    should_warn_self_stale = bool(in_silo == "__self__")
    if not quiet and should_warn_self_stale and _should_require_self_silo():
        repo_root = _get_git_root()
        if repo_root is not None and _is_dev_repo_at_root(repo_root):
            llmli_registry = _read_llmli_registry(db_path)
            self_entry = llmli_registry.get("__self__") if isinstance(llmli_registry, dict) else None
            self_path = (self_entry or {}).get("path") if isinstance(self_entry, dict) else None
            if self_path and str(Path(self_path).resolve()) == str(repo_root.resolve()):
                reg = _read_registry()
                if _self_silo_is_stale(repo_root, self_entry, reg):
                    print("⚠ Index may be outdated. Run `pal sync` for best results.")
    llmli_args = ["ask"]
    if unified:
        llmli_args.append("--unified")
    if in_silo and not unified:
        llmli_args.extend(["--in", in_silo])
    if strict:
        llmli_args.append("--strict")
    if quiet:
        llmli_args.append("--quiet")
    if explain:
        llmli_args.append("--explain")
    if force:
        llmli_args.append("--force")
    llmli_args.extend(query)
    _exit(_run_llmli(llmli_args))


@app.command("ls", help="List indexed silos.")
def ls_command() -> None:
    _exit(_run_llmli(["ls"]))


@app.command("inspect", help="Show details for a silo.")
def inspect_command(
    silo: str = typer.Argument(..., help="Silo slug or display name.", autocompletion=_complete_silo),
    top: int | None = typer.Option(None, "--top", help="Show top N files by chunks."),
    filter: str | None = typer.Option(None, "--filter", help="Show only pdf, docx, or code."),
) -> None:
    llmli_args = ["inspect", silo]
    if top is not None:
        llmli_args.extend(["--top", str(top)])
    if filter:
        llmli_args.extend(["--filter", filter])
    _exit(_run_llmli(llmli_args))


@app.command("capabilities", help="Supported file types.")
def capabilities_command() -> None:
    ensure_self_silo(force=False)
    _exit(_run_llmli(["capabilities"]))


@app.command("log", help="Show recent failures.")
def log_command() -> None:
    _exit(_run_llmli(["log", "--last"]))


@app.command("remove", help="Remove a silo.")
def remove_command(
    silo: list[str] = typer.Argument(..., help="Silo slug, display name, or path.", autocompletion=_complete_silo),
) -> None:
    name = " ".join(silo) if isinstance(silo, list) else str(silo)
    db_path = os.environ.get("LLMLIBRARIAN_DB", _DEFAULT_DB)
    source_path = _resolve_registry_source_for_remove(name, db_path)
    _ensure_src_on_path()
    from operations import op_remove_silo
    result = op_remove_silo(db_path, name)
    if result.get("chroma_warning"):
        print(f"Warning: {result['chroma_warning']}", file=sys.stderr)
    if result["not_found"]:
        print(f"Removed chunks and file registry for silo: {result['cleaned_slug']} (was not in silo list)")
    else:
        print(f"Removed silo: {result['removed_slug']}")
    if source_path:
        _remove_source_path(source_path)
    else:
        _remove_source_path(name)
    _sync_daemon_if_installed()


@app.command("sync", help="Re-index the project's own source (dev mode).")
def sync_command() -> None:
    _exit(ensure_self_silo(force=True))


@daemon_app.command("install", help="Install user-space watch services for registered silos.")
def daemon_install_command() -> None:
    metadata = _daemon_runtime_metadata()
    PAL_HOME.mkdir(parents=True, exist_ok=True)
    jobsrt.watch_log_dir(PAL_HOME).mkdir(parents=True, exist_ok=True)
    jobsrt.write_daemon_metadata(PAL_HOME, metadata)
    _exit(_sync_daemon_services(emit_output=True))


@daemon_app.command("sync", help="Reconcile daemon services against registered silos.")
def daemon_sync_command() -> None:
    _exit(_sync_daemon_services(emit_output=True))


def _daemon_status_impl() -> None:
    metadata, jobs, warnings, records = _daemon_status_rows()
    if not metadata:
        print("Daemon: not installed")
        return
    print("Daemon")
    print(f"  Installed: yes")
    print(f"  Manager: {metadata.get('manager')}")
    print(f"  Python: {metadata.get('python_executable')}")
    print(f"  Jobs: {len(jobs)}")
    if warnings:
        print(f"  ({len(warnings)} sources skipped — not indexed or missing)")
    record_by_slug = {str((record or {}).get("silo") or ""): record for record in records if isinstance(record, dict)}
    print("\nSilo                      State      Service")
    print("---------------------------------------------------------------")
    manager_name = str(metadata.get("manager") or "")
    manager = jobsrt.PlatformManager(manager_name)
    for job in jobs:
        record = record_by_slug.get(job.slug)
        state = str((record or {}).get("state") or "installed")
        if not manager.desired_path(job.slug).exists():
            state = "missing"
        print(f"{job.slug:24} {state:10} {job.service_name}")


@daemon_app.command("status", help="Show daemon install state and job health.")
def daemon_status_command() -> None:
    _daemon_status_impl()


@daemon_app.command("logs", help="Show recent daemon logs for one silo.")
def daemon_logs_command(
    target: str | None = typer.Argument(None, help="Silo slug, source path, or service name.", autocompletion=_complete_job_target),
    lines: int = typer.Option(100, "--lines", min=1, help="Number of log lines to show."),
) -> None:
    metadata = _daemon_metadata()
    if not metadata:
        print("Daemon is not installed. Use: pal daemon install", file=sys.stderr)
        raise typer.Exit(code=1)
    manager_name = str(metadata.get("manager") or "")
    jobs, _warnings = _derive_watch_jobs_for_daemon(manager_name, db_path=str(metadata.get("db_path") or _DEFAULT_DB))
    if not target:
        if not jobs:
            print("No daemon jobs.")
            return
        print("Available silos (pass one as TARGET):")
        for job in jobs:
            last_line = ""
            try:
                lines_tail = _tail_file(Path(job.log_path), lines=1)
                last_line = lines_tail[-1] if lines_tail else ""
            except Exception:
                pass
            suffix = f"  {last_line}" if last_line else ""
            print(f"  {job.slug}{suffix}")
        return
    job, err = _resolve_job_target(target, jobs)
    if err:
        print(err, file=sys.stderr)
        raise typer.Exit(code=1)
    out_lines = _tail_file(Path(job.log_path), lines=lines)
    err_lines = _tail_file(jobsrt.watch_stderr_log_path(PAL_HOME, job.slug), lines=lines)
    print(f"# {job.slug} stdout")
    for line in out_lines:
        print(line)
    if err_lines:
        print(f"\n# {job.slug} stderr")
        for line in err_lines:
            print(line)


@daemon_app.command("uninstall", help="Remove all daemon-managed services.")
def daemon_uninstall_command() -> None:
    metadata = _daemon_metadata()
    if not metadata:
        print("Daemon: not installed")
        return
    manager = jobsrt.PlatformManager(str(metadata.get("manager") or ""))
    result = manager.uninstall_all()
    jobsrt.remove_daemon_metadata(PAL_HOME)
    print(f"Removed services: {len(result.get('removed') or [])}")
    errors = result.get("errors") or []
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        raise typer.Exit(code=1)


def _jobs_ls_impl() -> None:
    metadata = _daemon_metadata()
    manager_name = str((metadata or {}).get("manager") or jobsrt.supported_service_manager() or "")
    if not manager_name:
        print(f"Unsupported platform for daemon jobs: {sys.platform}", file=sys.stderr)
        raise typer.Exit(code=1)
    db_path = str((metadata or {}).get("db_path") or os.environ.get("LLMLIBRARIAN_DB", _DEFAULT_DB))
    jobs, warnings = _derive_watch_jobs_for_daemon(manager_name, db_path=db_path)
    records, _ = _status_records(db_path, None)
    state_by_slug = {str((record or {}).get("silo") or ""): str((record or {}).get("state") or "installed") for record in records}
    if warnings:
        print(f"  ({len(warnings)} sources skipped — not indexed or missing)", file=sys.stderr)
    if not jobs:
        print("No daemon jobs.")
        return
    _ensure_src_on_path()
    from style import link_style
    no_color = not sys.stdout.isatty()
    print("Kind        Silo                      State      Service                      Log")
    print("-----------------------------------------------------------------------------------------------")
    for job in jobs:
        state = state_by_slug.get(job.slug, "installed")
        log_name = Path(job.log_path).name
        log_cell = link_style(no_color, f"file://{job.log_path}", log_name)
        print(f"{job.kind:10} {job.slug:24} {state:10} {job.service_name:28} {log_cell}")


@jobs_app.command("ls", help="List derived daemon jobs.")
def jobs_ls_command() -> None:
    _jobs_ls_impl()


@app.command("diff", help="Show files changed since last pull.")
def diff_command(
    silo: str = typer.Argument(..., help="Silo slug or display name.", autocompletion=_complete_silo),
) -> None:
    _ensure_src_on_path()
    from state import resolve_silo_to_slug, resolve_silo_prefix, list_silos
    from file_registry import _read_file_manifest
    from ingest import _load_limits_config, collect_files, ADD_DEFAULT_INCLUDE, ADD_DEFAULT_EXCLUDE

    db_path = os.environ.get("LLMLIBRARIAN_DB", _DEFAULT_DB)
    slug = resolve_silo_to_slug(db_path, silo) or resolve_silo_prefix(db_path, silo)
    if not slug:
        print(f"Error: silo not found: {silo}", file=sys.stderr)
        raise typer.Exit(code=1)

    silos = list_silos(db_path)
    info = next((s for s in silos if s.get("slug") == slug), None)
    root_path = (info or {}).get("path")
    if not root_path:
        print(f"Error: silo has no path: {slug}", file=sys.stderr)
        raise typer.Exit(code=1)
    root = Path(root_path).resolve()
    if not root.exists() or not root.is_dir():
        print(f"Error: silo path missing: {root}", file=sys.stderr)
        raise typer.Exit(code=1)

    manifest = _read_file_manifest(db_path)
    manifest_files = (((manifest.get("silos") or {}).get(slug) or {}).get("files") or {})
    if not isinstance(manifest_files, dict):
        manifest_files = {}

    changed: list[str] = []
    removed: list[str] = []
    for path_str, meta in manifest_files.items():
        p = Path(path_str)
        if not p.exists():
            removed.append(path_str)
            continue
        try:
            st = p.stat()
        except OSError:
            removed.append(path_str)
            continue
        if st.st_mtime != meta.get("mtime") or st.st_size != meta.get("size"):
            changed.append(path_str)

    max_file_bytes, max_depth, _max_archive_bytes, _max_files_per_zip, _max_extracted = _load_limits_config()
    current_entries = collect_files(
        root,
        ADD_DEFAULT_INCLUDE,
        ADD_DEFAULT_EXCLUDE,
        max_depth,
        max_file_bytes,
        follow_symlinks=False,
    )
    current_set = {str(p.resolve()) for p, _kind in current_entries}
    manifest_set = set(manifest_files.keys())
    added = sorted(current_set - manifest_set)
    changed = sorted(changed)
    removed = sorted(removed)

    if not changed and not added and not removed:
        print("No changes.")
        return

    print(f"{slug}: +{len(added)} added, ~{len(changed)} changed, -{len(removed)} removed")
    for f in added:
        print(f"  A {f}")
    for f in changed:
        print(f"  M {f}")
    for f in removed:
        print(f"  D {f}")


@app.command("status", help="Quick health check.")
def status_command() -> None:
    _ensure_src_on_path()
    from silo_audit import (
        load_registry,
        load_manifest,
        load_file_registry,
        find_count_mismatches,
        find_duplicate_hashes,
        find_path_overlaps,
    )

    db_path = os.environ.get("LLMLIBRARIAN_DB", _DEFAULT_DB)
    registry = load_registry(db_path)
    file_registry = load_file_registry(db_path)
    manifest = load_manifest(db_path)
    dupes = find_duplicate_hashes(file_registry)
    overlaps = find_path_overlaps(registry)
    mismatches = find_count_mismatches(registry, manifest)
    print(_render_health_summary(registry, dupes, overlaps, mismatches))

    _ensure_src_on_path()
    from operations import op_db_storage_summary

    stor = op_db_storage_summary(db_path)
    if "error" not in stor:
        print()
        print("On-disk index (Chroma persist folder)")
        print(f"  Folder size (approx): {_fmt_bytes_iec(int(stor.get('db_total_bytes') or 0))}")
        if int(stor.get("disk_free_bytes") or -1) >= 0:
            print(f"  Volume free space: {_fmt_bytes_iec(int(stor['disk_free_bytes']))}")
        for entry in stor.get("link_lists") or []:
            p = str(entry.get("path") or "")
            b = int(entry.get("bytes") or 0)
            print(f"  link_lists.bin: {_fmt_bytes_iec(b)}")
            if p:
                print(f"    ({p})")
        if stor.get("chroma_hnsw_bloat"):
            print("  WARNING: HNSW index file is abnormally large.")
            note = stor.get("chroma_hnsw_bloat_note")
            if note:
                print(f"    {note}")

    if not (dupes or overlaps or mismatches):
        return

    print("\nTop Issues")
    if mismatches:
        top = sorted(
            mismatches,
            key=lambda m: abs(int(m.get("registry_files", 0) or 0) - int(m.get("manifest_files", 0) or 0)),
            reverse=True,
        )[:3]
        reg_by_slug = {str((s or {}).get("slug") or ""): s for s in registry if isinstance(s, dict)}
        print(f"  Count mismatches: {len(mismatches)}")
        for m in top:
            slug = str(m.get("slug") or "?")
            rf = int(m.get("registry_files", 0) or 0)
            mf = int(m.get("manifest_files", 0) or 0)
            delta = mf - rf
            sign = "+" if delta >= 0 else ""
            path = str((reg_by_slug.get(slug) or {}).get("path") or "")
            action = _status_action_for_mismatch(slug, rf, mf, path or None)
            print(f"    - {slug}: indexed={rf}, manifest={mf} (delta {sign}{delta})")
            print(f"      action: {action}")
    if overlaps:
        print(f"  Path overlaps: {len(overlaps)}")
        first = overlaps[0]
        if first.get("type") == "nested":
            print(f"    - nested: {first.get('child')} inside {first.get('parent')}")
            print(f"      action: remove duplicate scope: pal remove {first.get('child')}")
        else:
            silos = first.get("silos") or []
            print(f"    - same path: {', '.join(str(s) for s in silos)}")
            if silos:
                print(f"      action: keep one scope: pal remove {silos[-1]}")
    if dupes:
        print(f"  Duplicate groups: {len(dupes)}")

    print("\nNext Actions")
    print("  1. Inspect full details: pal silos")
    print("  2. Re-index out-of-sync silos: pal pull --full")


@app.command("silos", help="Audit silo health.")
def silos_command() -> None:
    _ensure_src_on_path()
    from silo_audit import (
        load_registry,
        load_file_registry,
        load_manifest,
        find_duplicate_hashes,
        find_path_overlaps,
        find_count_mismatches,
    )
    db_path = os.environ.get("LLMLIBRARIAN_DB", _DEFAULT_DB)
    registry = load_registry(db_path)
    file_registry = load_file_registry(db_path)
    manifest = load_manifest(db_path)
    dupes = find_duplicate_hashes(file_registry)
    overlaps = find_path_overlaps(registry)
    mismatches = find_count_mismatches(registry, manifest)
    print(_render_health_summary(registry, dupes, overlaps, mismatches))

    reg_by_slug = {str((s or {}).get("slug") or ""): s for s in registry if isinstance(s, dict)}
    action_lines: list[str] = []

    if overlaps:
        print("\nPath Overlaps")
        for o in overlaps:
            if o.get("type") == "same_path":
                silos = [str(s) for s in (o.get("silos") or [])]
                path = str(o.get("path") or "?")
                print(f"  - same path: {', '.join(silos)}")
                print(f"    path: {path}")
                if silos:
                    action_lines.append(f"keep one overlapping scope: pal remove {silos[-1]}")
            else:
                parent = str(o.get("parent") or "?")
                child = str(o.get("child") or "?")
                print(f"  - nested scope: {child} inside {parent}")
                action_lines.append(f"remove nested duplicate scope: pal remove {child}")

    if mismatches:
        print("\nCount Mismatches")
        ordered = sorted(
            mismatches,
            key=lambda m: abs(int(m.get("registry_files", 0) or 0) - int(m.get("manifest_files", 0) or 0)),
            reverse=True,
        )
        for m in ordered:
            slug = str(m.get("slug") or "?")
            rf = int(m.get("registry_files", 0) or 0)
            mf = int(m.get("manifest_files", 0) or 0)
            delta = mf - rf
            sign = "+" if delta >= 0 else ""
            path = str((reg_by_slug.get(slug) or {}).get("path") or "")
            print(f"  - {slug}: indexed={rf}, manifest={mf} (delta {sign}{delta})")
            if path:
                print(f"    path: {path}")
            action_lines.append(_status_action_for_mismatch(slug, rf, mf, path or None))

    if dupes:
        print("\nDuplicate Content")
        print(f"  - {len(dupes)} duplicate hash group(s) found")

    if action_lines:
        deduped = list(dict.fromkeys(action_lines))
        print("\nRecommended Actions")
        for idx, action in enumerate(deduped, start=1):
            print(f"  {idx}. {action}")
    else:
        print("\nNo action needed.")


@app.command("tool", help="Pass-through to llmli.")
def tool_command(
    tool_name: str = typer.Argument(..., help="Tool name (e.g. llmli)."),
    tool_args: list[str] = typer.Argument(..., help="Arguments for the tool."),
) -> None:
    rest = [a for a in tool_args if a != "--"]
    if tool_name == "llmli":
        _exit(_run_llmli(rest))
        return
    print(f"Error: unknown tool '{tool_name}'. Use: pal tool llmli <args...>", file=sys.stderr)
    raise typer.Exit(code=1)


def main() -> int:
    if _argv_requests_watch_pull(sys.argv[1:]) and os.environ.get("LLMLIBRARIAN_WATCH_ENV_APPLIED") != "1":
        env = os.environ.copy()
        env["LLMLIBRARIAN_PROCESSOR_LOG_LEVEL"] = "ERROR"
        env["LLMLIBRARIAN_INGEST_LOG_LEVEL"] = "FATAL"
        env["LLMLIBRARIAN_SUPPRESS_RECOVERABLE_WARNINGS"] = "1"
        env["LLMLIBRARIAN_WATCH_ENV_APPLIED"] = "1"
        os.execvpe(sys.executable, [sys.executable] + sys.argv, env)
    if _argv_requests_watch_pull(sys.argv[1:]):
        _apply_watch_process_env()
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
