"""Unprivileged staging for an independently administered, isolated website.

This receiver never starts/stops a service, runs sudo, reads service secrets,
changes a root-owned release, or installs a privileged automation channel. An
independently provisioned publisher may consume its bounded data-only request.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import tempfile
import time
import uuid


SYSTEM_OWNER = 0
MODE = "isolated-manual"
AUTO_MODE = "isolated-auto"
RELEASE = re.compile(r"release-[0-9a-f]{20}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REQUEST_ID = re.compile(r"[0-9a-f]{32}\Z")
PUBLISHER_ERRORS = {"invalid-request", "stale-generation", "validation-failed", "preflight-failed",
                    "publication-failed-restored", "recovery-required", "request-id-collision",
                    "capacity-exceeded", "internal-error"}
TERMINAL = {"succeeded", "failed", "blocked"}


def nonnegative_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def bounded_json(path, owner, maximum=16384):
    """Reject links/devices before reading an owner-controlled bounded receipt."""
    path = absolute_path(str(path))
    if path.resolve() != path or path.is_symlink():
        raise ValueError("Receipt must not contain symlink components")
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        value = os.fstat(stream.fileno())
        if not stat.S_ISREG(value.st_mode) or value.st_uid != owner or value.st_mode & 0o022:
            raise ValueError("Receipt must be owner-controlled and not writable by other accounts")
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("Isolated receipt is too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Isolated receipt must be an object")
    return value, raw


def validate_auto_request(value):
    if (set(value) != {"schemaVersion", "requestId", "releaseId", "manifestSha256", "expectedCurrentRelease", "expectedGeneration"}
            or value.get("schemaVersion") != 2 or isinstance(value.get("schemaVersion"), bool)
            or not isinstance(value.get("requestId"), str) or not REQUEST_ID.fullmatch(value["requestId"])
            or not isinstance(value.get("releaseId"), str) or not RELEASE.fullmatch(value["releaseId"])
            or not isinstance(value.get("manifestSha256"), str) or not SHA256.fullmatch(value["manifestSha256"])
            or value["releaseId"] != "release-" + value["manifestSha256"][:20]
            or not isinstance(value.get("expectedCurrentRelease"), str) or not RELEASE.fullmatch(value["expectedCurrentRelease"])
            or not nonnegative_integer(value.get("expectedGeneration"))):
        raise ValueError("Invalid isolated publisher request")
    return value


def validate_publisher_status(value):
    required = {"schemaVersion", "publisherMode", "status", "generation", "currentRelease", "manifestSha256",
                "changed", "requestId", "releaseId", "previousRelease", "errorCode", "recent"}
    if (set(value) != required or value["schemaVersion"] != 1 or isinstance(value["schemaVersion"], bool)
            or value["publisherMode"] != AUTO_MODE or value["status"] not in TERMINAL | {"idle", "queued", "publishing"}
            or not nonnegative_integer(value["generation"])
            or not isinstance(value["currentRelease"], str) or not RELEASE.fullmatch(value["currentRelease"])
            or not isinstance(value["manifestSha256"], str) or not SHA256.fullmatch(value["manifestSha256"])
            or value["currentRelease"] != "release-" + value["manifestSha256"][:20]
            or not isinstance(value["changed"], bool)
            or value["errorCode"] is not None and value["errorCode"] not in PUBLISHER_ERRORS):
        raise ValueError("Invalid isolated publisher status")
    if value["requestId"] is not None:
        if (not isinstance(value["requestId"], str) or not REQUEST_ID.fullmatch(value["requestId"])
                or not isinstance(value["releaseId"], str) or not RELEASE.fullmatch(value["releaseId"])
                or not isinstance(value["previousRelease"], str) or not RELEASE.fullmatch(value["previousRelease"])):
            raise ValueError("Invalid publisher status request identity")
    elif value["releaseId"] is not None or value["previousRelease"] is not None or value["status"] not in ("idle", "blocked"):
        raise ValueError("Publisher status is missing its request identity")
    if (value["status"] in ("failed", "blocked")) != (value["errorCode"] is not None):
        raise ValueError("Publisher failure status must carry a fixed error code")
    if not isinstance(value["recent"], list) or len(value["recent"]) > 32:
        raise ValueError("Publisher recent receipts exceed their bound")
    seen = set()
    for row in value["recent"]:
        if (not isinstance(row, dict) or set(row) != {"requestId", "requestSha256", "status", "releaseId", "generation", "changed", "errorCode"}
                or not isinstance(row["requestId"], str) or not REQUEST_ID.fullmatch(row["requestId"])
                or row["requestId"] in seen
                or not isinstance(row["requestSha256"], str) or not SHA256.fullmatch(row["requestSha256"])
                or row["status"] not in TERMINAL or not nonnegative_integer(row["generation"])
                or not isinstance(row["releaseId"], str) or not RELEASE.fullmatch(row["releaseId"])
                or not isinstance(row["changed"], bool)
                or row["errorCode"] is not None and row["errorCode"] not in PUBLISHER_ERRORS
                or (row["status"] in ("failed", "blocked")) != (row["errorCode"] is not None)):
            raise ValueError("Invalid publisher recent receipt")
        seen.add(row["requestId"])
    return value


def terminal_receipt(status, request, digest):
    """Use a matching immutable receipt even if a newer request owns status."""
    recorded = next((row for row in status["recent"] if row["requestId"] == request["requestId"]), None)
    if recorded is not None:
        if recorded["requestSha256"] != digest or recorded["releaseId"] != request["releaseId"]:
            raise RuntimeError("Pending request differs from its terminal publisher receipt; inspect it manually")
        return recorded
    if status["requestId"] == request["requestId"] and status["status"] in TERMINAL:
        raise RuntimeError("Terminal publisher status lacks its immutable request receipt; inspect it manually")
    return None


def absolute_path(value):
    if not isinstance(value, str) or not value or any(c in value for c in "\0\r\n"):
        raise ValueError("Expected an absolute deployment path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Expected an absolute path without traversal")
    return path


def root_owned(path, directory=False):
    path = absolute_path(str(path))
    if path.resolve() != path or path.is_symlink():
        raise ValueError("Isolated state must not contain symlink components")
    value = path.lstat()
    valid = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
    if not valid or value.st_uid != SYSTEM_OWNER or value.st_mode & 0o022:
        raise ValueError("Isolated state must be root-owned and not writable by services")
    return path


class IncomingLock:
    def __init__(self, incoming):
        self.path = incoming / ".site-sync.lock"
        self.owned = False

    def __enter__(self):
        try:
            self.path.mkdir(mode=0o700)
        except FileExistsError:
            raise RuntimeError("Another staging operation holds the incoming lock; inspect a stale lock manually")
        self.owned = True
        try:
            (self.path / "owner.json").write_text(json.dumps({"pid": os.getpid(), "started": time.time()}))
        except BaseException:
            self.path.rmdir()
            self.owned = False
            raise
        return self

    def __exit__(self, *unused):
        if self.owned:
            (self.path / "owner.json").unlink()
            self.path.rmdir()


class IsolatedSite:
    def __init__(self, profile, helpers):
        self.mode = profile.get("publisherMode")
        if self.mode not in (MODE, AUTO_MODE):
            raise ValueError("Wrong publisher mode")
        self.profile, self.h = profile, helpers
        self.root = root_owned(absolute_path(profile["remoteRoot"]), directory=True)
        if self.mode == MODE:
            self.helper = absolute_path(profile["isolatedPublishHelper"])
            self.admin_python = absolute_path(profile.get("isolatedAdminPython", "/usr/bin/python3"))
            try:
                self.helper.relative_to(self.root)
            except ValueError:
                raise ValueError("Privileged publication helper must stay in the configured deployment root") from None

    def activation_command(self):
        return "sudo " + " ".join(shlex.quote(str(value)) for value in (self.admin_python, "-I", self.helper, "publish"))

    def state(self):
        path = root_owned(self.root / "public-state.json")
        state, raw = bounded_json(path, SYSTEM_OWNER)
        fields = {"schemaVersion", "publisherMode", "release", "manifestSha256", "incomingRoot", "phase"}
        if self.mode == AUTO_MODE:
            fields.add("publisher")
        if (set(state) != fields
                or state["schemaVersion"] != 1 or isinstance(state["schemaVersion"], bool)
                or state["publisherMode"] != self.mode or state["phase"] != "committed"
                or not isinstance(state["release"], str) or not RELEASE.fullmatch(state["release"])
                or not isinstance(state["manifestSha256"], str) or not SHA256.fullmatch(state["manifestSha256"])
                or state["release"] != "release-" + state["manifestSha256"][:20]
                or absolute_path(state["incomingRoot"]) != self.root / "incoming"):
            raise ValueError("Isolated state does not describe a committed immutable release")
        if self.mode == AUTO_MODE:
            publisher = state["publisher"]
            if (not isinstance(publisher, dict) or set(publisher) != {"schemaVersion", "generation"}
                    or publisher["schemaVersion"] != 1 or isinstance(publisher["schemaVersion"], bool)
                    or not nonnegative_integer(publisher["generation"])):
                raise ValueError("Invalid isolated publisher generation")
        root_owned(self.root / "releases", directory=True)
        release = root_owned(self.root / "releases" / state["release"], directory=True)
        manifest = root_owned(release / "file-manifest.json")
        if self.h.digest(manifest) != state["manifestSha256"]:
            raise ValueError("Active manifest differs from the administrator's public state")
        entries = json.loads(manifest.read_text(encoding="utf-8"))["files"]
        entries = list(entries) + [{"path": "file-manifest.json", "bytes": manifest.stat().st_size, "sha256": state["manifestSha256"]}]
        files = self.h.verify_release(release, entries)
        for path in release.rglob("*"):
            root_owned(path, directory=path.is_dir())
        revisions = self.h.revisions(json.loads((release / "release.json").read_text(encoding="utf-8")))
        result = {"release": str(release), "releaseId": state["release"], "manifestSha256": state["manifestSha256"],
                "files": list(files.values()), "revisions": revisions, "incomingRoot": state["incomingRoot"],
                "publisherMode": self.mode, "_publicState": raw}
        if self.mode == AUTO_MODE:
            result["publisher"] = publisher
        return result

    def incoming(self):
        path = self.h.directory(self.root / "incoming")
        value = path.stat()
        if value.st_uid != os.getuid() or value.st_mode & 0o077:
            raise ValueError("Incoming staging must belong only to the deployment SSH account with mode 0700")
        releases = self.h.directory(path / "releases")
        if releases.stat().st_uid != os.getuid() or releases.stat().st_mode & 0o077:
            raise ValueError("Incoming releases must be private to the deployment SSH account")
        return path

    def pending_request(self, with_digest=False):
        path = self.incoming() / "request.json"
        try:
            value, raw = bounded_json(path, os.getuid(), maximum=4096)
        except FileNotFoundError:
            if path.is_symlink():
                raise ValueError("Pending request must not be a symlink") from None
            return (None, None) if with_digest else None
        value = validate_auto_request(value)
        return (value, hashlib.sha256(raw).hexdigest()) if with_digest else value

    def publisher_status(self):
        value, unused = bounded_json(root_owned(self.root / "publisher-status.json"), SYSTEM_OWNER)
        return validate_publisher_status(value)

    def status(self):
        if self.mode != AUTO_MODE:
            raise ValueError("Publisher status requires isolated-auto mode")
        pending, digest = self.pending_request(with_digest=True)
        return {"publisherStatus": self.publisher_status(), "pendingRequest": pending, "pendingRequestSha256": digest}

    def auto_result(self, before, request, actual=None, changed=(), entries=None, reused=False, request_digest=None):
        entries = entries or {}
        if request_digest is None:
            request_digest = hashlib.sha256((json.dumps(request, sort_keys=True) + "\n").encode()).hexdigest()
        return {"publisherMode": AUTO_MODE, "releaseId": request["releaseId"], "release": before["release"],
                "requestId": request["requestId"], "manifestSha256": request["manifestSha256"],
                "requestSha256": request_digest,
                "stagedRelease": str(self.root / "incoming/releases" / request["releaseId"]),
                "revisions": actual, "pendingActivation": True, "activated": False,
                "reusedPendingRequest": reused, "changed": True,
                "changedFiles": len(changed), "reusedFiles": len(entries) - len(changed),
                "transferBytes": sum(entries[name]["bytes"] for name in changed)}

    def inspect(self):
        result = {key: value for key, value in self.state().items() if not key.startswith("_")}
        if self.mode == AUTO_MODE:
            result.update(self.status())
        return result

    def check(self, request=None):
        before = self.state()
        request = request or {}
        if (("baseRelease" in request and request["baseRelease"] != before["release"])
                or ("revisions" in request and request["revisions"] != before["revisions"])
                or (self.mode == AUTO_MODE and "expectedGeneration" in request
                    and request["expectedGeneration"] != before["publisher"]["generation"])):
            raise RuntimeError("Active isolated release changed during the check; retry")
        public = self.h.fetch_health(self.profile["publicUrl"], {}, self.profile.get("healthTimeoutSeconds", 20), minimal=True)
        if self.state()["_publicState"] != before["_publicState"]:
            raise RuntimeError("Administrator changed the release during the check; retry")
        result = {"release": before["release"], "revisions": before["revisions"], "publisherMode": self.mode,
                "health": {"public": public, "scope": "minimal-public-and-frozen-manifest", "internal": "administrator-verification-required"},
                "pendingAdminActivation": False}
        if self.mode == AUTO_MODE:
            result.update(self.status())
        return result

    def stage(self, request, archive):
        incoming = self.incoming()
        with IncomingLock(incoming):
            before = self.state()
            if request["baseRelease"] != before["release"]:
                raise RuntimeError("Current isolated release changed since planning; retry")
            if self.mode == AUTO_MODE:
                if (not nonnegative_integer(request.get("expectedGeneration"))
                        or request["expectedGeneration"] != before["publisher"]["generation"]):
                    raise RuntimeError("Publisher generation changed since planning; retry")
                if not isinstance(request.get("retryFailed", False), bool):
                    raise ValueError("retryFailed must be a boolean")
            entries = self.h.file_entries(request["files"])
            release_id = request["releaseId"]
            manifest_hash = entries["file-manifest.json"]["sha256"]
            if not isinstance(release_id, str) or not RELEASE.fullmatch(release_id) or release_id != "release-" + manifest_hash[:20]:
                raise ValueError("Incoming release is not bound to its manifest")
            if self.mode == AUTO_MODE:
                pending, pending_digest = self.pending_request(with_digest=True)
                status = self.publisher_status()
                if pending is not None:
                    recorded = terminal_receipt(status, pending, pending_digest)
                    if recorded is None:
                        if pending["releaseId"] != release_id or pending["manifestSha256"] != manifest_hash:
                            raise RuntimeError("Another unresolved publisher request exists; inspect its status")
                        return self.auto_result(before, pending, reused=True, request_digest=pending_digest)
                    if recorded["status"] in ("failed", "blocked") and not request.get("retryFailed", False):
                        raise RuntimeError("Failed publisher request requires explicit --retry-failed")
            base = {entry["path"]: entry for entry in before["files"]}
            changed = {name for name, entry in entries.items() if base.get(name) != entry}
            if changed != set(request["changed"]):
                raise RuntimeError("Base release content changed since planning; retry")
            staging = incoming / "releases" / (".staging-" + uuid.uuid4().hex)
            staging.mkdir(mode=0o700)
            destination = incoming / "releases" / release_id
            try:
                self.h.unpack_changed(archive, staging, changed, entries)
                for name, entry in entries.items():
                    if name in changed:
                        continue
                    source = root_owned(Path(before["release"]) / name)
                    target = staging / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Copy bytes only: never hard-link/chmod administrator-owned inodes.
                    shutil.copyfile(str(source), str(target))
                self.h.verify_release(staging, list(entries.values()))
                actual = self.h.revisions(json.loads((staging / "release.json").read_text(encoding="utf-8")))
                if actual != request["revisions"]:
                    raise ValueError("Staged revisions differ from the publication plan")
                if destination.exists() or destination.is_symlink():
                    self.h.verify_release(destination, list(entries.values()))
                else:
                    for path in staging.rglob("*"):
                        if path.is_file():
                            path.chmod(0o444)
                    staging.rename(destination)
                if self.state()["_publicState"] != before["_publicState"]:
                    raise RuntimeError("Administrator activated another release while staging; retry")
                publish_request = {"schemaVersion": 1, "releaseId": release_id, "manifestSha256": manifest_hash,
                                   "expectedCurrentRelease": before["releaseId"]}
                if self.mode == AUTO_MODE:
                    publish_request.update(schemaVersion=2, requestId=uuid.uuid4().hex,
                                           expectedGeneration=before["publisher"]["generation"])
                    validate_auto_request(publish_request)
                self.h.atomic_write(incoming / "request.json", (json.dumps(publish_request, sort_keys=True) + "\n").encode())
                if self.mode == AUTO_MODE:
                    return self.auto_result(before, publish_request, actual=actual, changed=changed, entries=entries)
                return {"publisherMode": MODE, "releaseId": release_id, "release": before["release"],
                        "stagedRelease": str(destination), "revisions": actual, "manifestSha256": manifest_hash,
                        "pendingAdminActivation": True, "activated": False, "activationCommand": self.activation_command(),
                        "health": {"activeRelease": "unchanged", "stagedRuntime": "administrator-preflight-required"},
                        "changed": True, "changedFiles": len(changed), "reusedFiles": len(entries) - len(changed),
                        "transferBytes": sum(entries[name]["bytes"] for name in changed)}
            finally:
                if staging.exists():
                    shutil.rmtree(staging)


def dispatch(profile, request, stream, helpers):
    site = IsolatedSite(profile, helpers)
    action = request["action"]
    if action == "inspect":
        return site.inspect()
    if action == "check":
        return site.check(request)
    if action == "status":
        return site.status()
    if action == "rollback":
        raise ValueError("Release rollback is unsupported in isolated publisher modes. The isolation installer's rollback restores the original service accounts and must not be used as a release rollback.")
    if action != "sync":
        raise ValueError("Unknown isolated receiver action")
    entries = helpers.file_entries(request["files"])
    changed = request["changed"]
    if not isinstance(changed, list) or len(changed) != len(set(changed)) or any(name not in entries for name in changed):
        raise ValueError("Invalid changed-file plan")
    size = request["archiveBytes"]
    maximum = sum(entries[name]["bytes"] for name in changed) + len(changed) * 4096 + 10240
    if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= maximum:
        raise ValueError("Transfer size exceeds its bounded plan")
    incoming = site.incoming()
    descriptor, filename = tempfile.mkstemp(prefix=".site-transfer-", suffix=".tar", dir=str(incoming))
    archive = Path(filename)
    try:
        import hashlib
        hasher = hashlib.sha256()
        with os.fdopen(descriptor, "wb") as output:
            remaining = size
            while remaining:
                data = stream.read(min(1024 * 1024, remaining))
                if not data:
                    raise ValueError("Truncated transfer")
                output.write(data)
                hasher.update(data)
                remaining -= len(data)
        if hasher.hexdigest() != request["archiveSha256"] or stream.read(1):
            raise ValueError("Transfer digest or framing mismatch")
        return site.stage(request, archive)
    finally:
        archive.unlink()
