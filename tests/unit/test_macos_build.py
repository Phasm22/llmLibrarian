"""Exercise the bundle lifecycle with fake platform tools, never the real app."""
import os
import plistlib
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def build_env(tmp_path):
    repo = tmp_path / "checkout with spaces"
    macos = repo / "macos"
    macos.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[2] / "macos/build.sh", macos / "build.sh")
    for folder, name in [("Resources", "Info.plist"), ("Tools", "make-icon.swift")]:
        (macos / folder).mkdir()
        (macos / folder / name).write_text("fixture")
    (macos / "Resources/Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "0.2.0"}))
    binary_dir = tmp_path / "swift output"
    binary_dir.mkdir()
    binary = binary_dir / "llmLibrarian"
    binary.write_text("new app")
    binary.chmod(0o755)
    install = tmp_path / "Applications"
    old = install / "llmLibrarian.app"
    old.mkdir(parents=True)
    (old / "old-marker").write_text("keep until ready")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tool = """#!/bin/bash
set -eu
name="${0##*/}"
echo "$name $*" >> "$BUILD_CALLS"
case "$name" in
  swift)
    if [[ "$*" == *--show-bin-path* ]]; then
      echo "$BUILD_BIN"
    elif [[ "$1" == build ]]; then
      exit "${BUILD_FAIL:-0}"
    else
      mkdir -p "$2"
    fi ;;
  iconutil)
    [[ "$5" == *.icns ]] || exit 8
    echo icon > "$5" ;;
  codesign) exit "${SIGN_FAIL:-0}" ;;
  ditto)
    [[ "${STAGE_FAIL:-0}" == 0 ]] || exit 6
    cp -R "$1" "$2" ;;
  open) [[ -f "$2/Contents/MacOS/llmLibrarian" ]] ;;
esac
"""
    for name in ["swift", "iconutil", "codesign", "ditto", "pkill", "open"]:
        path = fake_bin / name
        path.write_text(tool)
        path.chmod(0o755)
    plutil = fake_bin / "plutil"
    plutil.write_text(f"#!{sys.executable}\n" + """import plistlib, sys
from pathlib import Path
if sys.argv[1] == '-extract':
    info = plistlib.loads(Path(sys.argv[-1]).read_bytes())
    print(info[sys.argv[2]])
    sys.exit(0)
_, option, key, kind, value, filename = sys.argv
assert option in ('-insert', '-replace') and kind == '-string'
path = Path(filename)
info = plistlib.loads(path.read_bytes())
info[key] = value
path.write_bytes(plistlib.dumps(info))
""")
    plutil.chmod(0o755)
    env = {**os.environ, "PATH": str(fake_bin) + ":/usr/bin:/bin",
           "LLMLIBRARIAN_APP_DIR": str(install), "BUILD_BIN": str(binary_dir),
           "BUILD_CALLS": str(tmp_path / "calls")}
    return macos, old, env


def run_build(build_env, *args, **overrides):
    macos, _, env = build_env
    return subprocess.run(["/bin/bash", str(macos / "build.sh"), *args],
                          env={**env, **overrides}, capture_output=True, text=True, timeout=10)


def test_install_relaunch_and_icon_cache(build_env):
    macos, installed, env = build_env
    numbers = []
    for _ in range(2):
        result = run_build(build_env, "--install", "--relaunch")
        assert result.returncode == 0, result.stderr
        numbers.append(int(plistlib.loads((installed / "Contents/Info.plist").read_bytes())["CFBundleVersion"]))
    assert numbers == [3, 4]
    assert not (installed / "old-marker").exists()
    assert (installed / "Contents/MacOS/llmLibrarian").read_text() == "new app"
    info = plistlib.loads((installed / "Contents/Info.plist").read_bytes())
    assert info["LibrarianBuildSource"] == str(macos.parent)
    assert info["LibrarianBuildState"] == "unknown"
    assert info["LibrarianBuildDate"].endswith("Z")
    calls = Path(env["BUILD_CALLS"]).read_text().splitlines()
    assert sum(line.startswith("iconutil ") for line in calls) == 1
    assert sum(line.startswith("open ") for line in calls) == 2
    assert any(line.startswith("pkill -a -x -f ") for line in calls), "UI-triggered rebuild must include the parent app"
    assert next(i for i, line in enumerate(calls) if line.startswith("ditto ")) < next(
        i for i, line in enumerate(calls) if line.startswith("pkill "))
    os.utime(macos / "Tools/make-icon.swift", (2_000_000_000, 2_000_000_000))
    assert run_build(build_env).returncode == 0
    assert Path(env["BUILD_CALLS"]).read_text().count("iconutil ") == 2


@pytest.mark.parametrize("failure", ["BUILD_FAIL", "SIGN_FAIL", "STAGE_FAIL"])
def test_failure_keeps_installed_app_running(build_env, failure):
    _, installed, env = build_env
    result = run_build(build_env, "--install", "--relaunch", **{failure: "6"})
    assert result.returncode != 0
    assert (installed / "old-marker").exists()
    calls = Path(env["BUILD_CALLS"]).read_text()
    assert "pkill " not in calls
    assert "open " not in calls


@pytest.mark.parametrize("args", [("--relaunch",), ("--unknown",)])
def test_invalid_options_do_not_build(build_env, args):
    result = run_build(build_env, *args)
    assert result.returncode == 2
    assert not Path(build_env[2]["BUILD_CALLS"]).exists()


def test_build_number_recovers_from_installed_bundle(build_env):
    macos, installed, _ = build_env
    assert run_build(build_env, "--install").returncode == 0
    info_path = installed / "Contents/Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info["CFBundleVersion"] = "42"
    info_path.write_bytes(plistlib.dumps(info))
    (macos / ".build/app-build-number").unlink()
    assert run_build(build_env, "--install").returncode == 0
    assert plistlib.loads(info_path.read_bytes())["CFBundleVersion"] == "43"
