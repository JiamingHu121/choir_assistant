"""Independent checks reject real identity, timing, and source-evidence errors."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from workbench.score_model import build_model, public_model, write_canonical
from workbench.source_verification import verify_bundle


def note(step="C", duration=1, *, octave=4, voice="1", staff=1, extra="", xml_id=None):
    identifier = f' id="{xml_id}"' if xml_id else ""
    return (f'<note{identifier}>{extra}<pitch><step>{step}</step><octave>{octave}</octave></pitch>'
            f'<duration>{duration}</duration><voice>{voice}</voice><type>quarter</type><staff>{staff}</staff></note>')


def score(body, divisions=1, meter="4/4"):
    beats, unit = meter.split("/")
    return ('<score-partwise version="4.0"><part-list><score-part id="P1"><part-name>Test</part-name>'
            '</score-part></part-list><part id="P1"><measure number="1"><attributes>'
            f'<divisions>{divisions}</divisions><time><beats>{beats}</beats><beat-type>{unit}</beat-type>'
            '</time></attributes>' + body + '</measure></part></score-partwise>')


class SourceVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name)
        (self.bundle / "inputs").mkdir()
        (self.bundle / "assets").mkdir()
        self.model_path = self.bundle / "assets/interactive.json"
        self.canonical_path = self.bundle / "assets/canonical.musicxml"
        self.source_path = self.bundle / "inputs/source.musicxml"

    def build(self, xml, corrections=None):
        self.source_path.write_text(xml, encoding="utf-8")
        model = build_model(corrections, source_path=self.source_path)
        # prepare_model assigns a revision after importer/correction processing.
        # This fixture has no audio/runtime dependency, so supply that envelope.
        model["revision"] = sha256((xml + json.dumps(corrections)).encode()).hexdigest()[:16]
        write_canonical(model, self.canonical_path)
        self.model_path.write_text(json.dumps(public_model(model)), encoding="utf-8")
        self.manifest = {"revision": model["revision"], "status": "complete", "input": {
            "kind": "musicxml", "originalPath": "/unavailable/computer/source.musicxml",
            "musicxml": "inputs/source.musicxml"}, "artifacts": []}
        self.envelope()
        return model

    def envelope(self):
        """Rebind synthetic fixture metadata, so semantic tests reach semantics."""
        self.manifest["input"]["musicxmlFileSha256"] = sha256(self.source_path.read_bytes()).hexdigest()
        self.manifest["artifacts"] = [{"path": str(path.relative_to(self.bundle)),
                                       "sha256": sha256(path.read_bytes()).hexdigest()}
                                      for path in (self.source_path, self.canonical_path, self.model_path)]
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def facts(self, events):
        current = verify_bundle(self.bundle)
        self.assertTrue(current["passed"], current)
        return {"schemaVersion": 1, "revision": current["revision"],
                "hashes": {key: current["hashes"][key] for key in ("sourceMusicxml", "canonicalMusicxml", "model")},
                "scope": "Frozen source-image reading of P1, bar 1, staff 1",
                "staffBars": [{"sourcePart": "P1", "measureIndex": 1, "staff": 1, "events": events}]}

    @staticmethod
    def expected(pitch="C4", offset="0", duration="1", **optional):
        return {"kind": "note", "pitchName": pitch, "offset": offset,
                "duration": duration, **optional}

    def mutate_canonical(self, operation):
        root = ET.parse(self.canonical_path)
        operation(root)
        root.write(self.canonical_path, encoding="utf-8")
        self.envelope()

    def kinds(self, report):
        return {error["kind"] for error in report["artifactConsistency"]["errors"]}

    def test_original_xml_ids_are_not_stable_identity_and_no_facts_is_not_source_review(self):
        self.build(score(note(xml_id="arbitrary-original-id")))
        report = verify_bundle(self.bundle)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["sourceFidelity"]["status"], "not-supplied")
        self.assertIsNone(report["sourceFidelity"]["passed"])
        self.assertEqual(report["sourcePdfStatus"], "not-frozen")
        self.assertEqual(report["artifactConsistency"]["sourceSymbols"], 1)

    def test_midbar_divisions_backup_forward_and_chord_use_exact_sequential_units(self):
        body = (note(duration=2) + '<attributes><divisions>6</divisions></attributes>'
                + note("D", 3) + note("F", 3, extra='<chord/>')
                + '<backup><duration>9</duration></backup><forward><duration>3</duration></forward>'
                + note("E", 6, voice="2") + '<backup><duration>9</duration></backup>'
                + note("G", 9, voice="3", staff=2))
        model = self.build(score(body, divisions=2, meter="3/8"))
        report = verify_bundle(self.bundle)
        self.assertTrue(report["passed"], report)
        self.assertEqual([n["offset"] for n in model["notes"]], [0, 1, 1, .5, 0])
        frozen = self.facts([self.expected(), self.expected("D4", "1", "1/2"),
                             self.expected("F4", "1", "1/2"), self.expected("E4", "1/2", "1")])
        self.assertTrue(verify_bundle(self.bundle, frozen)["passed"])

    def test_canonical_wrong_id_and_reordered_nodes_are_rejected(self):
        self.build(score(note() + note("D")))
        self.mutate_canonical(lambda tree: tree.find("part/measure/note").set("id", "stale-id"))
        wrong_id = verify_bundle(self.bundle)
        self.assertFalse(wrong_id["passed"])
        self.assertIn("canonical-extra-symbol", self.kinds(wrong_id))
        self.assertIn("model-extra-symbol", self.kinds(wrong_id))
        self.build(score(note() + note("D")))

        def reorder(tree):
            measure = tree.find("part/measure")
            first, second = measure.findall("note")
            measure.remove(first)
            measure.insert(list(measure).index(second) + 1, first)

        self.mutate_canonical(reorder)
        reordered = verify_bundle(self.bundle)
        self.assertFalse(reordered["passed"])
        self.assertTrue(any(e.get("field") == "offset" for e in reordered["artifactConsistency"]["errors"]))

    def test_source_reordering_cannot_reuse_ordinal_snapshots_silently(self):
        self.build(score(note(xml_id="id-a") + note("D", xml_id="id-b")))
        self.source_path.write_text(score(note("D", xml_id="id-b") + note(xml_id="id-a")), encoding="utf-8")
        self.envelope()
        report = verify_bundle(self.bundle)
        self.assertFalse(report["passed"])
        self.assertIn("source-snapshot-mismatch", self.kinds(report))

    def test_accepted_pitch_and_duration_ledger_is_not_a_false_source_failure(self):
        self.build(score(note(duration=2) + note("D", duration=2)), {
            "n-P1-m1-o1": {"pitch": 72, "pitchName": "C5", "duration": 1,
                            "reason": "Original PDF reading corrected octave and duration"}})
        report = verify_bundle(self.bundle)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["artifactConsistency"]["sourceComparison"], "original-snapshots-checked")
        changed = report["artifactConsistency"]["sourceToCurrentChanges"]
        self.assertTrue(any("pitch" in item.get("fields", []) for item in changed))
        self.assertTrue(any("offset" in item.get("fields", []) for item in changed))

    def test_duplicate_unisons_are_multisets_and_optional_voice_needs_non_greedy_matching(self):
        self.build(score(note(voice="1") + '<backup><duration>1</duration></backup>' + note(voice="2")))
        frozen = self.facts([self.expected(), self.expected(voice="1")])
        report = verify_bundle(self.bundle, frozen)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["sourceFidelity"]["sourceObjectsChecked"], 2)
        self.assertEqual(report["sourceFidelity"]["status"], "selected-staff-bars-matched")
        frozen["staffBars"][0]["events"].pop()
        missing_fact = verify_bundle(self.bundle, frozen)
        self.assertFalse(missing_fact["passed"])
        self.assertTrue(any(e["kind"] == "unexpected-model-event" for e in missing_fact["sourceFidelity"]["errors"]))

    def test_reviewed_insertion_preserves_original_locator_without_reusing_ordinal(self):
        self.build(score(note() + note("D"), meter="2/4"), {
            "n-P1-m1-o1": {"duration": .5, "reason": "Source PDF has an eighth"},
            "__insertions__": {"r-review-P1-m1-1": {
                "sourcePart": "P1", "measureIndex": 1, "afterNoteId": "n-P1-m1-o1",
                "kind": "rest", "duration": .5, "reason": "Source PDF has an eighth rest",
                "evidence": "Source page 1, staff 1"}}})
        report = verify_bundle(self.bundle)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["artifactConsistency"]["sourceSymbols"], 2)
        self.assertEqual(report["artifactConsistency"]["modelSymbols"], 3)
        self.assertTrue(any(item["kind"] == "reviewed-insertion" for item in
                            report["artifactConsistency"]["sourceToCurrentChanges"]))

    def test_repaired_invalid_omr_duration_remains_a_diagnostic_not_current_failure(self):
        self.build(score(note(duration=0)), {"n-P1-m1-o1": {
            "duration": 1, "reason": "Source image clearly has a quarter note"}})
        report = verify_bundle(self.bundle)
        self.assertTrue(report["passed"], report)
        self.assertTrue(any(error["kind"] == "invalid-note-duration" for error in
                            report["artifactConsistency"]["sourceDiagnostics"]))

    def test_frozen_wrong_octave_duration_missing_extra_and_ties_are_rejected(self):
        self.build(score(note(extra='<tie type="start"/>')))
        original = self.facts([self.expected(tieTypes=["start"])])
        self.assertTrue(verify_bundle(self.bundle, original)["passed"])
        for mutation in ({"pitchName": "C5"}, {"duration": "2"}, {"tieTypes": []}):
            with self.subTest(mutation=mutation):
                frozen = deepcopy(original)
                frozen["staffBars"][0]["events"][0].update(mutation)
                self.assertFalse(verify_bundle(self.bundle, frozen)["passed"])
        extra = deepcopy(original)
        extra["staffBars"][0]["events"].append(self.expected("D4"))
        self.assertFalse(verify_bundle(self.bundle, extra)["passed"])
        self.mutate_canonical(lambda tree: tree.find("part/measure/note/pitch/octave").__setattr__("text", "5"))
        report = verify_bundle(self.bundle)
        self.assertFalse(report["passed"])
        self.assertTrue(any(e.get("field") == "pitch" for e in report["artifactConsistency"]["errors"]))

    def test_empty_facts_and_unknown_staff_bar_do_not_become_source_reviewed(self):
        self.build(score(note()))
        frozen = self.facts([])
        self.assertFalse(verify_bundle(self.bundle, frozen)["passed"])
        frozen["staffBars"][0]["events"] = [self.expected()]
        frozen["staffBars"][0]["measureIndex"] = 99
        report = verify_bundle(self.bundle, frozen)
        self.assertFalse(report["passed"])
        frozen["staffBars"] = []
        report = verify_bundle(self.bundle, frozen)
        self.assertFalse(report["passed"])
        self.assertEqual(report["sourceFidelity"]["sourceObjectsChecked"], 0)

    def test_stale_revision_hash_and_post_build_asset_tampering_are_rejected(self):
        self.build(score(note()))
        original = self.facts([self.expected()])
        for field in ("revision", "sourceMusicxml", "canonicalMusicxml", "model"):
            with self.subTest(field=field):
                frozen = deepcopy(original)
                if field == "revision":
                    frozen["revision"] = "previous-revision"
                else:
                    frozen["hashes"][field] = "0" * 64
                self.assertFalse(verify_bundle(self.bundle, frozen)["passed"])
        self.canonical_path.write_text(self.canonical_path.read_text() + "\n", encoding="utf-8")
        report = verify_bundle(self.bundle)
        self.assertFalse(report["passed"])
        self.assertIn("manifest-artifact-hash-mismatch", self.kinds(report))

    def test_compressed_source_pdf_snapshot_hash_and_path_escape(self):
        self.build(score(note()))
        compressed = self.bundle / "inputs/score.mxl"
        with zipfile.ZipFile(compressed, "w") as archive:
            archive.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="score.xml"/></rootfiles></container>')
            archive.writestr("score.xml", self.source_path.read_bytes())
        self.manifest["input"]["musicxml"] = "inputs/score.mxl"
        self.manifest["input"]["musicxmlFileSha256"] = sha256(compressed.read_bytes()).hexdigest()
        self.manifest["artifacts"].append({"path": "inputs/score.mxl", "sha256": sha256(compressed.read_bytes()).hexdigest()})
        pdf = self.bundle / "inputs/original.pdf"
        pdf.write_bytes(b"%PDF-1.4\nsynthetic source fingerprint\n")
        self.manifest["sourcePdf"] = {"path": "inputs/original.pdf", "sha256": sha256(pdf.read_bytes()).hexdigest()}
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        report = verify_bundle(self.bundle)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["hashes"]["sourcePdf"], sha256(pdf.read_bytes()).hexdigest())
        frozen = self.facts([self.expected()])
        frozen["hashes"]["sourcePdf"] = report["hashes"]["sourcePdf"]
        self.assertTrue(verify_bundle(self.bundle, frozen)["passed"])
        unbound = deepcopy(frozen)
        del unbound["hashes"]["sourcePdf"]
        self.assertFalse(verify_bundle(self.bundle, unbound)["passed"])
        frozen["hashes"]["sourcePdf"] = "0" * 64
        self.assertFalse(verify_bundle(self.bundle, frozen)["passed"])
        pdf.write_bytes(b"%PDF-1.4\nchanged source\n")
        self.assertIn("manifest-source-pdf-hash-mismatch", self.kinds(verify_bundle(self.bundle)))
        self.manifest["input"]["musicxml"] = "../outside.musicxml"
        (self.bundle / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        report = verify_bundle(self.bundle)
        self.assertFalse(report["passed"])
        self.assertIn("invalid-verification-input", self.kinds(report))

    def test_facts_file_byte_hash_is_recorded_and_no_assets_are_mutated(self):
        self.build(score(note()))
        frozen = self.facts([self.expected()])
        facts_file = self.bundle / "frozen.json"
        facts_file.write_text(json.dumps(frozen, indent=2), encoding="utf-8")
        before = {path: path.read_bytes() for path in self.bundle.rglob("*") if path.is_file()}
        report = verify_bundle(self.bundle, facts_file)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["hashes"]["facts"], sha256(facts_file.read_bytes()).hexdigest())
        self.assertEqual(report["factsHashKind"], "file-bytes")
        self.assertEqual(before, {path: path.read_bytes() for path in self.bundle.rglob("*") if path.is_file()})

    def test_declared_noncore_svg_and_audio_cannot_be_missing_tampered_or_wrong_size(self):
        self.build(score(note()))
        for filename in ("page-1.svg", "base.mp3"):
            with self.subTest(artifact=filename):
                path = self.bundle / "assets" / filename
                contents = b"synthetic rendered artifact"
                path.write_bytes(contents)
                self.envelope()
                item = {"path": f"assets/{filename}", "sha256": sha256(contents).hexdigest(), "bytes": len(contents)}
                self.manifest["artifacts"].append(item)
                (self.bundle / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
                self.assertTrue(verify_bundle(self.bundle)["passed"])
                path.unlink()
                self.assertIn("missing-manifest-artifact", self.kinds(verify_bundle(self.bundle)))
                path.write_bytes(b"tampered")
                report = verify_bundle(self.bundle)
                self.assertIn("manifest-artifact-hash-mismatch", self.kinds(report))
                self.assertIn("manifest-artifact-size-mismatch", self.kinds(report))
                path.write_bytes(contents)

    def test_duplicate_unsafe_incomplete_or_unindexed_core_manifest_is_rejected(self):
        self.build(score(note()))
        baseline = deepcopy(self.manifest)
        cases = [
            ("duplicate-manifest-artifact", {"artifacts": baseline["artifacts"] + [deepcopy(baseline["artifacts"][0])]}),
            ("unsafe-manifest-artifact-path", {"artifacts": baseline["artifacts"] + [{"path": "../escape.svg", "sha256": "0" * 64}]}),
            ("manifest-incomplete", {"status": "running"}),
            ("core-artifact-not-indexed", {"artifacts": baseline["artifacts"][1:]}),
        ]
        for kind, fields in cases:
            with self.subTest(kind=kind):
                manifest = deepcopy(baseline)
                manifest.update(fields)
                (self.bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                report = verify_bundle(self.bundle)
                self.assertFalse(report["passed"])
                self.assertIn(kind, self.kinds(report))


if __name__ == "__main__":
    unittest.main()
