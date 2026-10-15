#!/usr/bin/env python3
"""Check the distributable layout and optionally write a source plugin ZIP."""
from pathlib import Path
import argparse
import ast
import hashlib
import json
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", "__pycache__", ".venv", "venv", "env", "build", "dist", "generated", "runs",
        ".deploy", "site-data", "site-runtime", "site-profiles"}


def files():
    return sorted(path for path in ROOT.rglob("*") if path.is_file()
                  and not any(part in SKIP or part.endswith(".egg-info") for part in path.relative_to(ROOT).parts)
                  and path.suffix not in {".pyc", ".log"} and path.name != ".DS_Store"
                  and not path.name.endswith(".site.local.json"))


def check():
    manifest = json.loads((ROOT / "plugin.json").read_text())
    legacy = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
    marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
    assert manifest["name"] == legacy["name"] == "choir-assistant"
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    version_assignments = [node.value for node in ast.parse((ROOT / "choir_assistant/__init__.py").read_text()).body
                           if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)]
    assert len(version_assignments) == 1, "Expected one source package version"
    source_version = ast.literal_eval(version_assignments[0])
    assert manifest["version"] == legacy["version"] == project["version"] == source_version, "Plugin and Python package versions differ"
    assert marketplace["plugins"][0]["source"] == {"source": "local", "path": "./"}
    for name in ("README.md", "LICENSE", "THIRD_PARTY.md", "SETUP.md", "pyproject.toml", "requirements.txt",
                 "skills/choir-workbench/SKILL.md", "scripts/bootstrap.py", "scripts/doctor.py",
                 "scripts/choir_assistant.py", "choir_assistant/cli.py", "workbench/index.html",
                 "workbench/source_verification.py", "docs/SOURCE_REVIEW.md",
                 "examples/practice-study.pdf", "examples/practice-study.musicxml"):
        assert (ROOT / name).is_file(), f"Missing package resource: {name}"
    for path in files():
        if path.suffix in {".py", ".js", ".html", ".css", ".md", ".toml", ".json", ".yml", ".yaml"}:
            content = path.read_text(encoding="utf-8")
            private_prefix = "/" + "Users/" + "jiaminghu/"
            assert private_prefix not in content, f"Private machine path in {path.relative_to(ROOT)}"
        if path.suffix.lower() in {".pdf", ".mxl", ".omr", ".wav", ".mp3", ".sf2", ".jar"}:
            assert path.relative_to(ROOT).as_posix() == "examples/practice-study.pdf", f"Unexpected data/binary: {path}"
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    manifest = check()
    entries = files()
    print(f"Validated {manifest['name']} {manifest['version']}: {len(entries)} distributable files")
    if args.output:
        destination = args.output.expanduser().resolve()
        if destination.is_relative_to(ROOT):
            parser.error("--output must be outside the plugin directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        prefix = f"choir-assistant-{manifest['version']}"
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in entries:
                info = zipfile.ZipInfo(f"{prefix}/{path.relative_to(ROOT).as_posix()}", (2026, 10, 13, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, path.read_bytes())
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        destination.with_suffix(destination.suffix + ".sha256").write_text(f"{digest}  {destination.name}\n")
        print(f"ZIP: {destination}\nSHA256: {digest}")


if __name__ == "__main__":
    main()
