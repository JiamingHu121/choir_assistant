"""Portable import/resume boundary and source-identity regression checks."""
from argparse import Namespace
import json
from pathlib import Path
import tempfile
from unittest import TestCase, mock
from choir_assistant import cli
from workbench.calibrate import resolve_inputs
from workbench.score_model import build_model
from workbench.workflow import load_config, prepare_model, write_json, recognized_part_role, file_hash


def score(names):
    definition = ''.join(f'<score-part id="P{i}"><part-name>{name}</part-name></score-part>' for i,name in enumerate(names,1))
    bar = '<measure number="1"><attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes><note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><type>whole</type></note></measure>'
    parts = ''.join(f'<part id="P{i}">{bar}</part>' for i in range(1,len(names)+1))
    return '<score-partwise version="4.0"><work><work-title>Independent fixture</work-title></work><part-list>'+definition+'</part-list>'+parts+'</score-partwise>'


class PipelineBoundaryTests(TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name).resolve()
        self.pdf=self.root/'score.pdf';self.pdf.write_bytes(b'%PDF fixture only; OMR mocked')
        self.font=self.root/'font.sf2';self.font.write_bytes(b'font fixture')
        self.exports=self.root/'recognizer';self.exports.mkdir()
        self.xml=self.exports/'score.musicxml';self.omr=self.exports/'score.omr'
        self.xml.write_text(score(['Piano']));self.omr.write_bytes(b'OMR immutable fixture')
        self.cache=mock.patch('runtime.cache_dir',return_value=self.root/'cache');self.cache.start();self.addCleanup(self.cache.stop)
        self.omr_run=mock.patch('omr.audiveris_runner.run_omr',return_value=[str(self.xml)])
        self.recognizer=self.omr_run.start();self.addCleanup(self.omr_run.stop)

    def config(self):
        path=self.root/'config.json'
        write_json(path,{'input':{'pdf':str(self.pdf)},'soundfont':str(self.font),'rhythmPolicy':'draft'})
        return load_config(path)

    def import_args(self):
        return Namespace(pdf=str(self.pdf),output=str(self.root/'project'),part_map=None,score_index=None,
                         soundfont=str(self.font),title=None,project=None,port=8765,no_open=True,no_serve=True)

    def fake_scan(self, config, output, **kwargs):
        Path(output).mkdir(parents=True)
        return {'summary':{'measures':1,'symbols':1},'output':str(output)}

    def fake_build(self, config, output):
        Path(output).mkdir(parents=True)
        return {'revision':'fixture-revision','status':'complete','validation':{'scoreStatus':'unreviewed'}}

    def test_pdf_recognition_cache_binds_both_export_and_omr(self):
        config=self.config()
        self.assertEqual(resolve_inputs(config),resolve_inputs(config));self.recognizer.assert_called_once()
        self.omr.write_bytes(b'modified OMR')
        with self.assertRaisesRegex(ValueError,'Cached OMR project changed'):
            resolve_inputs(config)

    def test_pdf_recognition_cache_rejects_replaced_musicxml(self):
        config=self.config();resolve_inputs(config);self.xml.write_text(score(['Tenor']))
        with self.assertRaisesRegex(ValueError,'Cached OMR export changed'):
            resolve_inputs(config)

    def test_nine_ordinal_ids_never_imply_satb_or_a_project_title(self):
        self.xml.write_text(score(['Voice']*8+['Piano']))
        model=build_model(source_path=self.xml,omr_path='')
        self.assertEqual(model['title'],'Independent fixture')
        self.assertEqual([part['part'] for part in model['parts']],['Voice']*8+['Piano'])
        path=self.root/'ambiguous.json';write_json(path,{'input':{'musicxml':str(self.xml)},'soundfont':str(self.font),'rhythmPolicy':'draft'})
        with self.assertRaisesRegex(ValueError,'Add partMap'):
            prepare_model(load_config(path),self.xml)

    def test_named_piano_import_scans_and_builds_without_second_omr(self):
        with mock.patch('workbench.calibrate.scan',side_effect=self.fake_scan) as scan, mock.patch('workbench.workflow.build',side_effect=self.fake_build) as build:
            result=cli.initialize(self.import_args())
        self.assertEqual(result['status'],'ready');self.recognizer.assert_called_once();scan.assert_called_once();build.assert_called_once()
        config=json.loads((self.root/'project/project.json').read_text())
        self.assertIn('musicxml',config['input']);self.assertEqual(config['sourcePdf'],str(self.root/'project/inputs/original.pdf'))
        self.assertTrue(Path(scan.call_args.args[1]).is_relative_to(self.root/'project/preview/calibration'))
        self.assertEqual(cli.inspect_source(config['input']['musicxml'])['parts'][0]['recognizedRole'],'Piano')

    def test_ambiguous_voice_stops_for_source_mapping_then_resumes(self):
        self.xml.write_text(score(['Voice','Voice']));args=self.import_args()
        with mock.patch('workbench.calibrate.scan') as scan, mock.patch('workbench.workflow.build') as build:
            result=cli.initialize(args)
        self.assertEqual(result['status'],'needs-part-map');scan.assert_not_called();build.assert_not_called()
        self.assertEqual(json.loads((self.root/'project/part-map.template.json').read_text()),{'P1':None,'P2':None})
        mapping=self.root/'mapping.json';write_json(mapping,{'P1':'T','P2':'B'});args.part_map=str(mapping)
        with mock.patch('workbench.calibrate.scan',side_effect=self.fake_scan), mock.patch('workbench.workflow.build',side_effect=self.fake_build):
            resumed=cli.execute(args)
        self.assertEqual(resumed['status'],'ready');self.recognizer.assert_called_once()
        self.assertNotIn('unknownParts', resumed);self.assertNotIn('nextStep', resumed)
        saved=json.loads((self.root/'project/run-state.json').read_text())
        self.assertNotIn('unknownParts',saved);self.assertNotIn('nextStep',saved)
        config=load_config(args.project);prepared=prepare_model(config,config['input']['musicxml'],omr='')
        self.assertEqual([part['part'] for part in prepared['parts']],['T','B'])

    def test_installed_tree_cannot_be_used_as_mutable_project(self):
        with self.assertRaisesRegex(ValueError,'outside the installed plugin'):
            cli.external_path(cli.INSTALL_ROOT/'generated/new-project')

    def test_cross_project_apply_is_rejected_before_any_mutation(self):
        args=Namespace(run=str(self.root/'foreign/run'),project=str(self.root/'project/project.json'),review=str(self.root/'review.json'),accept_source_reviewed=False)
        with mock.patch('workbench.calibrate.apply_review') as apply:
            with self.assertRaisesRegex(ValueError,'outside this project'):
                cli.apply_project(args)
            apply.assert_not_called()

    def test_explicit_piano_abbreviations_are_recognized_without_guessing_voice(self):
        for name in ('Pno', 'Pno.', ' pno. ', 'Piano', 'Pianoforte'):
            with self.subTest(name=name):
                self.assertEqual(recognized_part_role(name), 'Piano')
        self.assertIsNone(recognized_part_role('Voice'))
        self.xml.write_text(score(['Pno.']))
        with mock.patch('workbench.calibrate.scan',side_effect=self.fake_scan), mock.patch('workbench.workflow.build',side_effect=self.fake_build):
            self.assertEqual(cli.initialize(self.import_args())['status'], 'ready')

    def frozen_project_run(self):
        from workbench.calibrate import model_from_config
        root = self.root / 'apply-project'
        root.mkdir(exist_ok=True)
        xml = root / 'source.musicxml'
        xml.write_text(score(['Piano']))
        project = root / 'project.json'
        write_json(project, {'input': {'musicxml': str(xml)}, 'sourcePdf': str(self.pdf),
                             'soundfont': str(self.font), 'rhythmPolicy': 'draft'})
        write_json(root / 'run-state.json', {'status': 'recognized'})
        config = load_config(project)
        model = model_from_config(config)
        run = root / 'preview/calibration/frozen'
        run.mkdir(parents=True, exist_ok=True)
        write_json(run / 'run.json', {'revision': model['revision'], 'sources': {
            'musicxml': {'path': str(xml), 'sha256': file_hash(xml)},
            'pdf': {'path': str(self.pdf), 'sha256': file_hash(self.pdf)}}})
        write_json(run / 'plan.json', {'revision': model['revision']})
        write_json(run / 'model-before.json', {'revision': model['revision']})
        args = Namespace(run=str(run), project=str(project), review='decisions.json', accept_source_reviewed=False)
        return args, config, xml

    def test_same_project_path_stale_source_config_or_ledger_is_rejected_before_apply(self):
        for changed in ('source', 'config', 'ledger'):
            with self.subTest(changed=changed):
                args, config, xml = self.frozen_project_run()
                if changed == 'source':
                    xml.write_text(score(['Piano']).replace('<step>C</step>', '<step>D</step>'))
                elif changed == 'config':
                    config['playback']['transpose'] = 1
                    write_json(args.project, config)
                else:
                    ledger = Path(args.project).parent / 'latest-ledger.json'
                    write_json(ledger, {'n-P1-m1-o1': {'pitch': 62, 'reason': 'visible independent source correction'}})
                    config['corrections'] = str(ledger)
                    write_json(args.project, config)
                with mock.patch('workbench.calibrate.apply_review') as apply:
                    with self.assertRaisesRegex(ValueError, 'active project'):
                        cli.apply_project(args)
                    apply.assert_not_called()

    def test_same_xml_with_another_original_pdf_is_rejected_before_apply(self):
        args, config, xml = self.frozen_project_run()
        other = self.root / 'other.pdf'
        other.write_bytes(b'another source PDF')
        config['sourcePdf'] = str(other)
        write_json(args.project, config)
        with mock.patch('workbench.calibrate.apply_review') as apply:
            with self.assertRaisesRegex(ValueError, 'source files differ'):
                cli.apply_project(args)
            apply.assert_not_called()

    def test_apply_uses_active_preview_config_and_its_latest_ledger(self):
        from workbench.calibrate import model_from_config
        args, config, xml = self.frozen_project_run()
        ledger = Path(args.project).parent / 'ui-reviewed-ledger.json'
        write_json(ledger, {'n-P1-m1-o1': {'pitch': 62, 'reason': 'latest UI source review'}})
        active = {**config, 'corrections': str(ledger)}
        revision = model_from_config(active)['revision']
        run = Path(args.run)
        record = json.loads((run / 'run.json').read_text())
        write_json(run / 'run.json', {**record, 'revision': revision})
        write_json(run / 'plan.json', {'revision': revision})
        write_json(run / 'model-before.json', {'revision': revision})
        paths = cli.project_paths(args.project)
        with mock.patch.object(cli, 'active_project', return_value=(*paths, {'status': 'ready'}, active)) as resolve, mock.patch('workbench.calibrate.apply_review', return_value={'status': 'validated-proposal'}) as apply:
            self.assertEqual(cli.apply_project(args)['status'], 'validated-proposal')
            resolve.assert_called_once()
            apply.assert_called_once_with(run, 'decisions.json')

    def test_part_map_changes_revision_while_source_identity_stays_stable(self):
        self.xml.write_text(score(['Voice']));path=self.root/'explicit.json'
        write_json(path,{'input':{'musicxml':str(self.xml)},'soundfont':str(self.font),'rhythmPolicy':'draft','partMap':{'P1':'T'}})
        config=load_config(path);before=prepare_model(config,self.xml,omr='');after=prepare_model({**config,'partMap':{'P1':'B'}},self.xml,omr='')
        self.assertNotEqual(before['revision'],after['revision']);self.assertEqual(before['provenance']['sourceSha256'],after['provenance']['sourceSha256'])
        self.assertEqual(before['notes'][0]['id'],after['notes'][0]['id'])
