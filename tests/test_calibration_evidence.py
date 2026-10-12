"""Original-PDF evidence must cover clean bars and bind every review decision."""
from pathlib import Path
from unittest.mock import patch
import json
import tempfile
import unittest
import zipfile

from PIL import Image

from workbench.calibration_evidence import build_evidence_packet, file_sha256, json_sha256, _region


def fixture(directory, stack_count=2):
    pdf, omr = directory / "source.pdf", directory / "source.omr"
    pdf.write_bytes(b"test-pdf-source-identity")
    stacks = ''.join(f'<stack left="{20 + i * 100}" right="{110 + i * 100}"/>' for i in range(stack_count))
    xml = f'''<sheet><picture width="300" height="400"/><page><system id="1">
      <part id="1"><staff id="1"><lines><line><point x="20" y="100"/><point x="280" y="100"/></line>
      <line><point x="20" y="140"/><point x="280" y="140"/></line></lines></staff></part>{stacks}</system></page></sheet>'''
    with zipfile.ZipFile(omr, "w") as archive:
        archive.writestr("sheet#1/sheet#1.xml", xml)
    model = {"version": "fixture-v1", "provenance": {"sourceSha256": "xml-hash"},
             "measures": [{"index": i, "number": i, "page": 1, "system": 1} for i in (1, 2)],
             "notes": [{"id": "first", "measureIndex": 1, "sourcePart": "P1", "staff": 1, "voice": "1",
                         "duration": 1, "source": {"noteOrdinal": 1}},
                       {"id": "second", "measureIndex": 2, "sourcePart": "P1", "staff": 1, "voice": "1",
                        "duration": 2, "source": {"noteOrdinal": 1}}], "rests": [],
             "audit": {"issues": [{"id": "issue-1", "kind": "duration-overflow", "measure": 1,
                                    "evidence": {"sourcePart": "P1", "voice": "1", "noteIds": ["first"]}}]}}
    plan = {"candidates": [{"id": "candidate-1", "measureIndices": [1],
                            "edits": [{"objectId": "first", "field": "duration", "before": 1, "after": .5}]}]}
    return pdf, omr, model, plan


def render_fixture(pdf, output, dpi=300, renderer=None):
    output.mkdir(parents=True, exist_ok=True)
    page = output / "page-001.png"
    Image.new("RGB", (300, 400), "white").save(page)
    return {"sourceSha256": file_sha256(pdf), "dpi": dpi, "pages": [{"page": 1, "path": page.name,
             "sha256": file_sha256(page), "width": 300, "height": 400}]}


class CalibrationEvidenceTests(unittest.TestCase):
    def test_xml_only_keeps_all_bars_pending_without_fabricating_source_images(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            _, omr, model, plan = fixture(directory)
            with patch("workbench.calibration_evidence.render_pdf") as render:
                packet = build_evidence_packet(model, None, omr, directory / "packet", plan)
            render.assert_not_called()
            self.assertEqual(packet["summary"]["measures"], 2)
            self.assertEqual(packet["summary"]["locatedMeasures"], 0)
            self.assertFalse(packet["summary"]["sourceImagesAvailable"])
            self.assertEqual(packet["sourceImageStatus"], "sourceimage-unavailable")
            self.assertEqual(packet["pages"], [])
            self.assertEqual(packet["source"]["musicxmlSha256"], "xml-hash")
            self.assertIsNone(packet["sourceSha256"])
            self.assertEqual(packet["candidates"][0]["sourceEvidenceStatus"], "unresolved")
            self.assertEqual(packet["candidates"][0]["sourceEvidence"], [])
            for measure in packet["measures"]:
                self.assertEqual(measure["locatorStatus"], "unresolved")
                self.assertEqual(measure["pageReferences"], [])
                self.assertNotIn("crop", measure)
            template = json.loads((directory / "packet/decisions.template.json").read_text())
            self.assertEqual(template["sourceSha256"], "xml-hash")
            self.assertIsNone(template["sourcePdfSha256"])
            self.assertEqual(template["decisions"][0]["decision"], "uncertain")
            self.assertEqual(template["decisions"][0]["evidence"], [])
            self.assertIn("暂无原始 PDF", (directory / "packet/index.html").read_text())

    def test_all_measures_including_clean_scope_have_original_context_and_bound_template(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            pdf, omr, model, plan = fixture(directory)
            with patch("workbench.calibration_evidence.render_pdf", side_effect=render_fixture):
                packet = build_evidence_packet(model, pdf, omr, directory / "packet", plan)
            self.assertEqual(packet["summary"]["measures"], 2)
            self.assertEqual(packet["summary"]["locatedMeasures"], 2)
            self.assertEqual(packet["measures"][1]["issueIds"], [])
            self.assertEqual(packet["planSha256"], json_sha256(plan))
            template = json.loads((directory / "packet/decisions.template.json").read_text())
            self.assertEqual(template["decisions"][0]["decision"], "uncertain")
            self.assertEqual(template["sourceSha256"], "xml-hash")
            self.assertEqual(template["sourcePdfSha256"], file_sha256(pdf))
            self.assertTrue(Path(template["decisions"][0]["evidence"][0]["path"]).is_absolute())
            for measure in packet["measures"]:
                reference = measure["crop"]
                self.assertEqual(reference["source"], "original-pdf")
                self.assertEqual(file_sha256(directory / "packet" / reference["path"]), reference["sha256"])
            clean = json.loads((directory / "packet/source-review-queue.json").read_text())
            serialized = json.dumps(clean)
            for term in ("candidate-1", '"duration"', '"after"', "duration-overflow"):
                self.assertNotIn(term, serialized)
            self.assertFalse(clean["engineEditsVisible"])
            self.assertEqual(len(clean["measures"]), 2)

    def test_stack_mismatch_never_fabricates_positional_source_match(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            pdf, omr, model, plan = fixture(directory, stack_count=1)
            with patch("workbench.calibration_evidence.render_pdf", side_effect=render_fixture):
                packet = build_evidence_packet(model, pdf, omr, directory / "packet", plan)
            self.assertEqual(packet["geometry"]["status"], "unresolved")
            self.assertEqual(packet["summary"]["locatedMeasures"], 0)
            self.assertEqual(packet["candidates"][0]["sourceEvidenceStatus"], "unresolved")
            self.assertTrue(packet["measures"][0]["pageReferences"])
            self.assertNotIn("crop", packet["measures"][0])

    def test_existing_page_system_join_disagreement_and_aspect_ratio_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            pdf, omr, model, plan = fixture(directory)
            model["measures"][0]["system"] = 2
            with patch("workbench.calibration_evidence.render_pdf", side_effect=render_fixture):
                packet = build_evidence_packet(model, pdf, omr, directory / "packet", plan)
            self.assertEqual(packet["geometry"]["status"], "unresolved")
            self.assertIn("disagrees", packet["geometry"]["error"])

    def test_global_issue_spans_all_declared_measures_and_note_edit_locates_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            pdf, omr, model, plan = fixture(directory)
            model["audit"]["issues"] += [{"id": "global", "kind": "meter-map", "measure": 1,
                                            "blockingMeasureIndices": [1, 2], "evidence": {}}]
            del plan["candidates"][0]["measureIndices"]
            with patch("workbench.calibration_evidence.render_pdf", side_effect=render_fixture):
                packet = build_evidence_packet(model, pdf, omr, directory / "packet", plan)
            self.assertIn("global", packet["measures"][1]["issueIds"])
            self.assertEqual(packet["candidates"][0]["measureIndices"], [1])

    def test_invalid_regions_do_not_create_deceptive_empty_evidence(self):
        for region in ({"x": 2, "y": 0, "w": .1, "h": .1},
                       {"x": 0, "y": 0, "w": 0, "h": .1},
                       {"x": float("nan"), "y": 0, "w": .1, "h": .1}):
            with self.subTest(region=region), self.assertRaises(ValueError):
                _region(region)
        self.assertEqual(_region({"x": -.1, "y": 0, "w": .2, "h": .3})["w"], .1)


if __name__ == "__main__":
    unittest.main()
