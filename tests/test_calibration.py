"""Calibration transfer, ambiguity and original-source safeguards."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

from workbench.calibration import plan_calibration, read_omr_features
from workbench.score_model import build_model


def note(duration=2, type_name="quarter", step="C", extra="", voice="1", staff=1):
    return (f"<note><pitch><step>{step}</step><octave>4</octave></pitch><duration>{duration}</duration>"
            f"<voice>{voice}</voice><type>{type_name}</type><staff>{staff}</staff>{extra}</note>")


def bar(body, number=1, meter="1/4", implicit=False, extra_attrs=""):
    beats, beat_type = meter.split("/")
    return (f'<measure number="{number}"' + (' implicit="yes"' if implicit else '') + '>'
            '<attributes><divisions>2</divisions><time><beats>' + beats + '</beats><beat-type>'
            + beat_type + '</beat-type></time>' + extra_attrs + '</attributes>' + body + '</measure>')


class CalibrationTests(unittest.TestCase):
    def model(self, bars, corrections=None, part_id="P1"):
        xml = (f'<score-partwise><part-list><score-part id="{part_id}"><part-name>Independent fixture</part-name>'
               f'</score-part></part-list><part id="{part_id}">' + bars + '</part></score-partwise>')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.xml"
            path.write_text(xml)
            return build_model(corrections, source_path=path)

    def rule(self, model, rule, **kwargs):
        return [candidate for candidate in plan_calibration(model, **kwargs)["candidates"] if candidate["ruleId"] == rule]

    def test_same_beam_error_transfers_to_relocated_bar_and_new_part(self):
        wrong = note(extra='<beam number="1">begin</beam>') + note(step="D", extra='<beam number="1">end</beam>')
        for preceding, part_id in ((0, "P1"), (8, "Voice77")):
            healthy = ''.join(bar(note(), number=index + 1) for index in range(preceding))
            model = self.model(healthy + bar(wrong, number=preceding + 1), part_id=part_id)
            proposals = self.rule(model, "beam-type-duration")
            self.assertEqual(len(proposals), 2)
            self.assertEqual({candidate["measureIndices"][0] for candidate in proposals}, {preceding + 1})
            self.assertEqual({candidate["sourcePart"] for candidate in proposals}, {part_id})
            self.assertTrue(all(candidate["edits"][0]["before"] == 1 and candidate["edits"][0]["after"] == .5 for candidate in proposals))
            self.assertTrue(all(candidate["status"] == "proposed" for candidate in proposals))
            solutions = [group["hypotheses"]["solutions"] for group in plan_calibration(model)["groups"] if group["measureIndex"] == preceding + 1]
            self.assertEqual(len(solutions[0]), 1)
            self.assertEqual(len(solutions[0][0]["edits"]), 2)

    def test_internal_consistency_does_not_hide_omr_flag_or_glyph_conflicts(self):
        model = self.model(bar(note(duration=4, type_name="half"), meter="2/4"))
        symbol = model["notes"][0]
        symbol["source"]["omr"] = {"shape": "NOTEHEAD_BLACK", "method": "checked-part-voice-chord-order"}
        proposals = self.rule(model, "omr-glyph-duration")
        self.assertEqual({candidate["edits"][0]["after"] for candidate in proposals}, {1, .5})
        self.assertEqual(plan_calibration(model)["summary"]["autoEligible"], 0)
        features = {symbol["id"]: {"sourceSha256": model["provenance"]["sourceSha256"],
                                  "shape": "NOTEHEAD_BLACK", "ordinaryFlagLevel": 1, "dotCount": 0}}
        flagged = self.rule(model, "omr-stem-duration", omr_features=features)
        self.assertEqual(flagged[0]["edits"][0]["after"], .5)

    def test_omr_relation_reader_preserves_join_and_reads_flags_dots(self):
        model = self.model(bar(note()))
        symbol = model["notes"][0]
        symbol["source"]["omr"] = {"method": "checked-part-voice-chord-order", "page": 1, "system": 7, "headId": "h1"}
        xml = ('<sheet><system id="7"><sig><inters><head id="h1" shape="NOTEHEAD_BLACK"/>'
               '<stem id="s1"/><flag id="f1" shape="FLAG_2_DOWN"/><augmentation-dot id="d1"/>'
               '</inters><relations><relation source="h1" target="s1"><head-stem/></relation>'
               '<relation source="f1" target="s1"><flag-stem/></relation>'
               '<relation source="d1" target="h1"><augmentation/></relation></relations></sig></system></sheet>')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.omr"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("sheet#1/sheet#1.xml", xml)
            features = read_omr_features(model, path)
        record = features[symbol["id"]]
        self.assertEqual(record["ordinaryFlagLevel"], 2)
        self.assertEqual(record["dotCount"], 1)
        self.assertFalse(record["isVerifiedImage"])
        proposal = self.rule(model, "omr-stem-duration", omr_features=features)[0]
        self.assertEqual(proposal["edits"][0]["after"], .375)

    def test_feathered_beams_and_ambiguous_stems_cannot_determine_duration(self):
        model = self.model(bar(note(extra='<beam number="1" fan="accel">begin</beam>')))
        self.assertEqual(self.rule(model, "beam-type-duration"), [])
        symbol = model["notes"][0]
        features = {symbol["id"]: {"sourceSha256": model["provenance"]["sourceSha256"], "shape": "NOTEHEAD_BLACK",
                                  "ordinaryFlagLevel": 1, "ambiguousStem": True}}
        self.assertEqual(self.rule(model, "omr-stem-duration", omr_features=features), [])

    def test_false_tuplet_hypothesis_is_coupled_and_never_auto_filled(self):
        mod = '<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification>'
        # Each XML duration agrees with its wrong ratio; all three are short.
        body = ''.join(note(duration=1, type_name="eighth", step=step, extra=mod) for step in ("C", "D", "E"))
        model = self.model(bar(body, meter="3/8").replace('<divisions>2</divisions>', '<divisions>3</divisions>'))
        proposals = self.rule(model, "tuplet-ratio-removal")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(len(proposals[0]["edits"]), 3)
        self.assertEqual({edit["after"] for edit in proposals[0]["edits"]}, {.5})
        group = plan_calibration(model)["groups"][0]
        self.assertEqual(len(group["hypotheses"]["solutions"]), 1)
        self.assertEqual(len(group["hypotheses"]["solutions"][0]["edits"]), 3)
        self.assertFalse(group["hypotheses"]["uniquenessProvesSourceAccuracy"])
        self.assertEqual(plan_calibration(model)["summary"]["autoEligible"], 0)

    def test_valid_triplet_is_preserved_without_false_ratio_proposal(self):
        mod = '<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification>'
        body = ''.join(note(duration=1, type_name="eighth", step=step, extra=mod) for step in ("C", "D", "E"))
        model = self.model(bar(body, meter="1/4").replace('<divisions>2</divisions>', '<divisions>3</divisions>'))
        self.assertEqual(model["rhythmValidation"]["status"], "validated")
        self.assertEqual(self.rule(model, "tuplet-ratio-removal"), [])
        self.assertEqual(plan_calibration(model)["summary"]["editableCandidates"], 0)

    def test_forward_pickup_sparse_and_independent_meter_prevent_force_fit(self):
        beam = '<beam number="1">begin</beam>'
        cases = [
            (bar(note(extra=beam), implicit=True), "implicit-or-pickup-measure"),
            (bar(note(extra=beam) + '<forward><duration>2</duration><voice>1</voice><staff>1</staff></forward>', meter="2/4"), "explicit-forward-preserved"),
            (bar(note(duration=4, type_name="half") + '<backup><duration>2</duration></backup>' + note(voice="2", extra=beam), meter="2/4"), "sparse-gap-or-overlapping-voice"),
            (bar(note(staff=1, extra=beam) + note(staff=2, extra=beam), meter="2/4",
                 extra_attrs='<staves>2</staves><time number="2"><beats>3</beats><beat-type>4</beat-type></time>'), "independent-staff-meters"),
        ]
        for body, expected in cases:
            with self.subTest(expected=expected):
                model = self.model(body)
                reasons = {group["hypotheses"].get("reason") for group in plan_calibration(model)["groups"]}
                self.assertIn(expected, reasons)
                self.assertEqual(plan_calibration(model)["summary"]["autoEligible"], 0)

    def test_chord_members_are_simultaneous_not_added_as_meter_length(self):
        beam = '<beam number="1">begin</beam>'
        body = note(extra=beam) + note(step="E", extra='<chord/>' + beam) + note(step="D", extra=beam)
        model = self.model(bar(body))
        solutions = plan_calibration(model)["groups"][0]["hypotheses"]["solutions"]
        self.assertEqual(len(solutions), 1)
        self.assertEqual(len(solutions[0]["edits"]), 3)
        self.assertEqual(solutions[0]["quarterDuration"], {"numerator": 1, "denominator": 1})

    def test_meter_only_underfill_gives_review_and_never_guessed_rest_or_pitch(self):
        model = self.model(bar(note(), meter="4/4"))
        plan = plan_calibration(model)
        self.assertGreater(plan["summary"]["candidateCount"], 0)
        self.assertEqual(plan["summary"]["editableCandidates"], 0)
        self.assertFalse(plan["groups"][0]["hypotheses"]["solutions"])
        self.assertTrue(all(candidate["edits"] == [] for candidate in plan["candidates"]))

    def test_source_evidence_requires_hash_status_reference_and_stable_id(self):
        model = self.model(bar(note(extra='<beam number="1">begin</beam>')))
        symbol = model["notes"][0]
        record = {"sourceSha256": model["provenance"]["sourceSha256"], "status": "source-verified",
                  "reference": "fixture-original.png#symbol1", "duration": .5}
        proposal = self.rule(model, "source-verified", evidence={symbol["id"]: record})[0]
        self.assertEqual(proposal["status"], "auto-eligible")
        self.assertEqual(proposal["edits"][0]["objectId"], symbol["id"])
        for change in ({"sourceSha256": "other"}, {"status": "confidence-high"}, {"reference": ""}, {"duration": 0}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                plan_calibration(model, evidence={symbol["id"]: record | change})
        with self.assertRaises(ValueError):
            plan_calibration(model, evidence={"unknown": record})

    def test_reviewed_pattern_proposes_relocated_rhythm_without_overriding_ledger(self):
        beam_begin = '<beam number="1">begin</beam>'
        beam_end = '<beam number="1">end</beam>'
        body = note(step="C", extra=beam_begin) + note(step="D", extra=beam_end)
        model = self.model(bar(body, number=1) + bar(body, number=2), corrections={
            "n-P1-m1-o1": {"duration": .5, "reason": "Original source review"},
            "n-P1-m1-o2": {"duration": .5, "reason": "Original source review"}})
        proposals = self.rule(model, "verified-pattern")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["measureIndices"], [2])
        self.assertEqual(len(proposals[0]["edits"]), 2)
        self.assertEqual(proposals[0]["status"], "proposed")
        self.assertTrue(all(candidate["measureIndices"] == [2] for candidate in self.rule(model, "beam-type-duration")))

    def test_bounded_search_reports_truncation_instead_of_false_uniqueness(self):
        body = ''.join(note(extra='<beam number="1">continue</beam>') for _ in range(10))
        model = self.model(bar(body, meter="5/4"))
        result = plan_calibration(model, max_hypotheses=1)["groups"][0]["hypotheses"]
        self.assertTrue(result["truncated"])
        self.assertEqual(result["combinationCount"], 1024)
        self.assertEqual(result["searched"], 256)
        self.assertFalse(result["uniquenessProvesSourceAccuracy"])

    def test_plan_is_pure_reproducible_and_json_serializable(self):
        model = self.model(bar(note(extra='<beam number="1">begin</beam>')))
        before = deepcopy(model)
        first, second = plan_calibration(model), plan_calibration(model)
        self.assertEqual(first, second)
        self.assertEqual(model, before)
        json.dumps(first)
        self.assertEqual(first["summary"]["scannedMeasures"], 1)
        self.assertEqual(first["summary"]["scannedSymbols"], 1)

    def test_reviewed_duplet_preserves_undotted_eighth_and_printed_two(self):
        mod = '<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification>'
        body = note(duration=1, type_name="eighth", extra=mod) + note(duration=1, type_name="eighth", step="D", extra=mod)
        source = bar(body, meter="3/8").replace('<divisions>2</divisions>', '<divisions>3</divisions>')
        desired = {"type": "eighth", "dots": 0, "timeModification": {"actualNotes": 2, "normalNotes": 3}}
        corrections = {f"n-P1-m1-o{ordinal}": {"duration": .75, "notation": desired,
                         "tuplets": [{"type": endpoint, "number": "1", "showNumber": "actual", "actualNumber": 2}],
                         "calibration": {"ruleId": "source-verified", "proposalId": "fixture-duplet"},
                         "reason": "Original source printed 2 over undotted eighths"}
                       for ordinal, endpoint in ((1, "start"), (2, "stop"))}
        model = self.model(source, corrections)
        self.assertEqual(model["rhythmValidation"]["status"], "validated")
        for symbol in model["notes"]:
            self.assertEqual(symbol["duration"], .75)
            self.assertEqual(symbol["notation"]["type"], "eighth")
            self.assertEqual(symbol["notation"]["dots"], 0)
            self.assertEqual(symbol["notation"]["timeModification"]["multiplier"], {"numerator": 3, "denominator": 2})
            self.assertEqual(symbol["notation"]["tuplets"][0]["actualNumber"], 2)
            self.assertEqual(symbol["source"]["notation"]["timeModification"]["actualNotes"], 3)
            self.assertEqual(symbol["source"]["calibration"]["proposalId"], "fixture-duplet")
        xml = ET.fromstring(model["_xml"])
        self.assertEqual(len(xml.findall('.//note/dot')), 0)
        self.assertEqual(xml.findtext('.//tuplet/tuplet-actual/tuplet-number'), "2")
        evidence = {symbol["id"]: {"sourceSha256": model["provenance"]["sourceSha256"],
                    "status": "source-verified", "reference": "original.png", "notation": desired}
                    for symbol in model["notes"]}
        self.assertEqual(self.rule(model, "source-verified", evidence=evidence), [])

    def test_printed_compound_three_remains_three_quarters_without_fake_dots(self):
        mod = '<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification>'
        body = ''.join(note(duration=2, type_name="quarter", step=step, extra=mod) for step in ("C", "D", "E"))
        source = bar(body, meter="3/4").replace('<divisions>2</divisions>', '<divisions>3</divisions>')
        desired = {"type": "quarter", "dots": 0, "timeModification": {"actualNotes": 3, "normalNotes": 3}}
        corrections = {f"n-P1-m1-o{ordinal}": {"duration": 1, "notation": desired,
                        "tuplets": ([{"type": "start" if ordinal == 1 else "stop", "number": "1", "actualNumber": 3}]
                                    if ordinal in (1, 3) else []), "reason": "Printed compound-meter three"}
                       for ordinal in (1, 2, 3)}
        model = self.model(source, corrections)
        self.assertEqual(model["rhythmValidation"]["status"], "validated")
        self.assertEqual([symbol["duration"] for symbol in model["notes"]], [1, 1, 1])
        self.assertEqual([symbol["notation"]["dots"] for symbol in model["notes"]], [0, 0, 0])
        self.assertEqual(ET.fromstring(model["_xml"]).findtext('.//tuplet/tuplet-actual/tuplet-number'), "3")

    def test_reviewed_notation_cannot_conflict_with_duration_or_invalid_marks(self):
        source = bar(note())
        for notation in ({"type": "eighth", "dots": 0, "timeModification": None},
                         {"type": "quarter", "dots": 0, "timeModification": {"actualNotes": 0, "normalNotes": 2}},
                         {"type": "quarter", "dots": 0, "timeModification": None, "unsupported": 1}):
            with self.subTest(notation=notation), self.assertRaises(ValueError):
                self.model(source, {"n-P1-m1-o1": {"notation": notation, "reason": "invalid evidence fixture"}})
        for tuplets in ([{"type": "continue", "number": "1"}], [{"type": "start", "actualNumber": True}],
                        [{"type": "start", "number": "1", "showNumber": "guess"}]):
            with self.subTest(tuplets=tuplets), self.assertRaises(ValueError):
                self.model(source, {"n-P1-m1-o1": {"tuplets": tuplets, "reason": "invalid evidence fixture"}})

    def test_compound_duplet_generalizes_without_any_target_score_ids(self):
        body = (note(duration=6, type_name="half", extra='<dot/>')
                + note(duration=1, type_name="eighth", step="D", extra='<beam number="1">begin</beam>')
                + note(duration=1, type_name="eighth", step="E", extra='<beam number="1">end</beam>')
                + note(duration=3, type_name="quarter", step="F", extra='<dot/>'))
        for preceding, part in ((0, "P1"), (3, "NewScoreVoice")):
            healthy = ''.join(bar(note(duration=12, type_name="whole", extra='<dot/>'), number=index + 1, meter="12/8")
                              for index in range(preceding))
            source = healthy + bar(body, number=preceding + 1, meter="12/8")
            model = self.model(source, part_id=part)
            proposal = self.rule(model, "compound-duplet")[0]
            self.assertEqual(proposal["measureIndices"], [preceding + 1])
            self.assertEqual(proposal["sourcePart"], part)
            self.assertEqual(proposal["status"], "proposed")
            self.assertEqual(len(proposal["edits"]), 6)
            corrections = {}
            for edit in proposal["edits"]:
                corrections.setdefault(edit["objectId"], {"reason": "Fixture original confirms printed 2"})[edit["field"]] = edit["after"]
            corrected = self.model(source, corrections=corrections, part_id=part)
            self.assertEqual(corrected["rhythmValidation"]["status"], "validated")
            pair = [symbol for symbol in corrected["notes"] if symbol["id"] in proposal["objectIds"]]
            self.assertEqual([symbol["duration"] for symbol in pair], [.75, .75])
            self.assertEqual([symbol["notation"]["dots"] for symbol in pair], [0, 0])
            solutions = next(group for group in plan_calibration(model)["groups"] if group["measureIndex"] == preceding + 1)["hypotheses"]["solutions"]
            self.assertEqual(len(solutions), 1)
            self.assertEqual(len(solutions[0]["edits"]), 6)

    def test_compound_three_retains_distinct_remove_and_preserve_number_hypotheses(self):
        ratio = '<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification>'
        body = note(duration=9, type_name="half", extra='<dot/>') + ''.join(
            note(duration=2, type_name="quarter", step=step, extra=ratio +
                 ('<notations><tuplet type="start"/></notations>' if ordinal == 0 else
                  '<notations><tuplet type="stop"/></notations>' if ordinal == 2 else ''))
            for ordinal, step in enumerate(("D", "E", "F")))
        source = bar(body, meter="12/8").replace('<divisions>2</divisions>', '<divisions>3</divisions>')
        model = self.model(source)
        preserved = self.rule(model, "compound-quarter-three")
        removed = self.rule(model, "tuplet-ratio-removal")
        self.assertEqual(len(preserved), 1)
        self.assertEqual(len(removed), 1)
        self.assertEqual(len(preserved[0]["edits"]), 9)
        hypotheses = plan_calibration(model)["groups"][0]["hypotheses"]["solutions"]
        self.assertEqual(len(hypotheses), 2)
        self.assertTrue(all(item["status"] == "meter-compatible-hypothesis" for item in hypotheses))
        self.assertEqual(plan_calibration(model)["summary"]["autoEligible"], 0)

    def test_verovio_spacing_uses_reviewed_normal_count_not_importer_default(self):
        import verovio
        old = '<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification>'
        duplet = bar(''.join(note(duration=1, type_name="eighth", step=step, extra=old) for step in ("C", "D")),
                      number=1, meter="3/8")
        triple = bar(''.join(note(duration=2, type_name="quarter", step=step, extra=old) for step in ("E", "F", "G")),
                      number=2, meter="3/4")
        source = (duplet + triple).replace('<divisions>2</divisions>', '<divisions>3</divisions>')
        changes = {}
        for measure, count, duration, type_name, actual in ((1, 2, .75, "eighth", 2), (2, 3, 1, "quarter", 3)):
            for ordinal in range(1, count + 1):
                changes[f"n-P1-m{measure}-o{ordinal}"] = {
                    "duration": duration,
                    "notation": {"type": type_name, "dots": 0, "timeModification": {"actualNotes": actual, "normalNotes": 3}},
                    # Normal number is intentionally omitted from the reviewed ledger.
                    "tuplets": ([{"type": "start" if ordinal == 1 else "stop", "number": "1", "actualNumber": actual}]
                                if ordinal in (1, count) else []), "reason": "Source-verified fixture printed counts"}
        model = self.model(source, changes)
        toolkit = verovio.toolkit()
        toolkit.setOptions({"inputFrom": "musicxml"})
        self.assertTrue(toolkit.loadData(model["_xml"]))
        expected = {"n-P1-m1-o1": 0, "n-P1-m1-o2": .75, "n-P1-m2-o1": 1.5,
                    "n-P1-m2-o2": 2.5, "n-P1-m2-o3": 3.5}
        actual = {object_id: item["qstamp"] for item in toolkit.renderToTimemap()
                  for object_id in item.get("on", []) if object_id in expected}
        self.assertEqual(actual, expected)
        self.assertEqual(toolkit.getTimeForElement("n-P1-m1-o2"), 375)
        self.assertEqual(toolkit.getTimeForElement("n-P1-m2-o3"), 1750)
        mei = ET.fromstring(toolkit.getMEI())
        tuplets = [node for node in mei.iter() if node.tag.endswith('}tuplet')]
        self.assertEqual([(node.get("num"), node.get("numbase")) for node in tuplets], [("2", "3"), ("3", "3")])
        self.assertEqual(model["notes"][0]["notation"]["tuplets"][0]["normalNumber"], 3)
        self.assertEqual(model["audit"]["summary"]["correctionCategories"]["tuplet-ratio-normalization"], 2)

    def test_reviewed_graphic_count_conflict_rejects_unsupported_nested_ratio(self):
        desired = {"type": "eighth", "dots": 0, "timeModification": {"actualNotes": 2, "normalNotes": 3}}
        for endpoint in ({"type": "start", "actualNumber": 3},
                         {"type": "start", "actualNumber": 2, "normalNumber": 2}):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                self.model(bar(note(), meter="1/4"), {"n-P1-m1-o1": {
                    "duration": .75, "notation": desired, "tuplets": [endpoint], "reason": "Conflicting reviewed counts"}})


if __name__ == "__main__":
    unittest.main()
