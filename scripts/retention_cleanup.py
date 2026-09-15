#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_LOG_DAYS = 30
DEFAULT_KEEP_NEWEST = 20
GENERATED_LOG_SUFFIXES = (".log", ".stdout.log", ".stderr.log")
GENERATED_LOG_PREFIXES = (
    "watch-",
    "llmlibrarian-",
    "chroma",
    "mcp",
    "usage",
)


@dataclass
class Candidate:
    path: Path
    age_days: int
    size: int


def default_log_dir() -> Path:
    pal_home = os.environ.get("PAL_HOME")
    return (Path(pal_home) if pal_home else Path("~/.pal").expanduser()) / "logs"


def generated_log(path: Path) -> bool:
    return (
        path.is_file()
        and path.name.endswith(GENERATED_LOG_SUFFIXES)
        and path.name.startswith(GENERATED_LOG_PREFIXES)
    )


def plan_deletes(
    log_dir: Path,
    *,
    log_days: int = DEFAULT_LOG_DAYS,
    keep_newest: int = DEFAULT_KEEP_NEWEST,
    now: datetime | None = None,
) -> list[Candidate]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=log_days)
    if not log_dir.exists():
        return []
    logs = sorted(
        (p for p in log_dir.iterdir() if generated_log(p)),
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )
    newest = {p.resolve() for p in logs[:keep_newest]}
    planned: list[Candidate] = []
    for path in logs:
        if path.resolve() in newest:
            continue
        stat = path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        if mtime <= cutoff:
            planned.append(Candidate(path=path, age_days=(now - mtime).days, size=stat.st_size))
    return planned


def run_cleanup(
    *,
    log_dir: Path,
    apply: bool,
    log_days: int,
    keep_newest: int,
) -> dict:
    planned = plan_deletes(log_dir, log_days=log_days, keep_newest=keep_newest)
    deleted: list[str] = []
    errors: list[str] = []
    if apply:
        for item in planned:
            try:
                item.path.unlink()
                deleted.append(str(item.path))
            except OSError as exc:
                errors.append(f"{item.path}: {exc}")
    return {
        "mode": "apply" if apply else "dry-run",
        "log_dir": str(log_dir),
        "policy": {
            "log_days": log_days,
            "keep_newest": keep_newest,
            "generated_only": True,
            "preserve_silos_and_chroma": True,
        },
        "planned": [item.__dict__ | {"path": str(item.path)} for item in planned],
        "deleted": deleted,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prune generated llmLibrarian logs.")
    parser.add_argument("--log-dir", type=Path, default=default_log_dir())
    parser.add_argument("--log-days", type=int, default=DEFAULT_LOG_DAYS)
    parser.add_argument("--keep-newest", type=int, default=DEFAULT_KEEP_NEWEST)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.log_days < 1 or args.keep_newest < 0:
        parser.error("--log-days must be >=1 and --keep-newest must be >=0")
    result = run_cleanup(
        log_dir=args.log_dir.expanduser(),
        apply=args.apply,
        log_days=args.log_days,
        keep_newest=args.keep_newest,
    )
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        action = "deleted" if args.apply else "would delete"
        print(f"llmLibrarian retention {result['mode']}: {action} {len(result['planned'])} generated logs")
        for item in result["planned"]:
            print(f"{action}: {item['path']}")
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
