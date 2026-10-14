"""The project verifier must inspect the same bundle selected in the workbench."""
from argparse import Namespace
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from choir_assistant.cli import verify_project


class VerificationCommandTests(unittest.TestCase):
    def test_active_review_pointer_is_used_only_for_the_same_base_and_project(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();project=root/'project.json';base=root/'base';reviewed=root/'reviewed';generated=root/'preview';generated.mkdir()
            project.write_text('{}');(root/'run-state.json').write_text(json.dumps({'latestBundle':str(base)}))
            args=Namespace(bundle=None,project=str(project),facts=None,output=None)
            for base_id,project_id,wanted in [(str(base),str(project),reviewed),('other-base',str(project),base),(str(base),'other-project',base)]:
                with self.subTest(base=base_id,project=project_id):
                    (generated/'current.json').write_text(json.dumps({'baseBundle':base_id,'configPath':project_id,'bundle':str(reviewed)}))
                    with mock.patch('workbench.source_verification.verify_bundle',return_value={'passed':True}) as verify:
                        verify_project(args)
                    self.assertEqual(verify.call_args.args[0],wanted)

    def test_output_cannot_overwrite_frozen_bundle_or_source_facts(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d).resolve();bundle=root/'bundle';bundle.mkdir();facts=root/'facts.json';facts.write_text('original evidence')
            for output in [bundle/'assets/interactive.json',facts]:
                with self.subTest(output=output):
                    args=Namespace(bundle=str(bundle),project=None,facts=str(facts),output=str(output))
                    with mock.patch('workbench.source_verification.verify_bundle',return_value={'passed':True}):
                        with self.assertRaisesRegex(ValueError,'must not overwrite'):
                            verify_project(args)
            self.assertEqual(facts.read_text(),'original evidence')
