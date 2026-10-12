"""Portable launcher checks; never start or stop the user's server."""
import tempfile
from pathlib import Path
import sys
from unittest import TestCase, mock
from workbench import start

class LauncherTests(TestCase):
    def identity(self, root, bundle, config):
        return {"service":start.SERVICE,"workspace":str(root),"revision":"test","bundle":str(bundle),"baseBundle":str(bundle),"config":str(config)}

    def test_launcher_requires_an_explicit_project(self):
        with self.assertRaisesRegex(ValueError, "必须显式"):
            start.ensure_server(8765)

    def test_matching_server_checks_bundle_and_project_and_reuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();bundle=root/'bundle';config=root/'project.json'
            identity=self.identity(start.ROOT,bundle,config)
            self.assertTrue(start.matching_server(identity,bundle=bundle,config=config))
            self.assertFalse(start.matching_server({**identity,'service':'other'},bundle=bundle,config=config))
            self.assertFalse(start.matching_server(identity,bundle=root/'another',config=config))
            with mock.patch.object(start,'server_identity',return_value=identity), mock.patch.object(start.subprocess,'Popen') as launch:
                self.assertEqual(start.ensure_server(8765,bundle=bundle,config=config),identity)
                launch.assert_not_called()

    def test_foreign_server_is_never_stopped(self):
        with mock.patch.object(start,'server_identity',return_value=None), mock.patch.object(start,'port_occupied',return_value=True), mock.patch.object(start.subprocess,'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError,'未停止任何进程'):
                start.ensure_server(8765,bundle='/tmp/bundle',config='/tmp/project.json')
            launch.assert_not_called()

    def test_new_server_uses_current_python_and_external_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();bundle=root/'bundle';config=root/'project.json';generated=root/'preview'
            identity=self.identity(start.ROOT,bundle,config)
            process=mock.Mock();process.poll.return_value=None
            with mock.patch.object(start,'server_identity',side_effect=[None,None,identity]), mock.patch.object(start,'port_occupied',return_value=False), mock.patch.object(start.subprocess,'Popen',return_value=process) as launch, mock.patch.object(start.time,'sleep'):
                self.assertEqual(start.ensure_server(8877,bundle=bundle,config=config,generated=generated),identity)
            command=launch.call_args.args[0]
            self.assertEqual(command[0],str(Path(sys.executable).resolve()))
            self.assertIn(str(bundle),command)
            self.assertIn(str(config),command)
            self.assertTrue((generated/'server-launch.log').is_file())
            self.assertTrue(launch.call_args.kwargs['start_new_session'])

    def test_child_failure_reports_its_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();process=mock.Mock();process.poll.return_value=1
            with mock.patch.object(start,'server_identity',return_value=None), mock.patch.object(start,'port_occupied',return_value=False), mock.patch.object(start.subprocess,'Popen',return_value=process):
                with self.assertRaisesRegex(RuntimeError,'server-launch.log'):
                    start.ensure_server(8877,bundle=root/'bundle',config=root/'project.json')
