"""Unprivileged publication stages data while root-owned services stay untouched."""
from copy import deepcopy
from contextlib import redirect_stdout
from types import SimpleNamespace
import io
import json
import os
from pathlib import Path
import tempfile
from unittest import TestCase, mock

from choir_assistant.deployment import isolated, remote, sync
import test_site_sync as fixtures


class IsolatedPublishingTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.root = self.base / "isolated-host"
        (self.root / "releases").mkdir(parents=True)
        (self.root / "incoming/releases").mkdir(parents=True)
        for path in (self.root / "incoming", self.root / "incoming/releases"):
            path.chmod(0o700)
        self.owner = mock.patch.object(isolated, "SYSTEM_OWNER", os.getuid())
        self.owner.start()
        self.addCleanup(self.owner.stop)
        initial = self.root / "releases/initial"
        self.old_entries = fixtures.release_at(initial)
        self.old = initial.with_name("release-" + remote.digest(initial / "file-manifest.json")[:20])
        initial.rename(self.old)
        self.public_state = {"schemaVersion": 1, "publisherMode": "isolated-manual", "release": self.old.name,
                             "manifestSha256": remote.digest(self.old / "file-manifest.json"),
                             "incomingRoot": str(self.root / "incoming"), "phase": "committed"}
        (self.root / "public-state.json").write_text(json.dumps(self.public_state))
        (self.root / "config").mkdir()
        (self.root / "config/do-not-read-or-write").write_bytes(b"unrelated private configuration")
        self.profile = {"schemaVersion": 1, "publisherMode": "isolated-manual", "libraryRoot": str(self.base / "library"),
                        "outputRoot": str(self.base / "exports"), "sshHost": "fixture-server", "remoteRoot": str(self.root),
                        "remotePython": "/usr/local/bin/python3", "isolatedPublishHelper": str(self.root / "audit/install.py"),
                        "publicUrl": "https://scores.example.org", "bind": "127.0.0.1", "port": 8782,
                        "healthTimeoutSeconds": 5}
        self.profile_path = self.base / "profile.json"
        self.profile_path.write_text(json.dumps(self.profile))
        self.site = isolated.IsolatedSite(self.profile, remote)

    def plan(self):
        local = self.base / "local-release"
        entries = fixtures.release_at(local, "v2")
        target = local.with_name("release-" + remote.digest(local / "file-manifest.json")[:20])
        local.rename(target)
        plan = sync._plan(target, entries, {"fixture/main": "v2"}, self.site.inspect())
        archive = self.base / "transfer.tar"
        sync._archive(target, plan["changed"], archive)
        plan.update(archiveBytes=archive.stat().st_size, archiveSha256=remote.digest(archive), action="sync")
        return target, entries, plan, archive

    def protected_snapshot(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mode)
                for path in self.root.rglob("*") if path.is_file() and self.root / "incoming" not in path.parents}

    def test_stage_is_incremental_complete_and_never_activates_or_modifies_services(self):
        target, entries, plan, archive = self.plan()
        before = self.protected_snapshot()
        self.assertEqual(plan["changed"], ["file-manifest.json", "release.json"])
        with mock.patch("subprocess.run", side_effect=AssertionError("No process/service operation is allowed")):
            with archive.open("rb") as stream:
                result = isolated.dispatch(self.profile, plan, stream, remote)
        self.assertTrue(result["pendingAdminActivation"])
        self.assertFalse(result["activated"])
        self.assertEqual(result["release"], str(self.old))
        self.assertIn(" -I ", result["activationCommand"])
        self.assertTrue(result["activationCommand"].endswith(" publish"))
        destination = self.root / "incoming/releases" / target.name
        remote.verify_release(destination, entries)
        request = json.loads((self.root / "incoming/request.json").read_text())
        self.assertEqual(request, {"schemaVersion": 1, "releaseId": target.name,
                                  "manifestSha256": remote.digest(target / "file-manifest.json"), "expectedCurrentRelease": self.old.name})
        self.assertEqual(before, self.protected_snapshot())
        self.assertFalse((self.root / "shared").exists())
        self.assertFalse((self.root / "incoming/.site-sync.lock").exists())
        self.assertLess(archive.stat().st_size, 20000)

    def test_compare_and_swap_and_lock_refuse_before_replacing_request(self):
        target, entries, plan, archive = self.plan()
        pending = self.root / "incoming/request.json"
        pending.write_text("prior pending request")
        plan["baseRelease"] += "-stale"
        with self.assertRaisesRegex(RuntimeError, "changed since planning"):
            self.site.stage(plan, archive)
        self.assertEqual(pending.read_text(), "prior pending request")
        lock = self.root / "incoming/.site-sync.lock"
        lock.mkdir()
        with self.assertRaisesRegex(RuntimeError, "holds the incoming lock"):
            self.site.stage(plan, archive)
        self.assertTrue(lock.exists())

    def test_symlink_or_nonprivate_incoming_cannot_redirect_writes(self):
        self.root.joinpath("incoming/releases").rmdir()
        self.root.joinpath("incoming/releases").symlink_to(self.base)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.site.incoming()
        self.root.joinpath("incoming/releases").unlink()
        self.root.joinpath("incoming/releases").mkdir(mode=0o700)
        self.root.joinpath("incoming").chmod(0o755)
        with self.assertRaisesRegex(ValueError, "0700"):
            self.site.incoming()

    def test_corrupt_transfer_leaves_active_files_and_pending_request_intact(self):
        target, entries, plan, archive = self.plan()
        before = self.protected_snapshot()
        plan["archiveSha256"] = "0" * 64
        with archive.open("rb") as stream, self.assertRaisesRegex(ValueError, "digest"):
            isolated.dispatch(self.profile, plan, stream, remote)
        self.assertEqual(before, self.protected_snapshot())
        self.assertFalse((self.root / "incoming/request.json").exists())
        self.assertEqual(list((self.root / "incoming").glob(".site-transfer-*")), [])

    def test_readonly_check_verifies_manifest_and_public_minimal_health_only(self):
        before = self.protected_snapshot()
        health = {"service": remote.SERVICE, "readOnly": True, "status": "ok"}
        with mock.patch.object(remote, "fetch_health", return_value=health) as fetch:
            result = self.site.check()
        self.assertEqual(result["revisions"], {"fixture/main": "v1"})
        self.assertEqual(result["health"]["internal"], "administrator-verification-required")
        self.assertEqual(fetch.call_args.args[0], self.profile["publicUrl"])
        self.assertEqual(fetch.call_args.kwargs, {"minimal": True})
        self.assertEqual(before, self.protected_snapshot())
        (self.old / "release.json").write_bytes(b"corrupted")
        with self.assertRaises(ValueError):
            self.site.check()

    def test_root_ownership_and_public_state_identity_are_enforced(self):
        with mock.patch.object(isolated, "SYSTEM_OWNER", os.getuid() + 1):
            with self.assertRaisesRegex(ValueError, "root-owned"):
                isolated.IsolatedSite(self.profile, remote)
        changed = dict(self.public_state, incomingRoot=str(self.base / "elsewhere"))
        (self.root / "public-state.json").write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, "committed"):
            self.site.inspect()

    def test_rollback_cannot_misuse_isolation_migration_rollback(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("No privilege invocation")):
            with self.assertRaisesRegex(ValueError, "unsupported.*original service accounts"):
                isolated.dispatch(self.profile, {"action": "rollback"}, io.BytesIO(), remote)

    def test_main_dispatch_never_enters_legacy_launchagent_mode(self):
        body = json.dumps({"action": "inspect", "profile": self.profile}).encode() + b"\n"
        result = io.StringIO()
        with mock.patch.object(remote.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(body))), redirect_stdout(result), \
                mock.patch.object(remote, "LaunchdService", side_effect=AssertionError("Retired LaunchAgent must never be read")):
            remote.main()
        response = json.loads(result.getvalue())
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["publisherMode"], "isolated-manual")

    def test_profile_uses_external_helper_and_does_not_require_retired_launchagent(self):
        loaded = sync.load_profile(self.profile_path)
        self.assertEqual(loaded["publisherMode"], "isolated-manual")
        wire = sync._remote_profile(loaded)
        self.assertNotIn("launchdLabel", wire)
        self.assertEqual(wire["isolatedPublishHelper"], self.profile["isolatedPublishHelper"])
        for field, value in (("publisherMode", "sudo-auto"), ("isolatedPublishHelper", "relative.py"),
                             ("isolatedPublishHelper", "/unrelated/helper.py")):
            self.profile_path.write_text(json.dumps(dict(self.profile, **{field: value})))
            with self.assertRaises(ValueError):
                sync.load_profile(self.profile_path)
