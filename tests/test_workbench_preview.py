"""Cross-score isolation checks; no live server, OMR, or synthesis is started."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
from unittest import TestCase, mock

from workbench.calibration_service import CalibrationManager, artifact_url
from workbench.preview import BundlePreview
from workbench.workflow import file_hash, load_config, write_json


def project(root, title, part):
    root.mkdir(parents=True)
    (root / "score.musicxml").write_text("<score-partwise/>")
    (root / "font.sf2").write_bytes(b"test font")
    (root / "ledger.json").write_text("{}")
    path = root / "project.json"
    write_json(path, {"title": title, "input": {"musicxml": "score.musicxml"},
                     "corrections": "ledger.json", "soundfont": "font.sf2", "partMap": {"P1": part}, "rhythmPolicy": "draft"})
    config = load_config(path)
    bundle = root / "bundle"
    assets = bundle / "assets"
    assets.mkdir(parents=True)
    model = {"title": title, "revision": title + "-rev", "duration": 1, "parts": [{"sourcePart": "P1", "part": part}],
             "pages": [], "measures": [{"number": 1, "start": 0, "end": 1}], "notes": [], "audio": "/assets/base.mp3",
             "rhythmValidation": {"status": "validated", "blockingMeasures": []}, "playbackSettings": config["playback"]}
    write_json(assets / "interactive.json", model)
    (assets / "base.mp3").write_bytes(title.encode())
    write_json(bundle / "manifest.json", {"status": "complete", "revision": model["revision"], "config": config,
        "artifacts": [{"path": str(p.relative_to(bundle)), "sha256": file_hash(p)} for p in assets.iterdir()]})
    return bundle, path


class PreviewIsolationTests(TestCase):
    def test_two_scores_use_distinct_assets_ledgers_and_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a = BundlePreview(*project(root / "a", "First", "T"))
            b = BundlePreview(*project(root / "b", "Second", "B"))
            self.assertEqual(a.score_projection()["groups"], ["T"])
            self.assertEqual(b.score_projection()["title"], "Second")
            self.assertNotEqual(a.config["corrections"], b.config["corrections"])
            self.assertNotEqual(a.assets, b.assets)
            self.assertNotEqual(a.generated, b.generated)
            with self.assertRaisesRegex(ValueError, "outside"):
                artifact_url(a.generated / "evidence.png", b.generated)
            with self.assertRaisesRegex(ValueError, "inside"):
                a.publish({"bundle": str(b.bundle), "revision": b.read_model()["revision"]})
            self.assertEqual(a.read_model()["title"], "First")

    def test_changed_or_failed_bundles_cannot_be_served(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, path = project(Path(directory) / "project", "First", "T")
            (bundle / "assets/base.mp3").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "artifact changed"):
                BundlePreview(bundle, path)
            manifest = json.loads((bundle / "manifest.json").read_text())
            manifest["status"] = "failed"
            write_json(bundle / "manifest.json", manifest)
            with self.assertRaisesRegex(ValueError, "completed"):
                BundlePreview(bundle, path)

    def test_preview_static_paths_never_fall_back_to_current_score(self):
        from workbench import server
        with tempfile.TemporaryDirectory() as directory:
            preview = BundlePreview(*project(Path(directory) / "project", "Second", "B"))
            with mock.patch.object(server, "PREVIEW", preview), mock.patch.object(server, "GENERATED", preview.generated):
                self.assertEqual(server.resolve_static("/assets/base.mp3"), preview.assets / "base.mp3")
                self.assertEqual(server.resolve_static("/generated/test.wav"), preview.generated / "test.wav")
                self.assertIsNone(server.resolve_static("/assets/../../corrections.json"))
                self.assertIsNone(server.resolve_static("/generated/../../../../assets/base.mp3"))
                self.assertEqual(server.resolve_static("/app.js"), server.WEB / "app.js")

    def test_another_score_cannot_replace_preview_even_inside_its_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a = BundlePreview(*project(root / "a", "First", "T"))
            other_bundle, _ = project(a.generated / "other", "Second", "B")
            with self.assertRaisesRegex(ValueError, "config differs"):
                a.publish({"bundle": str(other_bundle), "revision": "Second-rev"})
            self.assertEqual(a.read_model()["revision"], "First-rev")
            self.assertFalse(a.pointer.exists())

    def test_scan_callback_and_latest_pointer_are_project_local(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            generated = root / "preview"
            generated.mkdir()
            config = {"title": "Other score"}
            mutation = threading.Lock()
            manager = CalibrationManager(lambda: {"revision": "other-rev"}, mutation,
                                         read_config=lambda: deepcopy(config), generated=generated)
            finished = threading.Event()
            def fake_scan(received, output, **kwargs):
                self.assertEqual(received, config)
                self.assertEqual(kwargs["expected_revision"], "other-rev")
                self.assertEqual(Path(output).parent, generated / "calibration")
                finished.set()
                return {"output": str(output)}
            with mock.patch("workbench.calibration_service.scan", side_effect=fake_scan):
                manager.start({"action": "scan", "revision": "other-rev"})
                self.assertTrue(finished.wait(2))
                self.assertTrue(mutation.acquire(timeout=2))
                mutation.release()
            self.assertTrue((generated / "calibration/latest.json").is_file())
            write_json(generated / "calibration/latest.json", {"run": str(root / "foreign")})
            manager.run = None
            self.assertEqual(manager.snapshot()["status"], "idle")
