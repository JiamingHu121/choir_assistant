"""Run a separately installed Audiveris in headless batch mode.

Prefer the official launcher because it supplies matching Java/native libraries.
A jar installation can instead supply JAVA and AUDIVERIS_CLASSPATH overrides.
Nothing is downloaded or written into the installed plugin.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from typing import List, Optional

from runtime import find_binary

DEFAULT_APP = "/Applications/Audiveris.app"


def find_audiveris_launcher() -> Optional[str]:
    configured = os.environ.get("CHOIR_ASSISTANT_AUDIVERIS")
    if configured:
        return find_binary("audiveris", configured)
    for name in ("audiveris", "Audiveris"):
        found = shutil.which(name)
        if found:
            return str(Path(found).resolve())
    for path in (Path(DEFAULT_APP) / "Contents/MacOS/Audiveris",
                 Path("/opt/audiveris/bin/Audiveris"), Path("/opt/audiveris/bin/audiveris")):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def find_audiveris_jar(app_dir: str = DEFAULT_APP) -> Optional[str]:
    configured = os.environ.get("CHOIR_ASSISTANT_AUDIVERIS_JAR")
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"Configured Audiveris jar does not exist: {path}")
        return str(path)
    for path in (Path(app_dir) / "Contents/app/audiveris.jar",
                 Path(app_dir) / "Contents/Java/audiveris.jar",
                 Path(app_dir) / "Contents/Resources/audiveris.jar",
                 Path("/opt/audiveris/lib/app/audiveris.jar"),
                 Path("/usr/share/audiveris/audiveris.jar")):
        if path.is_file():
            return str(path)
    return None


def find_java(app_dir: str = DEFAULT_APP) -> str:
    if os.environ.get("CHOIR_ASSISTANT_JAVA"):
        return find_binary("java")
    for path in (Path(app_dir) / "Contents/runtime/Contents/Home/bin/java",
                 Path(app_dir) / "Contents/runtime/bin/java",
                 Path("/opt/audiveris/lib/runtime/bin/java")):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    if os.environ.get("JAVA_HOME"):
        candidate = Path(os.environ["JAVA_HOME"]) / "bin" / "java"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return find_binary("java")


def command_for(pdf: str | Path, outdir: str | Path, jar=None, java=None) -> tuple[list[str], dict]:
    env = dict(os.environ)
    if os.environ.get("CHOIR_ASSISTANT_TESSDATA"):
        tessdata = Path(os.environ["CHOIR_ASSISTANT_TESSDATA"]).expanduser().resolve()
        if not tessdata.is_dir():
            raise RuntimeError(f"Configured tessdata directory does not exist: {tessdata}")
        env["TESSDATA_PREFIX"] = str(tessdata)
    if jar or os.environ.get("CHOIR_ASSISTANT_AUDIVERIS_JAR"):
        jar = str(Path(jar or find_audiveris_jar()).expanduser().resolve())
        if not Path(jar).is_file():
            raise RuntimeError(f"Audiveris jar does not exist: {jar}")
        classpath = os.environ.get("CHOIR_ASSISTANT_AUDIVERIS_CLASSPATH", str(Path(jar).parent / "*"))
        java = find_binary("java", java) if java else find_java()
        cmd = [java, "-Djava.awt.headless=true", "--enable-native-access=ALL-UNNAMED", "-Xmx4g", "-cp", classpath, "Audiveris"]
    else:
        launcher = find_audiveris_launcher()
        if launcher:
            cmd = [launcher]
            env["JAVA_TOOL_OPTIONS"] = (env.get("JAVA_TOOL_OPTIONS", "") + " -Djava.awt.headless=true").strip()
        else:
            jar = find_audiveris_jar()
            if jar:
                return command_for(pdf, outdir, jar=jar, java=java)
            raise RuntimeError("Audiveris is unavailable. Install its official application or set CHOIR_ASSISTANT_AUDIVERIS; jar installs use CHOIR_ASSISTANT_AUDIVERIS_JAR/JAVA/CLASSPATH. See SETUP.md.")
    cmd.extend(["-batch", "-export", "-output", str(outdir), str(pdf)])
    return cmd, env


def run_omr(pdf_path: str, workdir: str, jar: Optional[str] = None,
            java: Optional[str] = None, timeout: int = 3600) -> List[str]:
    pdf = Path(pdf_path).expanduser().resolve()
    if not pdf.is_file():
        raise ValueError(f"PDF source does not exist: {pdf}")
    outdir = Path(workdir).expanduser().resolve() / "omr_out"
    outdir.mkdir(parents=True, exist_ok=True)
    cmd, env = command_for(pdf, outdir, jar=jar, java=java)
    print(f"[OMR] Audiveris batch recognition: {pdf}")
    log_path = outdir / "audiveris.log"
    try:
        with log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                                    text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Audiveris timed out after {timeout}s; log: {log_path}") from exc
    if result.returncode:
        with log_path.open("rb") as log:
            log.seek(max(0, log_path.stat().st_size - 2000))
            tail = log.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Audiveris failed (code={result.returncode}); log: {log_path}\n{tail}")
    produced = sorted({str(path) for pattern in ("*.mxl", "*.xml") for path in outdir.rglob(pattern)
                       if path.is_file() and not path.name.startswith("sheet#")})
    if not produced:
        raise RuntimeError(f"Audiveris completed without exporting MusicXML; inspect {log_path}")
    return produced
