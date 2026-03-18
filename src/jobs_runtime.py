from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SERVICE_DEBOUNCE_DEFAULT = 30.0
SERVICE_INTERVAL_DEFAULT = 60.0
SERVICE_RESTART_DELAY_DEFAULT = 15
SERVICE_PREFIX_LAUNCHD = "io.llmlibrarian.watch."
SERVICE_PREFIX_SYSTEMD = "llmlibrarian-watch-"
LOG_DIRNAME = "logs"


@dataclass
class JobSpec:
    id: str
    kind: str
    slug: str
    source_path: str
    service_name: str
    log_path: str
    interval: float
    debounce: float
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def daemon_metadata_path(pal_home: Path) -> Path:
    return pal_home / "daemon.json"


def read_daemon_metadata(pal_home: Path) -> dict[str, Any] | None:
    path = daemon_metadata_path(pal_home)
    if not path.exists():
        return None
    try:
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def write_daemon_metadata(pal_home: Path, payload: dict[str, Any]) -> Path:
    import json

    path = daemon_metadata_path(pal_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def remove_daemon_metadata(pal_home: Path) -> None:
    path = daemon_metadata_path(pal_home)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def supported_service_manager(platform_name: str | None = None) -> str | None:
    name = (platform_name or sys.platform).lower()
    if name.startswith("darwin"):
        return "launchd"
    if name.startswith("linux"):
        return "systemd"
    return None


def safe_service_suffix(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in str(value))
    cleaned = cleaned.strip("-_")
    return cleaned or "default"


def watch_log_dir(pal_home: Path) -> Path:
    return pal_home / LOG_DIRNAME


def watch_log_path(pal_home: Path, slug: str) -> Path:
    return watch_log_dir(pal_home) / f"watch-{safe_service_suffix(slug)}.log"


def watch_stderr_log_path(pal_home: Path, slug: str) -> Path:
    return watch_log_dir(pal_home) / f"watch-{safe_service_suffix(slug)}.stderr.log"


def launchd_agents_dir(home: Path | None = None) -> Path:
    root = home or Path.home()
    return root / "Library" / "LaunchAgents"


def systemd_user_dir(home: Path | None = None) -> Path:
    root = home or Path.home()
    return root / ".config" / "systemd" / "user"


def _launchd_label(slug: str) -> str:
    return f"{SERVICE_PREFIX_LAUNCHD}{safe_service_suffix(slug)}"


def _systemd_unit_name(slug: str) -> str:
    return f"{SERVICE_PREFIX_SYSTEMD}{safe_service_suffix(slug)}.service"


def desired_service_path(manager: str, slug: str, home: Path | None = None) -> Path:
    if manager == "launchd":
        return launchd_agents_dir(home) / f"{_launchd_label(slug)}.plist"
    if manager == "systemd":
        return systemd_user_dir(home) / _systemd_unit_name(slug)
    raise ValueError(f"Unsupported service manager: {manager}")


def desired_service_name(manager: str, slug: str) -> str:
    if manager == "launchd":
        return _launchd_label(slug)
    if manager == "systemd":
        return _systemd_unit_name(slug)
    raise ValueError(f"Unsupported service manager: {manager}")


def _env_for_service(env: dict[str, str] | None = None) -> dict[str, str]:
    allowed = {
        "PATH",
        "HOME",
        "SHELL",
        "TMPDIR",
        "VIRTUAL_ENV",
        "OLLAMA_HOST",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "LLMLIBRARIAN_DB",
        "PAL_HOME",
    }
    source = env if env is not None else os.environ
    out: dict[str, str] = {}
    for key, value in source.items():
        if key in allowed:
            out[key] = str(value)
    return out


def render_launchd_plist(
    job: JobSpec,
    *,
    python_executable: str,
    pal_path: str,
    workdir: str,
    env: dict[str, str] | None = None,
    stderr_path: str | None = None,
) -> str:
    payload = {
        "Label": job.service_name,
        "ProgramArguments": [
            python_executable,
            pal_path,
            "pull",
            job.source_path,
            "--watch",
            "--interval",
            str(int(job.interval)),
            "--debounce",
            str(int(job.debounce)),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": workdir,
        "EnvironmentVariables": _env_for_service(env),
        "StandardOutPath": job.log_path,
        "StandardErrorPath": stderr_path or job.log_path,
    }
    return plistlib.dumps(payload).decode("utf-8")


def _escape_systemd_env_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_systemd_unit(
    job: JobSpec,
    *,
    python_executable: str,
    pal_path: str,
    workdir: str,
    env: dict[str, str] | None = None,
    stderr_path: str | None = None,
    restart_sec: int = SERVICE_RESTART_DELAY_DEFAULT,
) -> str:
    exec_start = shlex.join(
        [
            python_executable,
            pal_path,
            "pull",
            job.source_path,
            "--watch",
            "--interval",
            str(int(job.interval)),
            "--debounce",
            str(int(job.debounce)),
        ]
    )
    env_lines = [
        f'Environment="{key}={_escape_systemd_env_value(value)}"'
        for key, value in sorted(_env_for_service(env).items())
    ]
    lines = [
        "[Unit]",
        f"Description=llmLibrarian watch silo {job.slug}",
        "After=default.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={workdir}",
        *env_lines,
        f"ExecStart={exec_start}",
        "Restart=always",
        f"RestartSec={int(restart_sec)}",
        f"StandardOutput=append:{job.log_path}",
        f"StandardError=append:{stderr_path or job.log_path}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _resolve_silo_by_path(llmli_registry: dict[str, Any], path: Path) -> tuple[str | None, dict[str, Any] | None]:
    target = str(path.resolve())
    for slug, data in llmli_registry.items():
        if not isinstance(data, dict):
            continue
        raw = data.get("path")
        if not raw:
            continue
        try:
            candidate = str(Path(str(raw)).resolve())
        except Exception:
            candidate = str(raw)
        if candidate == target:
            return str(slug), data
    return None, None


def derive_watch_jobs(
    source_registry: dict[str, Any],
    llmli_registry: dict[str, Any],
    *,
    pal_home: Path,
    db_path: str | Path,
    manager: str,
) -> tuple[list[JobSpec], list[str]]:
    jobs: list[JobSpec] = []
    warnings: list[str] = []
    seen: set[str] = set()
    sources = source_registry.get("sources", []) or []
    for raw in sources:
        if not isinstance(raw, dict):
            continue
        path_value = raw.get("path")
        if not path_value:
            continue
        try:
            path = Path(str(path_value)).expanduser().resolve()
        except Exception:
            warnings.append(f"Skipping unreadable source path: {path_value}")
            continue
        if str(path) in seen:
            continue
        seen.add(str(path))
        if not path.exists() or not path.is_dir():
            warnings.append(f"Skipping missing source: {path}")
            continue
        slug, _entry = _resolve_silo_by_path(llmli_registry, path)
        if not slug:
            warnings.append(f"Skipping unindexed source: {path}")
            continue
        service_name = desired_service_name(manager, slug)
        jobs.append(
            JobSpec(
                id=f"watch_silo:{slug}",
                kind="watch_silo",
                slug=slug,
                source_path=str(path),
                service_name=service_name,
                log_path=str(watch_log_path(pal_home, slug)),
                interval=SERVICE_INTERVAL_DEFAULT,
                debounce=SERVICE_DEBOUNCE_DEFAULT,
                enabled=True,
            )
        )
    jobs.sort(key=lambda item: item.slug)
    return jobs, warnings


def _run_command(cmd: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception as exc:
        return 1, str(exc)
    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    return int(result.returncode), output


class PlatformManager:
    def __init__(self, manager: str, home: Path | None = None) -> None:
        self.manager = manager
        self.home = home or Path.home()

    def service_dir(self) -> Path:
        if self.manager == "launchd":
            return launchd_agents_dir(self.home)
        if self.manager == "systemd":
            return systemd_user_dir(self.home)
        raise ValueError(f"Unsupported service manager: {self.manager}")

    def desired_path(self, slug: str) -> Path:
        return desired_service_path(self.manager, slug, self.home)

    def existing_service_paths(self) -> list[Path]:
        directory = self.service_dir()
        if not directory.exists():
            return []
        if self.manager == "launchd":
            return sorted(directory.glob(f"{SERVICE_PREFIX_LAUNCHD}*.plist"))
        return sorted(directory.glob(f"{SERVICE_PREFIX_SYSTEMD}*.service"))

    def write_service(
        self,
        job: JobSpec,
        *,
        python_executable: str,
        pal_path: str,
        workdir: str,
        env: dict[str, str] | None = None,
    ) -> Path:
        path = self.desired_path(job.slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        watch_log_dir(Path(env.get("PAL_HOME")) if env and env.get("PAL_HOME") else Path.home() / ".pal").mkdir(
            parents=True, exist_ok=True
        )
        stderr_path = str(watch_stderr_log_path(Path(env.get("PAL_HOME")) if env and env.get("PAL_HOME") else Path.home() / ".pal", job.slug))
        if self.manager == "launchd":
            payload = render_launchd_plist(
                job,
                python_executable=python_executable,
                pal_path=pal_path,
                workdir=workdir,
                env=env,
                stderr_path=stderr_path,
            )
        else:
            payload = render_systemd_unit(
                job,
                python_executable=python_executable,
                pal_path=pal_path,
                workdir=workdir,
                env=env,
                stderr_path=stderr_path,
            )
        path.write_text(payload, encoding="utf-8")
        return path

    def activate(self, job: JobSpec) -> tuple[bool, str | None]:
        path = self.desired_path(job.slug)
        if self.manager == "launchd":
            _run_command(["launchctl", "unload", str(path)])
            rc, out = _run_command(["launchctl", "load", str(path)])
            return rc == 0, out or None
        rc_reload, out_reload = _run_command(["systemctl", "--user", "daemon-reload"])
        if rc_reload != 0:
            return False, out_reload or None
        rc_enable, out_enable = _run_command(["systemctl", "--user", "enable", "--now", job.service_name])
        return rc_enable == 0, out_enable or None

    def deactivate_slug(self, slug: str) -> tuple[bool, str | None]:
        path = self.desired_path(slug)
        service_name = desired_service_name(self.manager, slug)
        if self.manager == "launchd":
            rc, out = _run_command(["launchctl", "unload", str(path)])
            return rc == 0 or not path.exists(), out or None
        _run_command(["systemctl", "--user", "disable", "--now", service_name])
        rc_reload, out_reload = _run_command(["systemctl", "--user", "daemon-reload"])
        return rc_reload == 0, out_reload or None

    def remove_service_path(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def sync(
        self,
        jobs: list[JobSpec],
        *,
        python_executable: str,
        pal_path: str,
        workdir: str,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        desired = {job.slug: job for job in jobs if job.enabled}
        written: list[str] = []
        activated: list[str] = []
        removed: list[str] = []
        errors: list[str] = []
        for job in desired.values():
            self.write_service(
                job,
                python_executable=python_executable,
                pal_path=pal_path,
                workdir=workdir,
                env=env,
            )
            written.append(job.service_name)
            ok, detail = self.activate(job)
            if ok:
                activated.append(job.service_name)
            elif detail:
                errors.append(f"{job.service_name}: {detail}")
        desired_paths = {self.desired_path(slug) for slug in desired.keys()}
        for path in self.existing_service_paths():
            if path in desired_paths:
                continue
            slug = path.stem.removeprefix(SERVICE_PREFIX_LAUNCHD)
            if self.manager == "systemd":
                slug = path.name.removeprefix(SERVICE_PREFIX_SYSTEMD).removesuffix(".service")
            self.deactivate_slug(slug)
            self.remove_service_path(path)
            removed.append(path.name)
        if self.manager == "systemd":
            rc_reload, out_reload = _run_command(["systemctl", "--user", "daemon-reload"])
            if rc_reload != 0 and out_reload:
                errors.append(out_reload)
        return {
            "written": written,
            "activated": activated,
            "removed": removed,
            "errors": errors,
        }

    def uninstall_all(self) -> dict[str, Any]:
        removed: list[str] = []
        errors: list[str] = []
        for path in self.existing_service_paths():
            slug = path.stem.removeprefix(SERVICE_PREFIX_LAUNCHD)
            if self.manager == "systemd":
                slug = path.name.removeprefix(SERVICE_PREFIX_SYSTEMD).removesuffix(".service")
            ok, detail = self.deactivate_slug(slug)
            if not ok and detail:
                errors.append(detail)
            self.remove_service_path(path)
            removed.append(path.name)
        if self.manager == "systemd":
            rc_reload, out_reload = _run_command(["systemctl", "--user", "daemon-reload"])
            if rc_reload != 0 and out_reload:
                errors.append(out_reload)
        return {"removed": removed, "errors": errors}
