"""Durable editing transactions with an injected builder, independent of engraving."""
from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError

from choir_assistant.deployment import editor, site_server
import test_site_export as fixtures
import test_site_auth_feedback as http_fixtures


def fake_build(base, output, patch, **options):
    """Test only persistence/CAS with a visible model change, not musical correctness."""
    reason = options.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError("Provide a nonempty edit reason of at most 2000 characters")
    shutil.copytree(base, output)
    for path in output.rglob("*"):
        if path.is_file():
            path.chmod(0o640)
    model = json.loads((output / "assets/interactive.json").read_bytes())
    if options.get("stage_callback"):
        options["stage_callback"]({"stage": "engraving", "status": "running", "pages": 1})
    for operation in patch.get("typedOperations", []):
        for note in model["notes"]:
            if note["id"] in operation["noteIds"]:
                note["pitch"] += operation.get("semitones", 0)
    model["revision"] = sha256(editor.encoded(model)).hexdigest()[:16]
    model["pagesInteractive"] = [{"page": 1, "svg": "/assets/notation-page-001.svg"}]
    (output / "assets/interactive.json").write_bytes(editor.encoded(model))
    audio = output / "assets/base.wav"
    samples = bytearray(audio.read_bytes())
    samples[-2:] = int(model["notes"][0]["pitch"]).to_bytes(2, "little")
    audio.write_bytes(samples)
    manifest = json.loads((output / "manifest.json").read_bytes())
    manifest["revision"] = model["revision"]
    manifest["artifacts"] = [{"path": path.relative_to(output).as_posix(), "sha256": editor.digest(path)}
                             for path in sorted(output.rglob("*")) if path.is_file() and path.name != "manifest.json"]
    (output / "manifest.json").write_bytes(editor.encoded(manifest))
    return {"bundle": str(output), "revision": model["revision"], "canPublish": True, "validation": {"newIssues": []}}


def rebased_release(source, destination, client):
    """Emulate ordinary content sync: edited A becomes the new frozen baseline."""
    shutil.copytree(source, destination)
    head = client.head("study", "main")
    release = json.loads((destination / "release.json").read_bytes())
    movement = release["works"]["study"]["movements"]["main"]
    target = destination / movement["bundle"]
    shutil.rmtree(target)
    shutil.copytree(client.bundle(head), target)
    movement["revision"] = head["revision"]
    projection = destination / movement["projection"]
    value = json.loads(projection.read_bytes()); value["revision"] = head["revision"]
    projection.write_bytes(editor.encoded(value))
    (destination / "release.json").write_bytes(editor.encoded(release))
    manifest = {"schemaVersion": 1, "files": [
        {"path": path.relative_to(destination).as_posix(), "bytes": path.stat().st_size, "sha256": editor.digest(path)}
        for path in sorted(destination.rglob("*")) if path.is_file() and path.name != "file-manifest.json"]}
    (destination / "file-manifest.json").write_bytes(editor.encoded(manifest))
    return destination


class EditorStoreTests(unittest.TestCase):
    setUp = fixtures.SiteExportTests.setUp
    refresh_manifest = fixtures.SiteExportTests.refresh_manifest
    register = fixtures.SiteExportTests.register
    exported = fixtures.SiteExportTests.exported

    def prepare(self):
        self.release, _ = self.exported()
        self.edit_root = self.root / "editing"
        (self.edit_root / "inbox").mkdir(parents=True)
        self.client = editor.EditorClient(self.edit_root)
        self.actor = "a" * 64
        self.worker = editor.EditorWorker(self.edit_root, self.release, build=fake_build)
        self.head = editor.baseline_head("study", "main", "fixture-v1")

    def submit(self, action="preview", **kwargs):
        data = {"action": action, "baseModelRevision": "fixture-v1", "basePublishedEditHead": self.head,
                "idempotencyKey": os.urandom(12).hex(), "typedOperations": [{"op": "pitch.delta", "noteIds": ["n1"], "semitones": 1}],
                "reason": "fixture user edit", **kwargs}
        return self.client.submit("study", "main", self.actor, data)

    def preview(self):
        job = self.submit()
        result = self.worker.run_once()
        self.assertEqual(result["id"], job["id"])
        self.assertEqual(result["status"], "ready_to_publish")
        return result

    def publish(self, preview):
        job = self.submit("publish", jobId=preview["id"])
        result = self.worker.run_once()
        self.assertEqual(result["status"], "published")
        return result

    def test_submission_is_durable_idempotent_and_changed_payload_conflicts(self):
        self.prepare()
        first = self.submit(idempotencyKey="fixed-fixture-key")
        again = self.submit(idempotencyKey="fixed-fixture-key")
        self.assertEqual(first["id"], again["id"])
        with self.assertRaises(editor.EditConflict):
            self.submit(idempotencyKey="fixed-fixture-key", reason="different")
        fresh = editor.EditorClient(self.edit_root)
        self.assertEqual(fresh.job("study", "main", self.actor, first["id"])["status"], "queued")

    def test_preview_does_not_change_shared_head_and_publish_preserves_original_release(self):
        self.prepare()
        frozen = {str(path): path.read_bytes() for path in self.release.rglob("*") if path.is_file()}
        preview = self.preview()
        self.assertIsNone(self.client.head("study", "main"))
        self.assertTrue(any(event["stage"] == "engraving" for event in preview["events"]))
        result = self.publish(preview)
        current = self.client.head("study", "main")
        self.assertEqual(current["editHead"], result["publishedHead"])
        self.assertEqual(current["parent"], self.head)
        self.assertEqual(frozen, {str(path): path.read_bytes() for path in self.release.rglob("*") if path.is_file()})
        self.assertEqual(self.client.history("study", "main")[0]["head"], current["editHead"])

    def test_two_previews_from_same_base_cannot_overwrite_each_other(self):
        self.prepare()
        first = self.preview(); second = self.preview()
        published = self.publish(first)
        self.submit("publish", jobId=second["id"])
        refused = self.worker.run_once()
        self.assertEqual(refused["status"], "conflict")
        self.assertEqual(self.client.head("study", "main")["editHead"], published["publishedHead"])

    def test_restore_creates_new_head_and_keeps_history_and_other_scope(self):
        self.prepare()
        first = self.publish(self.preview())
        self.client.submit("study", "main", self.actor, {"action": "restore", "targetHead": self.head,
            "baseModelRevision": first["revision"], "basePublishedEditHead": first["publishedHead"], "idempotencyKey": "restore-original"})
        restored = self.worker.run_once()
        self.assertEqual(restored["status"], "published")
        self.assertEqual(restored["revision"], "fixture-v1")
        self.assertNotEqual(restored["publishedHead"], first["publishedHead"])
        self.assertEqual(len(self.client.history("study", "main")), 3)
        self.assertEqual(self.client.head("study", "main")["restoreOf"], self.head)

    def test_natural_language_requires_confirmation_and_ambiguous_request_is_honest(self):
        self.prepare()
        request = self.submit("request", text="升半音", selectedNoteIds=["n1"])
        proposed = self.worker.run_once()
        self.assertEqual(proposed["status"], "needs_confirmation")
        self.assertIsNone(self.client.head("study", "main"))
        self.submit("confirm", jobId=request["id"])
        self.assertEqual(self.worker.run_once()["status"], "ready_to_publish")
        self.submit("request", text="这里不太对", selectedNoteIds=["n1"])
        self.assertEqual(self.worker.run_once()["status"], "needs_input")

    def test_configured_natural_language_executor_is_called_and_cannot_escape_selection(self):
        self.prepare()
        executor = mock.Mock(return_value={"typedOperations": [{"op": "pitch.delta", "noteIds": ["other"], "semitones": 1}]})
        self.worker.interpreter = executor
        self.submit("request", text="这里不太对", selectedNoteIds=["n1"])
        result = self.worker.run_once()
        self.assertEqual(result["status"], "failed")
        executor.assert_called_once()
        self.assertIsNone(self.client.head("study", "main"))

    def test_expired_worker_lease_recovers_and_commit_point_is_not_republished(self):
        self.prepare()
        queued = self.submit()
        claimed = self.worker.claim()
        another = editor.EditorWorker(self.edit_root, self.release, build=fake_build)
        self.assertIsNone(another.run_once())
        with editor.connection(self.worker.database) as db:
            db.execute("UPDATE jobs SET lease=0 WHERE id=?", (queued["id"],)); db.commit()
        preview = another.run_once()
        self.assertEqual(preview["attempt"], 2)
        self.worker = another
        result = self.publish(preview)
        before = self.client.heads()
        with editor.connection(self.worker.database) as db:
            db.execute("UPDATE jobs SET status='publishing',lease=0 WHERE id=?", (result["id"],)); db.commit()
        recovered = editor.EditorWorker(self.edit_root, self.release, build=fake_build).run_once()
        self.assertEqual(recovered["status"], "published")
        self.assertEqual(self.client.heads(), before)

    def test_lease_takeover_fences_the_old_worker_progress_and_publication(self):
        self.prepare()
        queued = self.submit()
        self.worker.claim()
        with editor.connection(self.worker.database) as db:
            db.execute("UPDATE jobs SET lease=0 WHERE id=?", (queued["id"],)); db.commit()
        replacement = editor.EditorWorker(self.edit_root, self.release, build=fake_build)
        replacement.claim()
        with self.assertRaises(editor.LeaseLost):
            self.worker.update(queued["id"], "published")
        self.assertIsNone(self.client.head("study", "main"))

    def test_jobs_and_history_are_scoped_and_export_preserves_pinned_manifest(self):
        self.prepare()
        preview = self.preview()
        self.assertEqual(self.client.job("study", "main", "b" * 64, preview["id"])["id"], preview["id"])
        with self.assertRaises(KeyError):
            self.client.job("other", "main", self.actor, preview["id"])
        self.publish(preview)
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(self.client.export("study", "main"))) as archive:
            head = json.load(archive.extractfile("head.json"))
            manifest = archive.extractfile("bundle/manifest.json").read()
            self.assertEqual(sha256(manifest).hexdigest(), head["bundleManifestSha256"])
            self.assertIn("ledger.json", archive.getnames())
        from choir_assistant.deployment.editor_sync import unpack_snapshot
        archive_path = self.root / "actual-editor-export.tar"
        archive_path.write_bytes(self.client.export("study", "main", head["editHead"]))
        mirrored = unpack_snapshot(archive_path, self.root / "actual-editor-mirror", "study", "main", head["editHead"], head["sourceSha256"])
        self.assertEqual(mirrored["model"]["revision"], head["revision"])

    def test_local_model_endpoint_is_loopback_only_and_bad_operations_rejected(self):
        for endpoint in ("https://example.com/v1", "http://example.com/v1", "http://user@127.0.0.1/v1", "http://127.0.0.1/other"):
            with self.assertRaises(ValueError):
                editor.LocalModelExecutor(endpoint, "fixture-model")
        editor.LocalModelExecutor("http://127.0.0.1:8080/v1", "fixture-model")
        for op in ({"op": "shell", "noteIds": ["n1"]}, {"op": "pitch.delta", "noteIds": ["n1"], "semitones": True},
                   {"op": "pitch.delta", "noteIds": ["n2"], "semitones": 1}):
            with self.assertRaises(ValueError):
                editor.validate_suggestion({"typedOperations": [op]}, ["n1"])

    def test_local_model_protocol_has_bounded_context_and_requires_real_json(self):
        executor = editor.LocalModelExecutor("http://127.0.0.1:8080/v1", "fixture-model")
        context = {"text": "只把这一处调高一点", "selectedNoteIds": ["n1"], "notes": [{"id": "n1", "pitch": 60}]}
        result = {"typedOperations": [{"op": "pitch.delta", "noteIds": ["n1"], "semitones": 1}], "explanation": "升半音"}
        response = mock.MagicMock(); response.status = 200
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"choices": [{"message": {"content": json.dumps(result)}}]}).encode()
        with mock.patch.object(editor, "build_opener") as opener:
            opener.return_value.open.return_value = response
            proposal = executor(context)
        self.assertEqual(proposal["interpreter"], "configured-local-model")
        call = opener.return_value.open.call_args
        body = json.loads(call.args[0].data)
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_tokens"], 1024)
        self.assertEqual(call.kwargs["timeout"], 120)
        self.assertIn('"n1"', body["messages"][1]["content"])
        self.assertNotIn("/Users/", body["messages"][1]["content"])
        response.read.return_value = b'{"choices":[{"message":{"content":"I cannot return JSON"}}]}'
        with mock.patch.object(editor, "build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(editor.EditorUnavailable):
                executor(context)

    def test_same_work_can_publish_after_session_changes_but_other_scope_cannot(self):
        self.prepare()
        preview = self.preview()
        self.client.submit("study", "main", "b" * 64, {"action": "publish", "jobId": preview["id"],
            "baseModelRevision": "fixture-v1", "basePublishedEditHead": self.head, "idempotencyKey": "second-session-publish"})
        result = self.worker.run_once()
        self.assertEqual(result["status"], "published")
        self.assertEqual(self.client.head("study", "main")["owner"], "b" * 64)
        with self.assertRaises(KeyError):
            self.client.commit("other", "main", result["publishedHead"])

    def test_quota_does_not_delete_history_or_advance_head(self):
        self.prepare()
        self.worker.max_store_bytes = 1
        self.submit()
        result = self.worker.run_once()
        self.assertEqual(result["status"], "blocked_capability")
        self.assertIsNone(self.client.head("study", "main"))
        self.assertFalse(any(self.worker.private.glob("build-*")))

    def test_worker_restart_can_publish_a_previously_completed_preview(self):
        self.prepare()
        preview = self.preview()
        self.worker = editor.EditorWorker(self.edit_root, self.release, build=fake_build)
        result = self.publish(preview)
        self.assertEqual(self.client.head("study", "main")["revision"], result["revision"])

    def test_changed_candidate_cannot_be_published(self):
        self.prepare()
        preview = self.preview()
        candidate = self.client.bundle(preview) / "assets/base.wav"
        candidate.chmod(0o640)
        candidate.write_bytes(b"changed after preview")
        self.submit("publish", jobId=preview["id"])
        result = self.worker.run_once()
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(self.client.head("study", "main"))

    def test_changed_historical_asset_cannot_be_restored(self):
        self.prepare()
        first = self.publish(self.preview())
        self.client.submit("study", "main", self.actor, {"action": "restore", "targetHead": self.head,
            "baseModelRevision": first["revision"], "basePublishedEditHead": first["publishedHead"], "idempotencyKey": "restore-base-first"})
        restored = self.worker.run_once()
        old_bundle = self.client.bundle(self.client.commit("study", "main", first["publishedHead"]))
        audio = old_bundle / "assets/base.wav"
        audio.chmod(0o640); audio.write_bytes(b"historical audio corruption")
        self.client.submit("study", "main", self.actor, {"action": "restore", "targetHead": first["publishedHead"],
            "baseModelRevision": restored["revision"], "basePublishedEditHead": restored["publishedHead"], "idempotencyKey": "restore-corrupted-old"})
        self.assertEqual(self.worker.run_once()["status"], "failed")
        self.assertEqual(self.client.head("study", "main")["editHead"], restored["publishedHead"])

    def test_init_inbox_cli_is_repeatable_and_needs_no_release(self):
        root = self.root / "cli-editor"
        (root / "inbox").mkdir(parents=True)
        output = io.StringIO()
        old_umask = os.umask(0o027)
        try:
            with redirect_stdout(output):
                self.assertEqual(editor.main(["init-inbox", "--root", str(root)]), 0)
                self.assertEqual(editor.main(["init-inbox", "--root", str(root)]), 0)
        finally:
            os.umask(old_umask)
        self.assertEqual((root / "inbox/requests.sqlite3").stat().st_mode & 0o777, 0o660)
        self.assertTrue(all(json.loads(line)["initialized"] for line in output.getvalue().splitlines()))
        with editor.connection(root / "inbox/requests.sqlite3", readonly=True) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_original_baseline_survives_new_release_and_further_edits(self):
        self.prepare()
        first = self.publish(self.preview())
        baseline = self.client.commit("study", "main", self.head)
        original_hash = baseline["bundleManifestSha256"]
        self.worker.release_root = rebased_release(self.release, self.root / "new-release", self.client)
        self.worker.reload_release()
        active, head = self.worker.active("study", "main")
        self.assertEqual(active["model"]["notes"][0]["pitch"], 61)
        self.submit(baseModelRevision=first["revision"], basePublishedEditHead=head)
        second_preview = self.worker.run_once()
        self.submit("publish", jobId=second_preview["id"], baseModelRevision=first["revision"], basePublishedEditHead=head)
        second = self.worker.run_once()
        self.assertEqual(second["status"], "published")
        self.assertEqual(self.client.commit("study", "main", self.head)["bundleManifestSha256"], original_hash)
        history = self.client.history("study", "main")
        self.assertEqual([row["head"] for row in history if row["head"].startswith("baseline-")], [self.head])
        self.assertEqual(history[-1]["revision"], "fixture-v1")
        with self.assertRaises(KeyError):
            self.client.commit("other", "main", self.head)
        with self.assertRaises(KeyError):
            self.client.commit("study", "main", "baseline-" + "0" * 32)

    def test_worker_status_is_bounded_fresh_and_does_not_claim_progress(self):
        self.prepare()
        self.assertEqual(self.client.worker_status()["status"], "unavailable")
        self.assertIsNone(self.worker.run_once())
        status = self.client.worker_status()
        self.assertEqual(status["status"], "idle")
        self.assertTrue(status["ready"])
        self.assertEqual(self.client.worker_status(now=status["lastSeen"] + 31)["status"], "stale")
        self.assertFalse(self.client.worker_status(now=status["lastSeen"] + 31)["ready"])
        self.assertEqual(self.client.worker_status(now=status["lastSeen"] - 60)["status"], "unavailable")
        self.assertEqual(set(editor.read_json(self.client.public / "worker-status.json")), {"schemaVersion", "status", "heartbeat"})
        self.assertEqual((self.client.public / "worker-status.json").stat().st_mode & 0o777, 0o640)
        self.assertLess((self.client.public / "worker-status.json").stat().st_size, 2048)
        editor.atomic_json(self.client.public / "worker-status.json", {"schemaVersion": 1, "status": "arbitrary", "heartbeat": status["lastSeen"]})
        self.assertEqual(self.client.worker_status()["status"], "unavailable")

    def test_worker_preclaim_failure_reports_unavailable_and_preserves_queued_request(self):
        self.prepare()
        queued = self.submit()
        with mock.patch.object(self.worker, "claim", side_effect=sqlite3.OperationalError("fixture-only private diagnostic")):
            with self.assertRaises(sqlite3.OperationalError):
                self.worker.run_once()
        self.assertEqual(self.client.worker_status()["status"], "error")
        self.assertFalse(self.client.worker_status()["ready"])
        self.assertEqual(self.client.job("study", "main", self.actor, queued["id"])["status"], "queued")
        def checked_build(*args, **kwargs):
            self.assertEqual(self.client.worker_status()["status"], "busy")
            self.assertTrue(self.client.worker_status()["ready"])
            return fake_build(*args, **kwargs)
        self.worker.build = checked_build
        self.assertEqual(self.worker.run_once()["id"], queued["id"])
        self.assertEqual(self.client.worker_status()["status"], "idle")

    def test_worker_status_write_failure_does_not_stall_or_duplicate_a_job(self):
        self.prepare()
        queued = self.submit()
        original_atomic = editor.atomic_json
        def diagnostic_failure(path, *args, **kwargs):
            if Path(path).name == "worker-status.json":
                raise PermissionError("fixture diagnostic write denied")
            return original_atomic(path, *args, **kwargs)
        with mock.patch.object(editor, "atomic_json", side_effect=diagnostic_failure):
            result = self.worker.run_once()
        self.assertEqual(result["id"], queued["id"])
        self.assertEqual(result["status"], "ready_to_publish")
        self.assertEqual(self.client.worker_status()["status"], "unavailable")
        self.assertIsNone(self.worker.run_once())

    def test_optional_blank_reason_is_normalized_before_build_audit_and_publication(self):
        self.prepare()
        for optional in ({}, {"reason": ""}, {"reason": "  \n\t  "}):
            request = {"action": "preview", "baseModelRevision": "fixture-v1", "basePublishedEditHead": self.head,
                       "idempotencyKey": os.urandom(12).hex(), "typedOperations": [{"op": "pitch.delta", "noteIds": ["n1"], "semitones": 1}], **optional}
            queued = self.client.submit("study", "main", self.actor, request)
            preview = self.worker.run_once()
            self.assertEqual(preview["status"], "ready_to_publish")
            self.assertEqual(preview["reason"], "用户校正")
            result = json.loads(self.worker._job(queued["id"])["result"])
            self.assertEqual(result["ledger"][-1]["reason"], "用户校正")
            with editor.connection(self.client.queue, readonly=True) as db:
                stored = json.loads(db.execute("SELECT payload FROM requests WHERE id=?", (queued["id"],)).fetchone()[0])
            if "reason" in optional:
                self.assertEqual(stored["reason"], optional["reason"])
            else:
                self.assertNotIn("reason", stored)
        self.submit("publish", jobId=preview["id"], reason="")
        published = self.worker.run_once()
        self.assertEqual(published["status"], "published")
        self.assertEqual(self.client.commit("study", "main", published["publishedHead"])["reason"], "用户校正")

    def test_invalid_optional_reason_is_rejected_before_enqueue(self):
        self.prepare()
        for value in (None, False, 12, [], {}, "字" * 2001, " " * 2001):
            with self.subTest(value_type=type(value).__name__), self.assertRaisesRegex(ValueError, "修改说明"):
                self.submit(reason=value)
        with editor.connection(self.client.queue, readonly=True) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM requests").fetchone()[0], 0)

    def test_long_natural_language_keeps_full_request_with_bounded_audit_reason(self):
        self.prepare()
        self.worker.interpreter = lambda context: {"typedOperations": [{"op": "pitch.delta", "noteIds": ["n1"], "semitones": 1}]}
        text = "这里请升半音。" + "说" * (4000 - len("这里请升半音。"))
        self.assertEqual(len(text), 4000)
        queued = self.submit("request", text=text, selectedNoteIds=["n1"], reason="")
        proposed = self.worker.run_once()
        self.assertEqual(proposed["status"], "needs_confirmation")
        self.assertEqual(proposed["requestText"], text)
        self.assertEqual(len(proposed["proposedPatch"]["reason"]), 2000)
        self.assertTrue(proposed["proposedPatch"]["reason"].endswith("…"))
        self.submit("confirm", jobId=queued["id"], reason="")
        confirmed = self.worker.run_once()
        self.assertEqual(confirmed["status"], "ready_to_publish")
        self.assertEqual(len(confirmed["reason"]), 2000)


class EditorHTTPTests(http_fixtures.SiteAccessHTTPTests):
    def serve_editor(self):
        release = self.serve()
        self.server.shutdown(); self.server.server_close()
        root = self.root / "editing"
        (root / "inbox").mkdir(parents=True)
        config = json.loads(self.config_path.read_bytes())
        config["editor"] = {"enabled": True, "root": str(root)}
        self.config_path.write_text(json.dumps(config))
        self.site = site_server.ScoreSite(release, self.root / "runtime", site_config=self.config_path)
        self.server = site_server.make_server(self.site, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        self.worker = editor.EditorWorker(root, release, build=fake_build)

    def test_publish_sync_then_original_model_audio_and_restore_remain_available(self):
        self.serve_editor()
        with self.login():
            pass
        with self.request("/w/study/main/api/editor") as response:
            original = json.load(response)
        with self.request("/w/study/main/assets/base.wav") as response:
            original_audio = response.read()
        data = {"action": "preview", "baseModelRevision": original["revision"], "basePublishedEditHead": original["publishedHead"],
                "csrfToken": original["csrfToken"], "idempotencyKey": "preview-before-site-sync", "typedOperations": [
                    {"op": "pitch.delta", "noteIds": ["n1"], "semitones": 1}]}
        with self.request("/w/study/main/api/editor", data) as response:
            preview = json.load(response)
        self.assertEqual(self.worker.run_once()["status"], "ready_to_publish")
        with self.request("/w/study/main/api/editor", dict(data, action="publish", jobId=preview["jobId"], idempotencyKey="publish-before-site-sync")):
            pass
        published = self.worker.run_once()
        self.assertEqual(published["status"], "published")
        new_release = rebased_release(self.site.root, self.root / "synced-release", self.worker.client)
        self.server.shutdown(); self.server.server_close()
        self.worker = editor.EditorWorker(self.worker.root, new_release, build=fake_build)
        self.site = site_server.ScoreSite(new_release, self.root / "runtime", site_config=self.config_path)
        self.server = site_server.make_server(self.site, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.addCleanup(self.server.server_close); self.addCleanup(self.server.shutdown)
        with self.request("/w/study/main/assets/interactive.json?editHead=" + original["publishedHead"]) as response:
            old_model = json.load(response)
        self.assertEqual(old_model["revision"], original["revision"])
        self.assertEqual(old_model["notes"][0]["pitch"], 60)
        with self.request("/w/study/main/assets/base.wav?editHead=" + original["publishedHead"]) as response:
            self.assertEqual(response.read(), original_audio)
        with self.request("/w/study/main/assets/base.wav?editHead=" + published["publishedHead"]) as response:
            self.assertNotEqual(response.read(), original_audio)
        with self.request("/w/study/main/api/editor") as response:
            current = json.load(response)
        baselines = [row["head"] for row in current["history"] if row["head"].startswith("baseline-")]
        self.assertEqual(baselines, [original["publishedHead"]])
        self.assertEqual(current["revision"], published["revision"])
        self.assertEqual(current["publishedHead"], published["publishedHead"])
        with self.request("/w/study/main/api/editor", {"action": "restore", "targetHead": original["publishedHead"],
            "baseModelRevision": current["revision"], "basePublishedEditHead": current["publishedHead"],
            "csrfToken": current["csrfToken"], "idempotencyKey": "restore-original-after-sync"}):
            pass
        restored = self.worker.run_once()
        self.assertEqual(restored["status"], "published")
        self.assertEqual(restored["revision"], original["revision"])
        self.assertNotEqual(restored["publishedHead"], original["publishedHead"])
        self.assertNotEqual(restored["publishedHead"], published["publishedHead"])
        with self.request("/w/study/main/assets/interactive.json") as response:
            self.assertEqual(json.load(response)["notes"][0]["pitch"], 60)
        self.assertEqual(self.site.movements[("study", "main")]["model"]["notes"][0]["pitch"], 61)
        self.assertEqual(self.site.movements[("second-study", "main")]["model"]["notes"][0]["pitch"], 60)

    def test_editor_requires_work_cookie_and_csrf_and_returns_real_job(self):
        self.serve_editor()
        self.failure(401, "/w/study/main/api/editor")
        with self.login():
            pass
        with self.request("/w/study/main/api/editor") as response:
            context = json.load(response)
        self.assertEqual(context["worker"]["status"], "unavailable")
        payload = {"action": "preview", "baseModelRevision": context["revision"], "basePublishedEditHead": context["publishedHead"],
                   "typedOperations": [{"op": "pitch.delta", "noteIds": ["n1"], "semitones": 1}], "idempotencyKey": "http-preview-fixture", "reason": ""}
        self.failure(403, "/w/study/main/api/editor", payload)
        payload["csrfToken"] = context["csrfToken"]
        with self.request("/w/study/main/api/editor", payload) as response:
            self.assertEqual(response.status, 202); queued = json.load(response)
        self.worker.run_once()
        with self.request("/w/study/main/api/editor?jobId=" + queued["jobId"]) as response:
            body = json.load(response)
            ready = body["job"]
        self.assertTrue(body["worker"]["ready"])
        self.assertEqual(ready["status"], "ready_to_publish")
        self.assertIn("/w/study/main/api/editor/asset", ready["preview"]["audio"])
        self.failure(401, "/w/second-study/main/api/editor?jobId=" + queued["jobId"])
        with self.request(ready["preview"]["audio"]) as response:
            self.assertEqual(response.read(4), b"RIFF")
        with self.request("/w/study/main/api/editor", dict(payload, action="publish", jobId=queued["jobId"], idempotencyKey="http-publish-fixture")) as response:
            published_job = json.load(response)
        published = self.worker.run_once()
        self.assertEqual(published["status"], "published")
        with self.request("/w/study/main/assets/interactive.json") as response:
            model = json.load(response)
        self.assertEqual(model["notes"][0]["pitch"], 61)
        self.assertEqual(model["editHead"], published["publishedHead"])
        health = self.site.health()
        movement = next(work for work in health["works"] if work["id"] == "study")["movements"][0]
        self.assertEqual(movement["revision"], published["revision"])
        self.assertEqual(movement["baseRevision"], "fixture-v1")
        self.assertEqual(movement["editHead"], published["publishedHead"])
        self.failure(409, "/w/study/main/api/editor/export?head=" + context["publishedHead"])
        self.failure(401, "/w/second-study/main/api/editor/asset?jobId=" + queued["jobId"] + "&name=assets%2Fbase.wav")
        with self.request("/w/study/main/assets/interactive.json?editHead=" + context["publishedHead"]) as response:
            original = json.load(response)
        self.assertEqual(original["notes"][0]["pitch"], 60)
        self.assertIn("editHead=", model["audio"])
