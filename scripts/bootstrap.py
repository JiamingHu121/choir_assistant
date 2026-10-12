#!/usr/bin/env python3
"""Install the plugin into a writable venv without changing its source tree."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))
from runtime import data_dir

PIP_VERSION = "26.2.1"
SETUPTOOLS_VERSION = "84.0.0"


def copy_install_source(source: Path, target: Path):
    """Only packaging inputs enter the temporary build directory."""
    for name in ("pyproject.toml", "requirements.txt", "README.md", "LICENSE", "runtime.py"):
        shutil.copy2(source / name, target / name)
    for name in ("choir_assistant", "workbench", "omr", "synth"):
        shutil.copytree(source / name, target / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "generated", "assets", "*.md", "*.json", "*.command"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv", type=Path, default=data_dir() / "venv", help="Writable virtual environment, outside the plugin directory")
    parser.add_argument("--wheelhouse", type=Path, help="Install offline using predownloaded PyPI wheels")
    parser.add_argument("--skip-doctor", action="store_true", help="Install Python only; system dependency checks can be run later")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 12):
        parser.error("Python >=3.12 is required; use python3.12 (or later) to run this script")
    target = args.venv.expanduser().resolve()
    if target == PLUGIN_ROOT or PLUGIN_ROOT in target.parents:
        parser.error("--venv must be outside the plugin installation; use a writable user/workspace directory")
    if target.exists() and not (target / "pyvenv.cfg").is_file():
        parser.error("--venv exists but is not a virtual environment; choose another directory")
    if not target.exists():
        venv.EnvBuilder(with_pip=True).create(target)
    python = target / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    index_args = ["--no-index", "--find-links", str(args.wheelhouse.expanduser().resolve())] if args.wheelhouse else []
    subprocess.run([str(python), "-m", "pip", "install", *index_args,
                    f"pip=={PIP_VERSION}", f"setuptools=={SETUPTOOLS_VERSION}"], check=True)
    subprocess.run([str(python), "-m", "pip", "install", *index_args,
                    "-r", str(PLUGIN_ROOT / "requirements.txt")], check=True)
    # Some package builders write egg-info alongside their source. Build from
    # a temporary copy even when the installed Codex plugin is read-only.
    with tempfile.TemporaryDirectory(prefix="choir-assistant-install-") as temporary:
        source_copy = Path(temporary) / "source"
        source_copy.mkdir()
        copy_install_source(PLUGIN_ROOT, source_copy)
        subprocess.run([str(python), "-m", "pip", "install", *index_args,
                        "--no-deps", "--no-build-isolation", str(source_copy)], check=True)
    subprocess.run([str(python), "-m", "pip", "check"], check=True)
    print(f"\nInstalled choir-assistant in {target}", flush=True)
    print(f"Run: {python} -m choir_assistant --help", flush=True)
    if not args.skip_doctor:
        return subprocess.run([str(python), str(PLUGIN_ROOT / "scripts/doctor.py")]).returncode
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
