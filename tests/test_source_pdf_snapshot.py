"""A bundle's original-score view must stay bound to its frozen PDF bytes.

The real model, engraver, MIDI writer and manifest builder run here. Audio
encoding and PDF image generation are isolated because these tests concern
source identity, not synthesis or the external renderer's visual accuracy.
"""
from pathlib import Path
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
import wave

import mido

from workbench.preview import BundlePreview
from workbench.workflow import build, file_hash, load_config, prepare_model, write_json


ROOT = Path(__file__).resolve().parents[1]
SCORE = ('<score-partwise version="4.0"><part-list><score-part id="P1">'
         '<part-name>Piano</part-name></score-part></part-list><part id="P1">'
         '<measure number="1"><attributes><divisions>1</divisions><time><beats>4</beats>'
         '<beat-type>4</beat-type></time><clef><sign>G</sign><line>2</line></clef>'
         '</attributes><note><pitch><step>C</step><octave>4</octave></pitch>'
         '<duration>4</duration><type>whole</type></note></measure></part></score-partwise>')


class SourcePdfSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.xml = self.root / "score.musicxml"
        self.xml.write_text(SCORE)
        self.pdf = self.root / "original.pdf"
        shutil.copy2(ROOT / "examples/practice-study.pdf", self.pdf)
        self.font = self.root / "fixture.sf2"
        self.font.write_bytes(b"not synthesized: source identity fixture")
        self.project = self.root / "project.json"

    def config(self, kind="musicxml"):
        raw = {"input": {kind: str(self.xml if kind == "musicxml" else self.pdf)},
               "soundfont": str(self.font)}
        if kind == "musicxml":
            raw["sourcePdf"] = str(self.pdf)
        write_json(self.project, raw)
        return load_config(self.project)

    @staticmethod
    def fake_audio(midi, soundfont, output):
        with wave.open(str(output), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\0\0" * round(mido.MidiFile(str(midi)).length * 8000))
        return str(output)

    def build_bundle(self, config):
        bundle = self.root / "bundle"
        with patch("synth.render.midi_to_wav", side_effect=self.fake_audio), patch(
                "synth.render.wav_to_mp3", side_effect=lambda source, output: Path(output).write_bytes(b"fixture")), patch(
                "workbench.playback.fit_audio_duration"):
            # fake_audio already writes exactly the MIDI duration. Source-PDF
            # identity tests must not invoke a host's external audio encoder.
            manifest = build(config, bundle)
        return bundle, manifest

    @staticmethod
    def rendered(pdf, output):
        return {"sourceSha256": file_hash(pdf), "pages": []}

    def test_musicxml_build_freezes_pdf_without_changing_model_revision_or_config(self):
        config = self.config()
        expected_revision = prepare_model(config, self.xml, omr="")["revision"]
        bundle, manifest = self.build_bundle(config)
        snapshot = bundle / "inputs/source.pdf"
        self.assertEqual(manifest["sourcePdf"], {"path": "inputs/source.pdf", "sha256": file_hash(self.pdf)})
        self.assertEqual(snapshot.read_bytes(), self.pdf.read_bytes())
        indexed = {item["path"]: item for item in manifest["artifacts"]}
        self.assertEqual(indexed["inputs/source.pdf"]["sha256"], manifest["sourcePdf"]["sha256"])
        self.assertEqual(manifest["config"], config)
        self.assertEqual(manifest["revision"], expected_revision)

        self.pdf.write_bytes(b"a different score replaced the same external path")
        with patch("workbench.calibration_evidence.render_pdf", side_effect=self.rendered) as render:
            preview = BundlePreview(bundle, self.project)
        self.assertEqual(Path(render.call_args.args[0]), snapshot)
        self.assertEqual(preview.score_projection()["sourcePdfBinding"],
                         {"status": "bundle-sha256-verified", "sha256": manifest["sourcePdf"]["sha256"]})

    def test_pdf_input_reuses_the_recognition_input_snapshot(self):
        def recognize(pdf, output):
            self.assertEqual(Path(pdf).read_bytes(), self.pdf.read_bytes())
            export = Path(output) / "omr_out/source.musicxml"
            export.parent.mkdir()
            export.write_text(SCORE)
            return [str(export)]
        with patch("omr.audiveris_runner.run_omr", side_effect=recognize):
            bundle, manifest = self.build_bundle(self.config("pdf"))
        self.assertEqual(manifest["sourcePdf"], {"path": "inputs/original.pdf", "sha256": file_hash(self.pdf)})
        self.assertEqual(len(list((bundle / "inputs").glob("*.pdf"))), 1)

    def test_separate_pdf_view_cannot_overwrite_a_same_named_recognition_snapshot(self):
        recognition_pdf = self.root / "source.pdf"
        shutil.copy2(self.pdf, recognition_pdf)
        self.pdf.write_bytes(self.pdf.read_bytes() + b"\n% distinct original-page view\n")
        write_json(self.project, {"input": {"pdf": str(recognition_pdf)}, "sourcePdf": str(self.pdf),
                                  "soundfont": str(self.font)})
        def recognize(pdf, output):
            self.assertEqual(Path(pdf).read_bytes(), recognition_pdf.read_bytes())
            export = Path(output) / "omr_out/source.musicxml"
            export.parent.mkdir()
            export.write_text(SCORE)
            return [str(export)]
        with patch("omr.audiveris_runner.run_omr", side_effect=recognize):
            bundle, manifest = self.build_bundle(load_config(self.project))
        self.assertEqual((bundle / "inputs/source.pdf").read_bytes(), recognition_pdf.read_bytes())
        self.assertEqual(manifest["sourcePdf"], {"path": "inputs/source-view.pdf", "sha256": file_hash(self.pdf)})

    def test_missing_changed_or_mismatched_pdf_snapshot_is_rejected(self):
        bundle, manifest = self.build_bundle(self.config())
        snapshot = bundle / manifest["sourcePdf"]["path"]
        original = snapshot.read_bytes()
        for change in ("missing", "changed", "metadata"):
            with self.subTest(change=change):
                snapshot.write_bytes(original)
                write_json(bundle / "manifest.json", manifest)
                if change == "missing":
                    snapshot.unlink()
                elif change == "changed":
                    snapshot.write_bytes(b"changed source PDF")
                else:
                    changed = json.loads(json.dumps(manifest))
                    changed["sourcePdf"]["sha256"] = "0" * 64
                    write_json(bundle / "manifest.json", changed)
                with self.assertRaises(ValueError), patch("workbench.calibration_evidence.render_pdf") as render:
                    BundlePreview(bundle, self.project)
                render.assert_not_called()

    def test_legacy_bundle_uses_external_pdf_with_binding_explicitly_unknown(self):
        bundle, manifest = self.build_bundle(self.config())
        del manifest["sourcePdf"]
        write_json(bundle / "manifest.json", manifest)
        with patch("workbench.calibration_evidence.render_pdf", side_effect=self.rendered) as render:
            preview = BundlePreview(bundle, self.project)
        self.assertEqual(Path(render.call_args.args[0]), self.pdf)
        self.assertEqual(preview.score_projection()["sourcePdfBinding"], {"status": "unknown"})


if __name__ == "__main__":
    unittest.main()
