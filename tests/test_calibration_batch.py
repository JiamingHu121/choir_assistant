"""Actual PDF-page evidence can review a score without OMR geometry."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from unittest import TestCase, mock
import xml.etree.ElementTree as ET

from runtime import find_binary
from workbench.calibrate import scan, source_review_decisions, apply_review
from workbench.workflow import load_config, file_hash, write_json

ROOT = Path(__file__).resolve().parents[1]


class PageEvidenceBatchTests(TestCase):
    def setUp(self):
        if not find_binary('pdftoppm', required=False):
            self.skipTest('Real original-PDF evidence regression requires pdftoppm')
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        cache = mock.patch('runtime.cache_dir', return_value=self.root / 'cache')
        cache.start()
        self.addCleanup(cache.stop)
        # The distributed, authored PDF visibly starts with C4. Inject a false
        # C#4 into its truth MusicXML and a separate rhythm hypothesis that must
        # remain unaccepted. No OMR geometry or note positions are available.
        self.pdf = ROOT / 'examples/practice-study.pdf'
        xml = ET.parse(ROOT / 'examples/practice-study.musicxml')
        pitch = xml.find('part/measure/note/pitch')
        alter = ET.Element('alter')
        alter.text = '1'
        pitch.insert(1, alter)
        xml.find('part/measure/note/type').text = 'eighth'
        self.source = self.root / 'recognition.musicxml'
        xml.write(self.source, encoding='utf-8', xml_declaration=True)
        font = self.root / 'not-rendered.sf2'
        font.write_bytes(b'Correction transaction test does not synthesize')
        project = self.root / 'project.json'
        write_json(project, {'input': {'musicxml': str(self.source)}, 'sourcePdf': str(self.pdf),
                             'soundfont': str(font), 'rhythmPolicy': 'draft'})
        self.config = load_config(project)
        initial = self.root / 'initial'
        scan(self.config, initial)
        self.packet = json.loads((initial / 'evidence/packet.json').read_text())
        self.assertEqual(self.packet['summary']['locatedMeasures'], 0)
        self.assertTrue(self.packet['pages'])
        self.assertTrue(all('crop' not in item for item in self.packet['measures']))
        self.model = json.loads((initial / 'model-before.json').read_text())
        self.target = self.model['notes'][0]['id']
        page = self.packet['pages'][0]
        self.page_path = initial / 'evidence' / page['path']
        pdf_hash = file_hash(self.pdf)
        self.findings = {'sourceSha256': self.model['provenance']['sourceSha256'],
                         'sourcePdfSha256': pdf_hash, 'fields': {self.target: {
            'sourceSha256': self.model['provenance']['sourceSha256'], 'sourcePdfSha256': pdf_hash,
            'status': 'source-verified', 'pitch': 60, 'reason': 'Authored original PDF page 1 starts with C4, without a sharp',
            'reference': {'source': 'original-pdf', 'sourceSha256': pdf_hash, 'page': page['page'],
                          'path': str(self.page_path), 'sha256': page['sha256']}}}}

    def test_original_page_without_geometry_applies_only_the_confirmed_field(self):
        run = self.root / 'reviewed-page'
        scan(self.config, run, evidence=self.findings)
        plan = json.loads((run / 'plan.json').read_text())
        self.assertTrue(any(item['ruleId'] != 'source-verified' for item in plan['candidates']))
        decisions = source_review_decisions(run)
        self.assertEqual(len(decisions['decisions']), 1)
        selected = {item['candidateId'] for item in decisions['decisions']}
        self.assertTrue(all(item['ruleId'] == 'source-verified' for item in plan['candidates'] if item['id'] in selected))
        proof = decisions['decisions'][0]['evidence'][0]
        self.assertEqual(proof['page'], 1)
        self.assertEqual(Path(proof['path']), run / 'evidence/pages/page-001.png')
        self.assertEqual(file_hash(proof['path']), proof['sha256'])
        result = apply_review(run, decisions, build_bundle=False)
        after = json.loads((run / 'model-after.json').read_text())
        self.assertEqual(next(note['pitch'] for note in after['notes'] if note['id'] == self.target), 60)
        self.assertEqual(next(note['source']['pitch'] for note in after['notes'] if note['id'] == self.target), 61)
        self.assertEqual(result['delta']['appliedEdits'], 1)
        self.assertEqual(result['delta']['sourceIdsPreserved'], len(self.model['notes']) + len(self.model['rests']))
        self.assertTrue(result['delta']['originalProvenancePreserved'])

    def test_explicit_original_pdf_reference_reuses_its_verified_page(self):
        findings = deepcopy(self.findings)
        reference = findings['fields'][self.target]['reference']
        reference.update(path=str(self.pdf), sha256=file_hash(self.pdf))
        run = self.root / 'original-pdf-reference'
        scan(self.config, run, evidence=findings)
        decisions = source_review_decisions(run)
        self.assertEqual(len(decisions['decisions']), 1)
        proof = decisions['decisions'][0]['evidence'][0]
        self.assertEqual(Path(proof['path']), run / 'evidence/pages/page-001.png')
        self.assertEqual(proof['sourceSha256'], file_hash(self.pdf))
        self.assertEqual(proof['page'], 1)

    def test_nonexistent_page_or_foreign_image_is_never_promoted_to_page_proof(self):
        for case in ('nonexistent-page', 'foreign-image'):
            with self.subTest(case=case):
                findings = deepcopy(self.findings)
                reference = findings['fields'][self.target]['reference']
                if case == 'nonexistent-page':
                    reference['page'] = 99
                else:
                    foreign = self.root / 'unrelated-image.png'
                    foreign.write_bytes(b'Unrelated file cannot prove an original score page')
                    reference.update(path=str(foreign), sha256=file_hash(foreign))
                run = self.root / case
                scan(self.config, run, evidence=findings)
                with self.assertRaisesRegex(ValueError, 'verified original-page evidence'):
                    source_review_decisions(run)
                self.assertFalse((run / 'application.json').exists())

    def test_changed_frozen_reference_is_rejected_even_when_current_packet_is_intact(self):
        run = self.root / 'changed-reference'
        scan(self.config, run, evidence=self.findings)
        self.page_path.write_bytes(b'Changed earlier evidence')
        with self.assertRaisesRegex(ValueError, 'verified original-page evidence'):
            source_review_decisions(run)
        self.assertFalse((run / 'application.json').exists())
