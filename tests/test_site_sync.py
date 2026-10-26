"""Exercise a real incremental filesystem transaction without connecting to a host."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import plistlib
import tarfile
import tempfile
from unittest import TestCase, mock

from choir_assistant.deployment import remote, sync


def release_at(path, revision="v1", extra=None):
    path.mkdir(parents=True)
    files = {"site_server.py": b"# standalone runtime\n", "assets/shared.bin": b"unchanged large fixture" * 10000,
             "release.json": json.dumps({"schemaVersion": 1, "works": {"fixture": {"movements": {
                 "main": {"revision": revision}}}}}, sort_keys=True).encode()}
    files.update(extra or {})
    for name, value in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    entries = [{"path": name, "sha256": remote.digest(path / name), "bytes": len(value)} for name, value in sorted(files.items())]
    manifest = path / "file-manifest.json"
    manifest.write_text(json.dumps({"schemaVersion": 1, "files": entries}, sort_keys=True))
    entries.append({"path": manifest.name, "sha256": remote.digest(manifest), "bytes": manifest.stat().st_size})
    return entries


class FakeService(remote.LaunchdService):
    def __init__(self, profile, plist):
        self.profile = profile
        self.root = Path(profile["remoteRoot"])
        self.label = profile["launchdLabel"]
        self.plist = plist
        self.fail_revision = None
        self.restarts = []

    def restart(self, data):
        self.restarts.append(data)
        self.plist.write_bytes(data)

    def health(self, expected, public=True):
        active = self.owned()["release"]
        actual = remote.revisions(json.loads((active / "release.json").read_text()))
        if actual != expected or self.fail_revision in actual.values():
            raise RuntimeError("simulated health failure")
        return {"local": "passed", "public": "passed"}


class SiteSyncTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "server"
        (self.root / "shared").mkdir(parents=True)
        (self.root / "releases").mkdir()
        self.old = self.root / "releases/old"
        self.old_entries = release_at(self.old)
        self.profile = {"schemaVersion": 1, "libraryRoot": str(self.base / "library"),
                        "outputRoot": str(self.base / "exports"), "sshHost": "fixture-server",
                        "remoteRoot": str(self.root), "remotePython": "/runtime/python",
                        "launchdLabel": "org.example.choir", "publicUrl": "https://choir.example.org",
                        "bind": "127.0.0.1", "port": 8780, "healthTimeoutSeconds": 5}
        self.profile_path = self.base / "profile.json"
        self.profile_path.write_text(json.dumps(self.profile))
        self.plist = self.root / "agent.plist"
        self.agent = {"Label": self.profile["launchdLabel"], "EnvironmentVariables": {"CUSTOM": "preserved"},
                      "ProgramArguments": ["/usr/bin/caffeinate", "-is", "/runtime/python",
                                           str(self.old / "site_server.py"), "--root", str(self.old),
                                           "--runtime", str(self.root / "shared/runtime"), "--bind", "127.0.0.1",
                                           "--port", "8780", "--soundfont", str(self.old / "font.sf2")],
                      "WorkingDirectory": str(self.old), "KeepAlive": True}
        self.plist.write_bytes(plistlib.dumps(self.agent))
        self.service = FakeService(self.profile, self.plist)

    def planned(self, revision="v2", extra=None):
        release = self.base / "local-release"
        entries = release_at(release, revision, extra)
        identifier = "release-" + remote.digest(release / "file-manifest.json")[:20]
        renamed = release.with_name(identifier)
        release.rename(renamed)
        state = remote.inspect(self.profile, self.service)
        expected = remote.revisions(json.loads((renamed / "release.json").read_text()))
        plan = sync._plan(renamed, entries, expected, state)
        archive = self.base / "transfer.tar"
        sync._archive(renamed, plan["changed"], archive)
        return renamed, entries, plan, archive

    def test_incremental_publish_reuses_large_file_and_preserves_agent_environment(self):
        release, entries, plan, archive = self.planned()
        self.assertEqual(plan["changed"], ["file-manifest.json", "release.json"])
        self.assertLess(archive.stat().st_size, 20000)
        shared_before = (self.old / "assets/shared.bin").stat()
        result = remote.apply_release(self.profile, plan, archive, self.service)
        deployed = Path(result["release"])
        self.assertEqual((self.root / "current").resolve(), deployed)
        remote.verify_release(deployed, entries)
        self.assertEqual(result["reusedFiles"], 2)
        self.assertLess(result["transferBytes"], 1000)
        # A writable previous release's modes and bytes stay untouched.
        self.assertEqual((self.old / "assets/shared.bin").stat().st_mode, shared_before.st_mode)
        active = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(active["EnvironmentVariables"], self.agent["EnvironmentVariables"])
        self.assertEqual(active["ProgramArguments"][-1], self.agent["ProgramArguments"][-1])
        self.assertIn(str(deployed), active["ProgramArguments"])

    def test_failed_new_health_restores_old_plist_and_missing_current(self):
        release, entries, plan, archive = self.planned()
        self.service.fail_revision = "v2"
        previous = self.plist.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "previous release restored and verified"):
            remote.apply_release(self.profile, plan, archive, self.service)
        self.assertEqual(self.plist.read_bytes(), previous)
        self.assertFalse((self.root / "current").is_symlink())
        self.assertEqual(len(self.service.restarts), 2)
        self.assertFalse((self.root / "shared/.site-sync.lock").exists())
        self.assertEqual(remote.revisions(json.loads((self.old / "release.json").read_text())), {"fixture/main": "v1"})

    def test_failed_health_restores_existing_current_symlink(self):
        (self.root / "current").symlink_to("releases/old")
        release, entries, plan, archive = self.planned()
        self.service.fail_revision = "v2"
        with self.assertRaises(RuntimeError):
            remote.apply_release(self.profile, plan, archive, self.service)
        self.assertEqual((self.root / "current").readlink(), Path("releases/old"))

    def test_explicit_rollback_restores_old_service_and_current(self):
        release, entries, plan, archive = self.planned()
        remote.apply_release(self.profile, plan, archive, self.service)
        result = remote.rollback(self.profile, self.service)
        self.assertTrue(result["rolledBack"])
        self.assertEqual(self.service.owned()["release"], self.old)
        with self.assertRaisesRegex(ValueError, "no longer describes"):
            remote.rollback(self.profile, self.service)

    def test_rollback_rejects_changed_retained_release_before_restarting(self):
        release, entries, plan, archive = self.planned()
        remote.apply_release(self.profile, plan, archive, self.service)
        (self.old / "assets/shared.bin").write_bytes(b"unexpected change")
        before = len(self.service.restarts)
        with self.assertRaisesRegex(ValueError, "SHA-256 changed"):
            remote.rollback(self.profile, self.service)
        self.assertEqual(len(self.service.restarts), before)

    def test_repeat_sync_no_change_does_not_tar_or_write_remote(self):
        release, entries, plan, archive = self.planned()
        remote.apply_release(self.profile, plan, archive, self.service)
        state = remote.inspect(self.profile, self.service)
        with mock.patch.object(sync, "_export", return_value=(release, entries, plan["revisions"], {})), \
                mock.patch.object(sync, "_call", side_effect=[state, {"health": "passed", "release": state["release"],
                    "revisions": plan["revisions"]}]) as call, \
                mock.patch.object(sync, "_archive") as make_tar:
            result = sync.sync_site(self.profile_path)
        self.assertFalse(result["changed"])
        self.assertEqual(result["transferBytes"], 0)
        self.assertEqual([item.args[1] for item in call.call_args_list], ["inspect", "check"])
        make_tar.assert_not_called()

    def test_concurrent_release_switch_during_noop_cannot_report_desired_success(self):
        release, entries, plan, archive = self.planned()
        remote.apply_release(self.profile, plan, archive, self.service)
        state = remote.inspect(self.profile, self.service)
        raced = {"health": "passed", "release": str(self.old), "revisions": {"fixture/main": "v1"}}
        with mock.patch.object(sync, "_export", return_value=(release, entries, plan["revisions"], {})), \
                mock.patch.object(sync, "_call", side_effect=[state, raced]):
            with self.assertRaisesRegex(RuntimeError, "changed during the no-op"):
                sync.sync_site(self.profile_path)

    def test_check_only_does_not_call_mutating_remote_actions(self):
        release, entries, plan, archive = self.planned()
        state = remote.inspect(self.profile, self.service)
        with mock.patch.object(sync, "_export", return_value=(release, entries, plan["revisions"], {})), \
                mock.patch.object(sync, "_call", return_value=state) as call:
            result = sync.sync_site(self.profile_path, check_only=True)
        self.assertTrue(result["checkOnly"])
        self.assertEqual(result["changedFiles"], 2)
        self.assertEqual([item.args[1] for item in call.call_args_list], ["inspect"])

    def test_online_editor_heads_block_stale_export_before_transfer(self):
        release, entries, plan, archive = self.planned()
        state = remote.inspect(self.profile, self.service)
        state["editorHeads"] = {"fixture/main": "a" * 64}
        with mock.patch.object(sync, "_export", return_value=(release, entries, plan["revisions"], {})), \
                mock.patch.object(sync, "_call", return_value=state) as call:
            with self.assertRaisesRegex(RuntimeError, "pull and review"):
                sync.sync_site(self.profile_path)
        self.assertEqual([item.args[1] for item in call.call_args_list], ["inspect"])

    def test_explicit_readiness_is_checked_before_export_or_network(self):
        value = dict(self.profile, requireReady=True)
        self.profile_path.write_text(json.dumps(value))
        with mock.patch.object(sync, "_call") as call, mock.patch.object(sync, "_export") as export:
            with self.assertRaisesRegex(ValueError, "requires --ready"):
                sync.sync_site(self.profile_path)
            with self.assertRaises(ValueError):
                sync.check_site(self.profile_path)
        call.assert_not_called()
        export.assert_not_called()

    def test_concurrent_deploy_does_not_remove_another_lock(self):
        release, entries, plan, archive = self.planned()
        lock = self.root / "shared/.site-sync.lock"
        lock.mkdir()
        with self.assertRaisesRegex(RuntimeError, "holds the project lock"):
            remote.apply_release(self.profile, plan, archive, self.service)
        self.assertTrue(lock.exists())

    def test_compare_and_swap_rejects_changed_current(self):
        release, entries, plan, archive = self.planned()
        plan["baseRelease"] += "-stale"
        with self.assertRaisesRegex(RuntimeError, "changed since planning"):
            remote.apply_release(self.profile, plan, archive, self.service)
        self.assertEqual(self.service.restarts, [])

    def test_tampered_reused_file_is_not_promoted(self):
        release, entries, plan, archive = self.planned()
        (self.old / "assets/shared.bin").write_bytes(b"changed after planning")
        with self.assertRaisesRegex(RuntimeError, "content changed since planning"):
            remote.apply_release(self.profile, plan, archive, self.service)

    def test_exact_file_set_rejects_extra_and_symlink(self):
        release, entries, plan, archive = self.planned()
        (release / "unindexed.txt").write_text("extra")
        with self.assertRaisesRegex(ValueError, "exact file set"):
            remote.verify_release(release, entries)
        (release / "unindexed.txt").unlink()
        (release / "escape").symlink_to(self.old)
        with self.assertRaisesRegex(ValueError, "symlink"):
            remote.verify_release(release, entries)

    def test_tar_rejects_path_traversal_hardlink_and_unexpected_member(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("release.json", tarfile.LNKTYPE), ("unexpected", tarfile.REGTYPE)):
            with self.subTest(name=name, kind=kind):
                output = self.base / ("stage-" + str(len(list(self.base.glob("stage-*")))))
                output.mkdir()
                archive = self.base / "unsafe.tar"
                data = b"{}"
                with tarfile.open(archive, "w") as stream:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.size = len(data) if kind == tarfile.REGTYPE else 0
                    member.linkname = "outside"
                    stream.addfile(member, io.BytesIO(data) if member.size else None)
                with self.assertRaises(ValueError):
                    remote.unpack_changed(archive, output, {"release.json"}, {"release.json": {"bytes": 2}})

    def test_tar_rejects_duplicate_file_even_when_hash_matches(self):
        output = self.base / "stage"
        output.mkdir()
        archive = self.base / "duplicate.tar"
        with tarfile.open(archive, "w") as stream:
            for unused in range(2):
                member = tarfile.TarInfo("release.json")
                member.size = 2
                stream.addfile(member, io.BytesIO(b"{}"))
        entry = {"bytes": 2, "sha256": hashlib.sha256(b"{}").hexdigest()}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            remote.unpack_changed(archive, output, {"release.json"}, {"release.json": entry})

    def test_unrelated_launchagent_is_rejected(self):
        agent = deepcopy(self.agent)
        agent["ProgramArguments"][3] = "/unrelated/site_server.py"
        self.plist.write_bytes(plistlib.dumps(agent))
        with self.assertRaises(ValueError):
            self.service.owned()

    def test_profile_rejects_shell_ssh_alias_nonlocal_bind_and_credentials(self):
        for field, value in (("sshHost", "-oProxyCommand=bad"), ("sshHost", "user@host"),
                             ("bind", "0.0.0.0"), ("publicUrl", "https://user:secret@example.org"),
                             ("remoteRoot", "/tmp/../other")):
            with self.subTest(field=field, value=value):
                self.profile_path.write_text(json.dumps(dict(self.profile, **{field: value})))
                with self.assertRaises(ValueError):
                    sync.load_profile(self.profile_path)

    def test_profile_rejects_library_or_export_inside_plugin(self):
        plugin = Path(sync.__file__).resolve().parents[2]
        for field in ("libraryRoot", "outputRoot"):
            self.profile_path.write_text(json.dumps(dict(self.profile, **{field: str(plugin / "private")})))
            with self.assertRaisesRegex(ValueError, "outside the installed plugin"):
                sync.load_profile(self.profile_path)

    def test_health_requires_every_revision_and_synthesis(self):
        health = {"service": remote.SERVICE, "readOnly": True, "synthesisAvailable": True,
                  "loadedWorks": 1, "works": [{"id": "fixture", "loaded": True, "movements": [
                      {"id": "main", "loaded": True, "revision": "v1"}]}]}
        self.assertEqual(remote.verify_health(health, {"fixture/main": "v1"}), health)
        for change in ({"synthesisAvailable": False}, {"loadedWorks": 2}, {"works": []}):
            with self.assertRaises(ValueError):
                remote.verify_health(dict(health, **change), {"fixture/main": "v1"})

    def test_http_probe_is_self_identifying_and_forbids_redirects(self):
        with mock.patch("urllib.request.build_opener") as build:
            response = build.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = json.dumps({"service": remote.SERVICE, "readOnly": True,
                "synthesisAvailable": True, "loadedWorks": 1, "works": [{"id": "fixture", "loaded": True,
                "movements": [{"id": "main", "loaded": True, "revision": "v1"}]}]}).encode()
            remote.fetch_health("https://choir.example.org", {"fixture/main": "v1"})
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), remote.USER_AGENT)
        self.assertIsNone(build.call_args.args[0].redirect_request(None, None, 302, "", {}, "http://other"))
