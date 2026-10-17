"""Data-only automatic publishing contract; no SSH or privileged worker is used."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from choir_assistant import cli
from choir_assistant.deployment import isolated, remote, sync
import test_site_isolated as manual


class AutomaticPublishingTests(TestCase):
    def setUp(self):
        manual.IsolatedPublishingTests.setUp(self)
        self.profile["publisherMode"] = "isolated-auto"
        self.profile.pop("isolatedPublishHelper")
        self.profile_path.write_text(json.dumps(self.profile))
        self.public_state.update(publisherMode="isolated-auto", publisher={"schemaVersion": 1, "generation": 7})
        (self.root / "public-state.json").write_text(json.dumps(self.public_state))
        self.site = isolated.IsolatedSite(self.profile, remote)
        self.status = {"schemaVersion": 1, "publisherMode": "isolated-auto", "status": "idle", "generation": 7,
                       "currentRelease": self.old.name, "manifestSha256": self.public_state["manifestSha256"],
                       "requestId": None, "releaseId": None, "previousRelease": None,
                       "changed": False, "errorCode": None, "recent": []}
        self.write_status()

    plan = manual.IsolatedPublishingTests.plan
    protected_snapshot = manual.IsolatedPublishingTests.protected_snapshot

    def write_status(self):
        (self.root / "publisher-status.json").write_text(json.dumps(self.status))

    def stage(self):
        target, entries, plan, archive = self.plan()
        result = self.site.stage(plan, archive)
        request = json.loads((self.root / "incoming/request.json").read_text())
        return target, entries, plan, archive, result, request

    def terminal_failure(self, request):
        request_hash = hashlib.sha256((self.root / "incoming/request.json").read_bytes()).hexdigest()
        self.status.update(requestId=request["requestId"], releaseId=request["releaseId"], previousRelease=self.old.name,
                           status="failed", errorCode="validation-failed", recent=[{
                               "requestId": request["requestId"], "requestSha256": request_hash, "status": "failed",
                               "releaseId": request["releaseId"], "generation": 7, "changed": False,
                               "errorCode": "validation-failed"}])
        self.write_status()

    def test_auto_profile_does_not_need_or_send_any_root_helper(self):
        profile = sync.load_profile(self.profile_path)
        wire = sync._remote_profile(profile)
        self.assertEqual(wire["publisherMode"], "isolated-auto")
        self.assertNotIn("isolatedPublishHelper", wire)
        self.assertNotIn("isolatedAdminPython", wire)
        self.assertNotIn("launchdLabel", wire)

    def test_schema2_stage_only_changes_private_incoming_and_never_runs_a_process(self):
        before = self.protected_snapshot()
        with mock.patch("subprocess.run", side_effect=AssertionError("No sudo or service control")):
            target, entries, plan, archive, result, request = self.stage()
        self.assertEqual(set(request), {"schemaVersion", "requestId", "releaseId", "manifestSha256",
                                       "expectedCurrentRelease", "expectedGeneration"})
        self.assertEqual(request["schemaVersion"], 2)
        self.assertEqual(request["expectedGeneration"], 7)
        self.assertEqual(request["expectedCurrentRelease"], self.old.name)
        self.assertRegex(request["requestId"], r"^[0-9a-f]{32}$")
        self.assertTrue(result["pendingActivation"])
        self.assertFalse(result["activated"])
        self.assertNotIn("activationCommand", result)
        self.assertEqual(before, self.protected_snapshot())
        self.assertLess(result["transferBytes"], 1000)
        remote.verify_release(self.root / "incoming/releases" / target.name, entries)

    def test_same_unresolved_target_reuses_request_id_without_unpacking_or_rewriting(self):
        target, entries, plan, archive, result, request = self.stage()
        path = self.root / "incoming/request.json"
        old = path.read_bytes()
        self.status.update(status="publishing", requestId=request["requestId"], releaseId=target.name,
                           previousRelease=self.old.name)
        self.write_status()
        with mock.patch.object(remote, "unpack_changed", side_effect=AssertionError("Do not restage")):
            repeated = self.site.stage(plan, archive)
        self.assertTrue(repeated["reusedPendingRequest"])
        self.assertEqual(repeated["requestId"], result["requestId"])
        self.assertEqual(path.read_bytes(), old)

    def test_different_unresolved_target_cannot_replace_request(self):
        target, entries, plan, archive, result, request = self.stage()
        plan = dict(plan, releaseId="release-" + "a" * 20, files=[dict(entry,
                    sha256="a" * 64) if entry["path"] == "file-manifest.json" else entry for entry in entries])
        old = (self.root / "incoming/request.json").read_bytes()
        with self.assertRaisesRegex(RuntimeError, "unresolved publisher request"):
            self.site.stage(plan, archive)
        self.assertEqual((self.root / "incoming/request.json").read_bytes(), old)

    def test_matching_short_release_id_does_not_replace_a_different_full_manifest(self):
        target, entries, plan, archive, result, request = self.stage()
        full_hash = request["manifestSha256"]
        changed_hash = full_hash[:20] + ("a" if full_hash[20] != "a" else "b") + full_hash[21:]
        plan["files"] = [dict(entry, sha256=changed_hash) if entry["path"] == "file-manifest.json" else entry
                         for entry in entries]
        with self.assertRaisesRegex(RuntimeError, "unresolved publisher request"):
            self.site.stage(plan, archive)

    def test_generation_compare_and_swap_refuses_before_writing_request(self):
        target, entries, plan, archive = self.plan()
        plan["expectedGeneration"] = 6
        with self.assertRaisesRegex(RuntimeError, "generation changed"):
            self.site.stage(plan, archive)
        self.assertFalse((self.root / "incoming/request.json").exists())

    def test_failed_request_is_terminal_until_explicit_retry_uses_new_uuid(self):
        target, entries, plan, archive, result, request = self.stage()
        self.terminal_failure(request)
        old = (self.root / "incoming/request.json").read_bytes()
        with self.assertRaisesRegex(RuntimeError, "--retry-failed"):
            self.site.stage(plan, archive)
        self.assertEqual((self.root / "incoming/request.json").read_bytes(), old)
        repeated = self.site.stage(dict(plan, retryFailed=True), archive)
        self.assertNotEqual(repeated["requestId"], request["requestId"])
        self.assertTrue(repeated["pendingActivation"])

    def test_tampered_terminal_request_cannot_be_mistaken_for_finished_receipt(self):
        target, entries, plan, archive, result, request = self.stage()
        self.terminal_failure(request)
        path = self.root / "incoming/request.json"
        path.write_text(path.read_text() + " ")
        with self.assertRaisesRegex(RuntimeError, "differs from its terminal"):
            self.site.stage(dict(plan, retryFailed=True), archive)

    def test_lock_is_not_removed_or_stolen_from_another_staging_operation(self):
        target, entries, plan, archive = self.plan()
        lock = self.root / "incoming/.site-sync.lock"
        lock.mkdir()
        with self.assertRaisesRegex(RuntimeError, "holds the incoming lock"):
            self.site.stage(plan, archive)
        self.assertTrue(lock.exists())

    def test_public_state_generation_and_request_schema_are_strict(self):
        for generation in (True, -1, "7"):
            changed = dict(self.public_state, publisher={"schemaVersion": 1, "generation": generation})
            (self.root / "public-state.json").write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, "generation"):
                self.site.state()
        (self.root / "public-state.json").write_text(json.dumps(self.public_state))
        request = {"schemaVersion": 2, "requestId": "a" * 32, "releaseId": "release-" + "b" * 20,
                   "manifestSha256": "b" * 64, "expectedCurrentRelease": self.old.name, "expectedGeneration": 7}
        for changed in (dict(request, command="bad"), dict(request, expectedGeneration=True),
                        dict(request, requestId="../bad"), dict(request, manifestSha256="c" * 64)):
            with self.assertRaises(ValueError):
                isolated.validate_auto_request(changed)

    def test_status_reader_bounds_bytes_and_rejects_links_and_unknown_error_codes(self):
        path = self.root / "publisher-status.json"
        path.write_bytes(b"x" * 16385)
        with self.assertRaisesRegex(ValueError, "too large"):
            self.site.status()
        path.unlink()
        path.symlink_to(self.root / "public-state.json")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.site.status()
        path.unlink()
        self.status.update(status="blocked", errorCode="arbitrary-private-error-message")
        self.write_status()
        with self.assertRaisesRegex(ValueError, "publisher status"):
            self.site.status()

    def test_noop_sync_sends_generation_guard_without_tar_or_write(self):
        state = self.site.inspect()
        expected = state["revisions"]
        checked = {"release": state["release"], "revisions": expected, "health": {"public": "passed"}}
        with mock.patch.object(sync, "_export", return_value=(self.old, self.old_entries, expected, {})), \
                mock.patch.object(sync, "_call", side_effect=[state, checked]) as call, \
                mock.patch.object(sync, "_archive", side_effect=AssertionError("No transfer")):
            result = sync.sync_site(self.profile_path)
        self.assertFalse(result["changed"])
        self.assertEqual(call.call_args_list[-1].args[2]["expectedGeneration"], 7)

    def test_pending_sync_reuses_existing_request_without_transferring_again(self):
        target, entries, plan, archive, result, request = self.stage()
        state = self.site.inspect()
        with mock.patch.object(sync, "_export", return_value=(target, entries, plan["revisions"], {})), \
                mock.patch.object(sync, "_call", return_value=state) as call, \
                mock.patch.object(sync, "_wait_for_publisher", side_effect=lambda p, r, e: r), \
                mock.patch.object(sync, "_archive", side_effect=AssertionError("No repeat transfer")):
            result = sync.sync_site(self.profile_path)
        self.assertTrue(result["reusedPendingRequest"])
        self.assertEqual(result["requestId"], request["requestId"])
        self.assertEqual([entry.args[1] for entry in call.call_args_list], ["inspect"])

    def test_poll_success_requires_matching_target_and_actual_public_check(self):
        target, entries, plan, archive, result, request = self.stage()
        success = dict(self.status, status="succeeded", requestId=request["requestId"], releaseId=target.name,
                       previousRelease=self.old.name, changed=True, generation=8,
                       currentRelease=target.name, manifestSha256=request["manifestSha256"], recent=[{
                           "requestId": request["requestId"], "requestSha256": result["requestSha256"],
                           "releaseId": target.name, "status": "succeeded", "generation": 8,
                           "changed": True, "errorCode": None}])
        checked = {"release": str(self.root / "releases" / target.name), "health": {"public": "passed"}}
        profile = sync.load_profile(self.profile_path)
        with mock.patch.object(sync, "_call", side_effect=[{"publisherStatus": success}, checked]) as call:
            completed = sync._wait_for_publisher(profile, result, plan["revisions"])
        self.assertTrue(completed["activated"])
        self.assertFalse(completed["pendingActivation"])
        self.assertEqual([entry.args[1] for entry in call.call_args_list], ["status", "check"])
        self.assertEqual(call.call_args_list[-1].args[2]["expectedGeneration"], 8)

    def test_pending_poll_deadline_does_not_claim_activation(self):
        target, entries, plan, archive, result, request = self.stage()
        with mock.patch.object(sync, "_call", return_value={"publisherStatus": self.status}) as call, \
                mock.patch.object(sync.time, "monotonic", side_effect=[0, 0, 100]):
            pending = sync._wait_for_publisher(sync.load_profile(self.profile_path), result, plan["revisions"])
        self.assertFalse(pending["activated"])
        self.assertTrue(pending["pendingActivation"])
        self.assertTrue(pending["pollTimedOut"])
        self.assertLessEqual(call.call_args.kwargs["timeout"], 90)

    def test_terminal_failure_is_not_retried_and_cli_returns_failure(self):
        target, entries, plan, archive, result, request = self.stage()
        self.terminal_failure(request)
        with mock.patch.object(sync, "_call", return_value={"publisherStatus": self.status}) as call:
            failed = sync._wait_for_publisher(sync.load_profile(self.profile_path), result, plan["revisions"])
        self.assertTrue(failed["publicationFailed"])
        self.assertFalse(failed["activated"])
        self.assertFalse(failed["pendingActivation"])
        self.assertEqual(len(call.call_args_list), 1)
        with mock.patch.object(cli, "deployment_command", return_value=failed), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli.main(["site", "sync", "--profile", str(self.profile_path), "--retry-failed"])
        self.assertEqual(code, 2)

    def test_recent_terminal_receipt_is_used_even_when_latest_status_has_another_id(self):
        target, entries, plan, archive, result, request = self.stage()
        self.terminal_failure(request)
        self.status.update(requestId="b" * 32, releaseId="release-" + "c" * 20,
                           status="publishing", errorCode=None)
        self.write_status()
        with self.assertRaisesRegex(RuntimeError, "--retry-failed"):
            self.site.stage(plan, archive)
        with mock.patch.object(sync, "_call", return_value={"publisherStatus": self.status}):
            completed = sync._wait_for_publisher(sync.load_profile(self.profile_path), result, plan["revisions"])
        self.assertTrue(completed["publicationFailed"])
        self.assertEqual(completed["requestReceipt"]["requestId"], request["requestId"])

    def test_root_capacity_failure_is_reported_but_cannot_authorize_request_replacement(self):
        target, entries, plan, archive, result, request = self.stage()
        self.status.update(requestId=request["requestId"], releaseId=target.name, previousRelease=self.old.name,
                           status="blocked", errorCode="capacity-exceeded")
        self.write_status()
        with mock.patch.object(sync, "_call", return_value={"publisherStatus": self.status}):
            completed = sync._wait_for_publisher(sync.load_profile(self.profile_path), result, plan["revisions"])
        self.assertTrue(completed["publicationFailed"])
        self.assertTrue(completed["terminalReceiptMissing"])
        self.assertEqual(completed["errorCode"], "capacity-exceeded")
        with self.assertRaisesRegex(RuntimeError, "lacks its immutable"):
            self.site.stage(dict(plan, retryFailed=True), archive)

    def test_poll_success_for_another_current_release_cannot_claim_activation(self):
        target, entries, plan, archive, result, request = self.stage()
        recent = {"requestId": request["requestId"], "requestSha256": result["requestSha256"],
                  "releaseId": target.name, "status": "succeeded", "generation": 8,
                  "changed": True, "errorCode": None}
        status = dict(self.status, requestId=request["requestId"], releaseId=target.name,
                      previousRelease=self.old.name, status="succeeded", recent=[recent])
        with mock.patch.object(sync, "_call", return_value={"publisherStatus": status}) as call:
            with self.assertRaisesRegex(RuntimeError, "another target"):
                sync._wait_for_publisher(sync.load_profile(self.profile_path), result, plan["revisions"])
        self.assertEqual([entry.args[1] for entry in call.call_args_list], ["status"])

    def test_auto_receiver_dispatch_never_constructs_legacy_user_service(self):
        body = json.dumps({"action": "status", "profile": self.profile}).encode() + b"\n"
        output = io.StringIO()
        with mock.patch.object(remote.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(body))), redirect_stdout(output), \
                mock.patch.object(remote, "LaunchdService", side_effect=AssertionError("No legacy service")):
            remote.main()
        self.assertTrue(json.loads(output.getvalue())["ok"])
