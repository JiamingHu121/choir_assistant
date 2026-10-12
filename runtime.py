"""Portable external-tool discovery and writable user storage.

The installed plugin is immutable. Scores, caches, virtual environments, and
generated artifacts live in the user's data directory or an explicit project.
Environment overrides are paths to tools, never shell commands.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

ENV_PREFIX = "CHOIR_ASSISTANT_"
TOOL_ENV = {"fluidsynth": "FLUIDSYNTH", "ffmpeg": "FFMPEG", "pdftoppm": "PDFTOPPM",
            "pdfinfo": "PDFINFO", "java": "JAVA"}


def data_dir() -> Path:
    configured = os.environ.get(ENV_PREFIX + "DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "choir-assistant"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "choir-assistant"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "choir-assistant"


def cache_dir() -> Path:
    return data_dir() / "cache"


def find_binary(name: str, explicit=None, *, required: bool = True) -> str | None:
    """Resolve a path or a single PATH command; invalid overrides fail loudly."""
    configured = explicit or os.environ.get(ENV_PREFIX + TOOL_ENV.get(name, name.upper()))
    if configured:
        expanded = str(Path(configured).expanduser())
        candidate = shutil.which(expanded)
        if not candidate:
            raise RuntimeError(f"Configured {name} executable is unavailable: {configured}")
        return str(Path(candidate).resolve())
    found = shutil.which(name)
    if found:
        return str(Path(found).resolve())
    if required:
        variable = ENV_PREFIX + TOOL_ENV.get(name, name.upper())
        raise RuntimeError(f"{name} is unavailable. Install it or set {variable} to its executable path; see SETUP.md.")
    return None


def find_soundfont(explicit=None, *, required: bool = True) -> str | None:
    configured = explicit or os.environ.get(ENV_PREFIX + "SOUNDFONT")
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"Configured SoundFont does not exist: {path}")
        return str(path)
    # Only standard installed locations are examined. No recursive home scan.
    for path in (Path("/usr/share/sounds/sf2/FluidR3_GM.sf2"),
                 Path("/usr/share/sounds/sf2/TimGM6mb.sf2"),
                 Path("/usr/share/soundfonts/default.sf2"),
                 data_dir() / "soundfonts" / "default.sf2"):
        if path.is_file():
            return str(path.resolve())
    if required:
        raise RuntimeError("A General MIDI SoundFont is required. Set CHOIR_ASSISTANT_SOUNDFONT to a licensed .sf2/.sf3 file; see SETUP.md.")
    return None


def default_soundfont() -> str:
    return find_soundfont()


def dependency_report() -> dict:
    """Inspect runnable dependencies; the OMR engine itself is tested on import."""
    import importlib
    import importlib.metadata
    import platform
    import subprocess
    from omr.audiveris_runner import command_for

    checks = []

    def check(name, probe, required=True):
        try:
            detail = probe()
            checks.append({"name": name, "required": required, "available": True, "detail": detail})
        except Exception as exc:
            checks.append({"name": name, "required": required, "available": False, "detail": str(exc)})

    def python_version():
        if sys.version_info < (3, 12):
            raise RuntimeError("Python >=3.12 is required")
        return {"version": platform.python_version(), "executable": sys.executable}

    def python_dependency(module, distribution, expected):
        imported = importlib.import_module(module)
        version = importlib.metadata.version(distribution)
        if version != expected:
            raise RuntimeError(f"Expected {distribution}=={expected}, found {version}; rerun scripts/bootstrap.py")
        if module == "verovio":
            # This exercises the native extension rather than trusting metadata.
            imported.toolkit().getVersion()
        return {"version": version}

    def tool(name, flags):
        executable = find_binary(name)
        result = subprocess.run([executable, *flags], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RuntimeError(f"{name} failed to start: {(result.stderr or result.stdout)[-500:]}")
        return {"path": executable, "version": (result.stdout or result.stderr).splitlines()[0][:300]}

    def audiveris():
        cmd, _env = command_for("dependency-check.pdf", data_dir() / "dependency-check")
        if "-cp" in cmd:
            result = subprocess.run([cmd[0], "-version"], capture_output=True, text=True, timeout=15)
            if result.returncode:
                raise RuntimeError(f"Java failed to start: {(result.stderr or result.stdout)[-500:]}")
            return {"mode": "java-classpath", "java": cmd[0],
                    "version": (result.stderr or result.stdout).splitlines()[0],
                    "classpath": cmd[cmd.index("-cp") + 1], "engineCheck": "availability; full batch execution occurs during PDF import"}
        return {"mode": "official-launcher", "path": cmd[0],
                "engineCheck": "availability; full batch execution occurs during PDF import"}

    def soundfont():
        path = Path(find_soundfont())
        with path.open("rb") as stream:
            header = stream.read(12)
        if header[:4] != b"RIFF" or header[8:12] != b"sfbk":
            raise RuntimeError(f"SoundFont is not a RIFF sfbk file: {path}")
        return {"path": str(path), "bytes": path.stat().st_size,
                "license": "user-provided; check the font's license before redistribution"}

    check("python", python_version)
    for module, distribution, expected in (("music21", "music21", "10.5.0"),
                                            ("mido", "mido", "1.3.3"),
                                            ("verovio", "verovio", "6.3.0"),
                                            ("PIL", "Pillow", "12.3.0")):
        check(distribution, lambda m=module, d=distribution, v=expected: python_dependency(m, d, v))
    check("audiveris", audiveris)
    check("fluidsynth", lambda: tool("fluidsynth", ["--version"]))
    check("pdftoppm", lambda: tool("pdftoppm", ["-v"]))
    check("soundfont", soundfont)
    check("ffmpeg", lambda: tool("ffmpeg", ["-version"]), required=False)
    return {"schemaVersion": 1, "ready": all(c["available"] for c in checks if c["required"]),
            "platform": platform.system(), "machine": platform.machine(),
            "dataDirectory": str(data_dir()), "cacheDirectory": str(cache_dir()), "checks": checks}


def doctor_main(argv=None) -> int:
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Check Choir Assistant dependencies without installing software")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = dependency_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for check in report["checks"]:
            state = "OK" if check["available"] else ("MISSING" if check["required"] else "OPTIONAL")
            print(f"{state:8s} {check['name']}: {check['detail']}")
        print(f"Data: {report['dataDirectory']}\nReady for PDF import: {report['ready']}")
        if not report["ready"]:
            print("Install the missing system dependencies listed in SETUP.md, then rerun doctor.")
    return 0 if report["ready"] else 1
