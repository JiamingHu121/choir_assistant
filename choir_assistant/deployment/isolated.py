"""Unprivileged staging for an independently administered, isolated website.

This receiver never starts/stops a service, runs sudo, reads service secrets,
changes a root-owned release, or creates a privileged automation channel.
"""
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
RELEASE = re.compile(r"release-[0-9a-f]{20}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


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
        if profile.get("publisherMode") != MODE:
            raise ValueError("Wrong publisher mode")
        self.profile, self.h = profile, helpers
        self.root = root_owned(absolute_path(profile["remoteRoot"]), directory=True)
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
        raw = path.read_bytes()
        if len(raw) > 16384:
            raise ValueError("Isolated public state is too large")
        state = json.loads(raw)
        if (set(state) != {"schemaVersion", "publisherMode", "release", "manifestSha256", "incomingRoot", "phase"}
                or state["schemaVersion"] != 1 or state["publisherMode"] != MODE or state["phase"] != "committed"
                or not isinstance(state["release"], str) or not RELEASE.fullmatch(state["release"])
                or not isinstance(state["manifestSha256"], str) or not SHA256.fullmatch(state["manifestSha256"])
                or state["release"] != "release-" + state["manifestSha256"][:20]
                or absolute_path(state["incomingRoot"]) != self.root / "incoming"):
            raise ValueError("Isolated state does not describe a committed immutable release")
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
        return {"release": str(release), "releaseId": state["release"], "manifestSha256": state["manifestSha256"],
                "files": list(files.values()), "revisions": revisions, "incomingRoot": state["incomingRoot"],
                "publisherMode": MODE, "_publicState": raw}

    def incoming(self):
        path = self.h.directory(self.root / "incoming")
        value = path.stat()
        if value.st_uid != os.getuid() or value.st_mode & 0o077:
            raise ValueError("Incoming staging must belong only to the deployment SSH account with mode 0700")
        releases = self.h.directory(path / "releases")
        if releases.stat().st_uid != os.getuid() or releases.stat().st_mode & 0o077:
            raise ValueError("Incoming releases must be private to the deployment SSH account")
        return path

    def inspect(self):
        return {key: value for key, value in self.state().items() if not key.startswith("_")}

    def check(self, request=None):
        before = self.state()
        request = request or {}
        if (("baseRelease" in request and request["baseRelease"] != before["release"])
                or ("revisions" in request and request["revisions"] != before["revisions"])):
            raise RuntimeError("Active isolated release changed during the check; retry")
        public = self.h.fetch_health(self.profile["publicUrl"], {}, self.profile.get("healthTimeoutSeconds", 20), minimal=True)
        if self.state()["_publicState"] != before["_publicState"]:
            raise RuntimeError("Administrator changed the release during the check; retry")
        return {"release": before["release"], "revisions": before["revisions"], "publisherMode": MODE,
                "health": {"public": public, "scope": "minimal-public-and-frozen-manifest", "internal": "administrator-verification-required"},
                "pendingAdminActivation": False}

    def stage(self, request, archive):
        incoming = self.incoming()
        with IncomingLock(incoming):
            before = self.state()
            if request["baseRelease"] != before["release"]:
                raise RuntimeError("Current isolated release changed since planning; retry")
            entries = self.h.file_entries(request["files"])
            release_id = request["releaseId"]
            manifest_hash = entries["file-manifest.json"]["sha256"]
            if not isinstance(release_id, str) or not RELEASE.fullmatch(release_id) or release_id != "release-" + manifest_hash[:20]:
                raise ValueError("Incoming release is not bound to its manifest")
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
                self.h.atomic_write(incoming / "request.json", (json.dumps(publish_request, sort_keys=True) + "\n").encode())
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
    if action == "rollback":
        raise ValueError("Release rollback is unsupported in isolated-manual mode. The isolation installer's rollback restores the original service accounts and must not be used as a release rollback.")
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
