"""Invalid musical timing must not silently become validated audio."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from workbench.validation import require_rhythm
from workbench.workflow import build, load_config

ROOT = Path(__file__).resolve().parents[1]


def example():
    return {"schemaVersion": 2,
            "measures": [{"index": i + 1, "number": i + 1, "start": i * 4, "end": (i + 1) * 4}
                         for i in range(3)],
            "notes": [{"id": "a", "start": 0, "end": 1}, {"id": "b", "start": 4, "end": 5},
                      {"id": "c", "start": 8, "end": 9}],
            "rhythmValidation": {"status": "needs-review", "blockingMeasureIndices": [2], "blockingMeasures": [2]}}


class RhythmGateTests(unittest.TestCase):
    def test_strict_rejects_full_invalid_timeline_and_draft_records_scope(self):
        with self.assertRaisesRegex(ValueError, "第 2 小节"):
            require_rhythm(example())
        result = require_rhythm(example(), "draft")
        self.assertEqual(result["status"], "draft")
        self.assertEqual(result["blockingMeasures"], [2])

    def test_clean_selection_passes_and_intervening_invalid_silence_does_not(self):
        self.assertEqual(require_rhythm(example(), note_ids=["a"])["status"], "rhythm-validated")
        with self.assertRaisesRegex(ValueError, "第 2 小节"):
            require_rhythm(example(), note_ids=["a", "c"])

    def test_missing_contract_bad_ids_and_unlocated_errors_cannot_pass(self):
        with self.assertRaisesRegex(ValueError, "契约"):
            require_rhythm({"schemaVersion": 2})
        for ids in ([], ["unknown"]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                require_rhythm(example(), note_ids=ids)
        model = example()
        model["rhythmValidation"]["blockingMeasureIndices"] = []
        with self.assertRaisesRegex(ValueError, "未定位"):
            require_rhythm(model)
        with self.assertRaises(ValueError):
            require_rhythm(example(), "anything")

    def test_workflow_blocks_invalid_rhythm_before_engraving_and_audio(self):
        source = '''<score-partwise version="4.0"><part-list><score-part id="Solo"><part-name>Solo</part-name></score-part></part-list>
        <part id="Solo"><measure number="1"><attributes><divisions>4</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
        <note><pitch><step>C</step><octave>4</octave></pitch><duration>20</duration><type>whole</type></note></measure></part></score-partwise>'''
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            path = directory / "bad.musicxml"
            path.write_text(source)
            font = directory / "fixture.sf2"
            font.write_bytes(b"Not rendered: structural gate must fail first")
            config_path = directory / "project.json"
            config_path.write_text(json.dumps({"schemaVersion": 1, "input": {"musicxml": "bad.musicxml"},
                "partMap": {"Solo": "S"}, "soundfont": str(font)}))
            config = load_config(config_path)
            self.assertEqual(config["rhythmPolicy"], "strict")
            output = directory / "failed-build"
            with patch("workbench.engrave.engrave") as engrave, patch("synth.render.midi_to_wav") as render:
                with self.assertRaisesRegex(ValueError, "节奏问题"):
                    build(config, output)
                engrave.assert_not_called()
                render.assert_not_called()
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["stages"][-1]["name"], "canonical-audit")
            self.assertTrue((output / "assets/rhythm-validation.json").exists())
            self.assertFalse((output / "assets/base.mid").exists())
            self.assertFalse((output / "assets/interactive.json").exists())
            self.assertEqual(path.read_text(), source)


if __name__ == "__main__":
    unittest.main()
