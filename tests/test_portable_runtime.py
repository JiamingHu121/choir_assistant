"""Portable runtime checks must not depend on the original development tree."""
from pathlib import Path
from unittest import TestCase, mock
import json
import os
import tempfile
import unittest

import runtime
from omr import audiveris_runner
from scripts.bootstrap import copy_install_source


def executable(path, body="#!/bin/sh\nexit 0\n"):
    path.write_text(body)
    path.chmod(0o755)
    return path


class PortableRuntimeTests(TestCase):
    def test_configured_tools_support_spaces_and_invalid_override_does_not_fallback(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {}, clear=True):
            path = executable(Path(temporary) / "my fluid synth")
            with mock.patch.dict(os.environ, {"CHOIR_ASSISTANT_FLUIDSYNTH": str(path)}):
                self.assertEqual(runtime.find_binary("fluidsynth"), str(path.resolve()))
            with mock.patch.dict(os.environ, {"CHOIR_ASSISTANT_FLUIDSYNTH": str(path) + " --bad"}):
                with self.assertRaisesRegex(RuntimeError, "Configured"):
                    runtime.find_binary("fluidsynth", required=False)

    def test_data_and_cache_resolve_outside_code_without_creating_directories(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {}, clear=True):
            target = Path(temporary) / "user data"
            with mock.patch.dict(os.environ, {"CHOIR_ASSISTANT_DATA_DIR": str(target)}):
                self.assertEqual(runtime.data_dir(), target.resolve())
                self.assertEqual(runtime.cache_dir(), target.resolve() / "cache")
                self.assertFalse(target.exists())
            with mock.patch("runtime.sys.platform", "linux"), mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(target)}):
                self.assertEqual(runtime.data_dir(), target / "choir-assistant")

    def test_jar_overrides_launcher_and_preserves_external_native_environment(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {}, clear=True):
            folder = Path(temporary)
            java, jar, launcher = executable(folder / "java"), folder / "audiveris.jar", executable(folder / "launcher")
            jar.write_bytes(b"jar fixture")
            (folder / "tessdata").mkdir()
            overrides = {"CHOIR_ASSISTANT_AUDIVERIS_JAR": str(jar), "CHOIR_ASSISTANT_JAVA": str(java),
                         "CHOIR_ASSISTANT_AUDIVERIS": str(launcher),
                         "CHOIR_ASSISTANT_AUDIVERIS_CLASSPATH": "custom/*:native/*",
                         "CHOIR_ASSISTANT_TESSDATA": str(folder / "tessdata"),
                         "DYLD_LIBRARY_PATH": "external-native-directory"}
            with mock.patch.dict(os.environ, overrides):
                command, environment = audiveris_runner.command_for("score with spaces.pdf", folder / "out")
            self.assertEqual(command[0], str(java.resolve()))
            self.assertEqual(command[command.index("-cp") + 1], "custom/*:native/*")
            self.assertIn("-Djava.awt.headless=true", command)
            self.assertEqual(command[-1], "score with spaces.pdf")
            self.assertEqual(environment["TESSDATA_PREFIX"], str((folder / "tessdata").resolve()))
            self.assertEqual(environment["DYLD_LIBRARY_PATH"], "external-native-directory")

    def test_launcher_receives_batch_mode_without_shell_interpolation(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {}, clear=True):
            launcher = executable(Path(temporary) / "Audiveris")
            with mock.patch.dict(os.environ, {"CHOIR_ASSISTANT_AUDIVERIS": str(launcher), "JAVA_TOOL_OPTIONS": "-Xmx2g"}):
                command, environment = audiveris_runner.command_for("score.pdf", "output")
            self.assertEqual(command, [str(launcher.resolve()), "-batch", "-export", "-output", "output", "score.pdf"])
            self.assertIn("-Xmx2g", environment["JAVA_TOOL_OPTIONS"])
            self.assertIn("-Djava.awt.headless=true", environment["JAVA_TOOL_OPTIONS"])

    def test_run_omr_executes_external_launcher_and_keeps_writable_log(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {}, clear=True):
            folder = Path(temporary)
            source = folder / "score with spaces.pdf"
            source.write_bytes(b"PDF fixture")
            launcher = executable(folder / "external Audiveris", '''#!/bin/sh
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-output" ]; then shift; dest="$1"; fi
  shift
done
mkdir -p "$dest"
printf '<score-partwise/>' > "$dest/result.mxl"
printf 'batch log\n'
''')
            with mock.patch.dict(os.environ, {"CHOIR_ASSISTANT_AUDIVERIS": str(launcher)}):
                output = audiveris_runner.run_omr(str(source), str(folder / "project"))
            self.assertEqual(output, [str((folder / "project/omr_out/result.mxl").resolve())])
            self.assertIn("batch log", (folder / "project/omr_out/audiveris.log").read_text())
            self.assertEqual(source.read_bytes(), b"PDF fixture")

    def test_soundfont_invalid_explicit_path_is_never_silently_replaced(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.dict(os.environ, {"CHOIR_ASSISTANT_SOUNDFONT": str(Path(temporary) / "missing.sf2")}):
                with self.assertRaisesRegex(RuntimeError, "does not exist"):
                    runtime.find_soundfont(required=False)

    def test_install_copy_excludes_scores_caches_and_generated_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            target = Path(temporary) / "temporary-build"
            root.mkdir(); target.mkdir()
            for name in ("pyproject.toml", "requirements.txt", "README.md", "LICENSE", "runtime.py"):
                (root / name).write_text("fixture")
            for name in ("choir_assistant", "workbench", "omr", "synth"):
                package = root / name
                package.mkdir()
                (package / "module.py").write_text("fixture")
                (package / "generated").mkdir()
                (package / "generated/private.pdf").write_bytes(b"personal score")
                (package / "config.json").write_text("private config")
            (root / "workbench/index.html").write_text("workbench static asset")
            before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
            copy_install_source(root, target)
            self.assertTrue((target / "workbench/index.html").is_file())
            self.assertTrue((target / "runtime.py").is_file())
            self.assertFalse(list(target.rglob("*.pdf")))
            self.assertFalse(list(target.rglob("config.json")))
            self.assertEqual(before, sorted(str(p.relative_to(root)) for p in root.rglob("*")))

    def test_doctor_reports_broken_dependencies_without_aborting_remaining_checks(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("runtime.shutil.which", return_value=None), \
             mock.patch("runtime.Path.is_file", return_value=False), \
             mock.patch("importlib.import_module", side_effect=ImportError("unavailable")):
            report = runtime.dependency_report()
        self.assertFalse(report["ready"])
        self.assertEqual(len(report["checks"]), 10)
        self.assertEqual([c["name"] for c in report["checks"]][-1], "ffmpeg")
        self.assertFalse(report["checks"][-1]["required"])
        self.assertEqual(json.loads(json.dumps(report))["schemaVersion"], 1)


if __name__ == "__main__":
    unittest.main()
