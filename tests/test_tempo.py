"""Source tempo units, conservative estimates and tempo evidence identity."""
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from workbench.score_model import build_model, write_canonical
from workbench.tempo import load_evidence, meter_pulse
from workbench.workflow import load_config

ROOT = Path(__file__).resolve().parents[1]


def fixture(direction="", meter="4/4", second=""):
    count, unit = meter.split("/")
    length = int(count) * 4 / int(unit)
    xml = f'''<score-partwise version="4.0"><part-list><score-part id="Voice"><part-name>S</part-name></score-part></part-list>
    <part id="Voice"><measure number="1"><attributes><divisions>4</divisions><time><beats>{count}</beats><beat-type>{unit}</beat-type></time></attributes>
    {direction}<note><pitch><step>A</step><octave>4</octave></pitch><duration>{length * 4:g}</duration></note></measure>{second}</part></score-partwise>'''
    return xml


def split_staff_fixture(p1_body=None, p5_direction=""):
    names = ("Soprano", "Alto", "Tenor", "Bass", "Soprano", "Alto", "Tenor", "Bass", "Piano")
    definitions = "".join(f'<score-part id="P{i}"><part-name>{name}</part-name></score-part>' for i, name in enumerate(names, 1))
    parts = []
    for i in range(1, 10):
        attrs = '<attributes><divisions>4</divisions><time><beats>12</beats><beat-type>8</beat-type></time><clef><sign>G</sign><line>2</line></clef></attributes>'
        rest = '<note><rest/><duration>24</duration><type>whole</type><dot/></note>'
        note = '<note><pitch><step>A</step><octave>4</octave></pitch><duration>24</duration><type>whole</type><dot/></note>'
        body = p1_body if i == 1 and p1_body is not None else p5_direction + note if i == 5 else note if i == 9 else rest
        parts.append(f'<part id="P{i}"><measure number="1">{attrs}{body}</measure></part>')
    return f'<score-partwise version="4.0"><part-list>{definitions}</part-list>{"".join(parts)}</score-partwise>'


class TempoTests(unittest.TestCase):
    def model(self, xml, context=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.musicxml"
            path.write_text(xml)
            return build_model(source_path=path, tempo_context=context)

    def test_numeric_dotted_metronome_counts_written_unit(self):
        model = self.model(fixture('<direction><direction-type><metronome><beat-unit>quarter</beat-unit><beat-unit-dot/><per-minute>60</per-minute></metronome></direction-type></direction>', "12/8"))
        meta = model["tempoMetadata"]
        self.assertEqual(meta["initialQuarterBpm"], 90)
        self.assertEqual(meta["initialBeatBpm"], 60)
        self.assertEqual(meta["beatQuarterLength"], 1.5)
        self.assertEqual(meta["beatUnit"], "dotted-quarter")
        self.assertFalse(meta["inferred"])
        self.assertAlmostEqual(model["duration"], 4)

    def test_sound_tempo_is_quarter_notes_even_in_compound_meter(self):
        model = self.model(fixture('<direction><sound tempo="90"/></direction>', "6/8"))
        meta = model["tempoMetadata"]
        self.assertEqual(meta["initialQuarterBpm"], 90)
        self.assertEqual(meta["initialBeatBpm"], 60)
        self.assertEqual(meta["sourceKind"], "sound-tempo")
        self.assertFalse(meta["inferred"])
        self.assertAlmostEqual(model["duration"], 2)

    def test_standalone_sound_offset_and_changes_share_note_clock(self):
        xml = fixture('<sound tempo="60"/>', second='''<measure number="2"><sound tempo="120"><offset>4</offset></sound><note><pitch><step>A</step><octave>4</octave></pitch><duration>16</duration></note></measure>''')
        model = self.model(xml)
        self.assertEqual([m["onset"] for m in model["tempos"]], [0, 5])
        self.assertAlmostEqual(model["measures"][1]["start"], 4)
        self.assertAlmostEqual(model["duration"], 6.5)
        self.assertEqual(ET.fromstring(model["_xml"]).findtext('part/measure[@number="2"]/sound/offset'), "10080")

    def test_direction_sound_offset_overrides_display_offset(self):
        xml = fixture('<direction><offset>8</offset><sound tempo="60"><offset>4</offset></sound></direction>')
        model = self.model(xml)
        self.assertEqual(model["tempos"][1]["onset"], 1)
        self.assertAlmostEqual(model["duration"], 3.5)

    def test_numeric_changes_have_real_seconds_and_same_onset_conflicts(self):
        xml = fixture('<direction><sound tempo="60"/></direction><direction><sound tempo="90"/></direction>', second='''<measure number="2"><direction><direction-type><metronome><beat-unit>half</beat-unit><per-minute>60</per-minute></metronome></direction-type></direction><note><pitch><step>B</step><octave>4</octave></pitch><duration>16</duration></note></measure>''')
        model = self.model(xml)
        self.assertEqual(model["tempoMetadata"]["initialQuarterBpm"], 60)
        self.assertEqual(model["tempos"][1]["bpm"], 120)
        self.assertAlmostEqual(model["duration"], 6)
        self.assertIn("tempo-conflict", model["audit"]["summary"]["byKind"])

    def test_sound_priority_keeps_written_metronome_conflict_visible(self):
        xml = fixture('<direction><direction-type><metronome><beat-unit>quarter</beat-unit><per-minute>80</per-minute></metronome></direction-type><sound tempo="90"/></direction>')
        model = self.model(xml)
        self.assertEqual(model["tempoMetadata"]["initialQuarterBpm"], 90)
        self.assertEqual(model["tempoMetadata"]["changes"][0]["writtenMetronome"]["bpm"], 80)
        self.assertIn("metronome-sound-conflict", model["audit"]["summary"]["byKind"])

    def test_text_andante_compound_pulse_is_disclosed_estimate(self):
        xml = fixture('<direction><direction-type><words>Andante</words></direction-type></direction>', "12/8")
        model = self.model(xml)
        meta = model["tempoMetadata"]
        self.assertEqual(meta["initialQuarterBpm"], 108)
        self.assertEqual(meta["initialBeatBpm"], 72)
        self.assertEqual(meta["beatUnit"], "dotted-quarter")
        self.assertTrue(meta["inferred"])
        self.assertEqual(meta["sourceKind"], "tempo-text")
        self.assertAlmostEqual(model["duration"], 10 / 3)
        self.assertEqual(ET.fromstring(model["_xml"]).find('part/measure/direction/sound').get("tempo"), "108")
        self.assertIn("initial-tempo-inferred", model["audit"]["summary"]["byKind"])

    def test_other_text_terms_fallback_and_relative_changes_are_not_curves(self):
        adagio = self.model(fixture('<direction><direction-type><words>Adagio</words></direction-type></direction>'))
        self.assertEqual(adagio["tempoMetadata"]["initialBeatBpm"], 56)
        self.assertTrue(adagio["tempoMetadata"]["inferred"])
        unknown = self.model(fixture('<direction><direction-type><words>With grace</words></direction-type></direction>'))
        self.assertEqual(unknown["tempoMetadata"]["sourceKind"], "fallback")
        self.assertEqual(unknown["provenance"]["tempoFallback"], 120)
        relative = self.model(fixture('<direction><sound tempo="80"/></direction>', second='''<measure number="2"><direction><direction-type><words>poco rit.</words></direction-type></direction><note><pitch><step>A</step><octave>4</octave></pitch><duration>16</duration></note></measure>'''))
        self.assertEqual(len(relative["tempos"]), 1)
        self.assertEqual(relative["tempoMetadata"]["unresolvedChanges"], 1)
        self.assertEqual(relative["duration"], 6)
        self.assertIn("unresolved-tempo-change", relative["audit"]["summary"]["byKind"])

    def test_reviewed_pdf_text_is_bound_to_source_and_revision(self):
        xml = fixture(meter="12/8")
        context = {"schemaVersion": 1, "sourceSha256": sha256(xml.encode()).hexdigest(), "marks": [{"measure": 1, "text": "Andante", "evidence": {"page": 1}}]}
        unchanged = self.model(xml)
        reviewed = self.model(xml, context)
        self.assertEqual(reviewed["tempoMetadata"]["sourceKind"], "pdf-reviewed-text")
        self.assertEqual(reviewed["tempoMetadata"]["initialQuarterBpm"], 108)
        self.assertNotEqual(unchanged["version"], reviewed["version"])
        self.assertEqual(ET.fromstring(reviewed["_xml"]).findtext('part/measure/direction/direction-type/words'), "Andante")
        context["sourceSha256"] = "stale"
        with self.assertRaisesRegex(ValueError, "different MusicXML source"):
            self.model(xml, context)

    def test_compound_meter_pulse_units_are_consistent(self):
        self.assertEqual(meter_pulse({"text": "3/8"})["beatQuarterLength"], .5)
        self.assertEqual(meter_pulse({"text": "9/8"})["beatQuarterLength"], 1.5)
        self.assertEqual(meter_pulse({"text": "9/8"})["beatUnit"], "dotted-quarter")

    def test_inferred_tempo_export_has_matching_numeric_playback(self):
        from music21 import converter
        model = self.model(fixture('<direction><direction-type><words>Andante</words></direction-type></direction>', "12/8"))
        exported = converter.parseData(model["_xml"], format="musicxml")
        first_mark = exported.metronomeMarkBoundaries()[0][2]
        self.assertEqual(first_mark.getQuarterBPM(), 108)

    def test_numeric_priority_also_survives_generated_export(self):
        from music21 import converter
        for directions in (
            '<direction><sound tempo="90"/></direction><direction><direction-type><words>Andante</words></direction-type></direction>',
            '<direction><direction-type><words>Andante</words></direction-type></direction><direction><sound tempo="90"/></direction>',
        ):
            model = self.model(fixture(directions, "12/8"))
            self.assertEqual(model["tempoMetadata"]["initialQuarterBpm"], 90)
            exported = converter.parseData(model["_xml"], format="musicxml")
            self.assertTrue(all(mark.getQuarterBPM() == 90 for _, _, mark in exported.metronomeMarkBoundaries()))

    def test_canonical_export_preserves_every_source_part_and_reviewed_tempo(self):
        xml = split_staff_fixture()
        context = {"schemaVersion": 1, "sourceSha256": sha256(xml.encode()).hexdigest(),
                   "marks": [{"measure": 1, "text": "Andante", "evidence": {"page": 1}}]}
        model = self.model(xml, context)
        with tempfile.TemporaryDirectory() as directory:
            path = write_canonical(model, Path(directory) / "source-parts.musicxml")
            result = ET.parse(path).getroot()
            self.assertEqual([part.get('id') for part in result.findall('part')], [f'P{i}' for i in range(1, 10)])
            self.assertEqual(result.find('part[@id="P5"]/measure/note').get('id'), 'n-P5-m1-o1')
            direction = result.find('part[@id="P1"]/measure/direction')
            self.assertEqual(direction.findtext('direction-type/words'), 'Andante')
            self.assertEqual(direction.find('sound').get('tempo'), '108')
            with self.assertRaisesRegex(ValueError, 'merging is unsupported'):
                write_canonical(model, Path(directory) / 'merged.musicxml', merge_parts=True)
            self.assertFalse((Path(directory) / 'merged.musicxml').exists())

    def test_preserved_source_parts_keep_numeric_priority_and_cursor_offsets(self):
        word = '<direction><direction-type><words>Andante</words></direction-type></direction>'
        rest = '<note><rest/><duration>24</duration></note>'
        model = self.model(split_staff_fixture(word + rest, '<direction><sound tempo="90"/></direction>'))
        with tempfile.TemporaryDirectory() as directory:
            path = write_canonical(model, Path(directory) / 'numeric.musicxml')
            first = ET.parse(path).find('part[@id="P1"]/measure')
            self.assertEqual(first.findtext('direction/direction-type/words'), 'Andante')
            self.assertTrue(all(sound.get('tempo') == '90' for sound in first.findall('direction/sound')))
            self.assertEqual(model['tempoMetadata']['initialQuarterBpm'], 90)
            body = '<note><rest/><duration>8</duration></note><direction><offset>4</offset><sound tempo="60"><offset>2</offset></sound></direction><note><rest/><duration>16</duration></note>'
            model = self.model(split_staff_fixture(body))
            path = write_canonical(model, Path(directory) / 'offset.musicxml')
            direction = ET.parse(path).find('part[@id="P1"]/measure/direction')
            self.assertEqual(direction.findtext('offset'), '10080')
            self.assertEqual(direction.findtext('sound/offset'), '5040')
            self.assertEqual(model['tempos'][1]['onset'], 2.5)


if __name__ == "__main__":
    unittest.main()
