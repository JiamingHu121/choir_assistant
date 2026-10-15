"""Stdlib-only receiver for immutable site releases (Python 3.8+).

The caller sends this source over an already configured SSH connection.  It
manages one explicitly named user LaunchAgent; DNS, gateways and SSH keys are
outside this module's authority.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid


SERVICE = "choir-score-library-site"
USER_AGENT = "ChoirAssistant-SiteSync/1.0"


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def safe_name(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid relative file path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(item in ("", ".", "..") for item in value.split("/")):
        raise ValueError("Unsafe relative file path: " + value)
    return value


def regular(path):
    return stat.S_ISREG(Path(path).lstat().st_mode)


def directory(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or not path.is_dir() or path.is_symlink():
        raise ValueError("Expected an existing directory without symlink components: " + str(path))
    return path


def file_entries(entries):
    result = {}
    if not isinstance(entries, list) or len(entries) > 100000:
        raise ValueError("Invalid file manifest")
    for entry in entries:
        name = safe_name(entry["path"])
        if name in result or not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", "")):
            raise ValueError("Duplicate or invalid manifest entry")
        size = entry.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= 20 * 1024 ** 3:
            raise ValueError("Invalid manifest file size")
        result[name] = {"path": name, "sha256": entry["sha256"], "bytes": size}
    if "site_server.py" not in result or "release.json" not in result or "file-manifest.json" not in result:
        raise ValueError("Release lacks its standalone runtime or manifests")
    # A file cannot also be another entry's directory.
    for name in result:
        if any(str(parent) in result for parent in PurePosixPath(name).parents if str(parent) != "."):
            raise ValueError("Manifest has conflicting file and directory paths")
    return result


def inventory(root):
    root = directory(root)
    found = {}
    for base, directories, files in os.walk(str(root), followlinks=False):
        for name in directories + files:
            path = Path(base) / name
            if path.is_symlink():
                raise ValueError("Release contains a symlink: " + str(path))
        for name in files:
            path = Path(base) / name
            if not regular(path):
                raise ValueError("Release contains a non-regular file")
            relative = path.relative_to(root).as_posix()
            found[relative] = {"path": relative, "sha256": digest(path), "bytes": path.stat().st_size}
    return found


def verify_release(root, entries):
    expected = file_entries(entries)
    actual = inventory(root)
    if actual != expected:
        raise ValueError("Staged release exact file set or SHA-256 mismatch")
    manifest = json.loads((Path(root) / "file-manifest.json").read_text(encoding="utf-8"))
    described = {entry["path"]: entry for entry in manifest["files"]}
    without_self = {key: value for key, value in expected.items() if key != "file-manifest.json"}
    if manifest.get("schemaVersion") != 1 or described != without_self or len(described) != len(manifest["files"]):
        raise ValueError("Manifest does not describe the exact staged release")
    return actual


def revisions(release):
    result = {}
    for wid, work in release["works"].items():
        for mid, movement in work["movements"].items():
            result[wid + "/" + mid] = movement["revision"]
    if not result:
        raise ValueError("Release has no movements")
    return result


def verify_health(health, expected):
    actual = {}
    works = health.get("works", [])
    for work in works:
        if work.get("loaded") is not True:
            raise ValueError("An exported work did not load")
        for movement in work.get("movements", []):
            key = work["id"] + "/" + movement["id"]
            if movement.get("loaded") is not True or key in actual:
                raise ValueError("An exported movement did not load exactly once")
            actual[key] = movement["revision"]
    if (health.get("service") != SERVICE or health.get("readOnly") is not True or
            health.get("synthesisAvailable") is not True or actual != expected or
            health.get("loadedWorks") != len(works) or len(works) != len(set(key.split("/")[0] for key in expected))):
        raise ValueError("Site health does not match the complete expected release or synthesis runtime")
    return health


def fetch_health(url, expected, timeout=10):
    request = urllib.request.Request(url.rstrip("/") + "/api/health", headers={"User-Agent": USER_AGENT,
                                    "Cache-Control": "no-cache"})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, new_url):
            return None
    # TLS keeps the system trust store, hostname verification and SNI.  A
    # redirect cannot silently send this check to another origin or plain HTTP.
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError("Site health returned a non-200 status")
        body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise ValueError("Health response is too large")
        return verify_health(json.loads(body), expected)


def atomic_write(path, data, mode=0o600):
    path = Path(path)
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def replace_current(root, target):
    current = root / "current"
    temporary = root / (".current-" + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target)
        os.replace(str(temporary), str(current))
    finally:
        if temporary.is_symlink():
            temporary.unlink()


class LaunchdService:
    def __init__(self, profile):
        self.profile = profile
        self.root = directory(Path(profile["remoteRoot"]))
        self.label = profile["launchdLabel"]
        self.plist = Path.home() / "Library/LaunchAgents" / (self.label + ".plist")
        self.domain = "gui/" + str(os.getuid())

    def owned(self):
        if not self.plist.exists() or not regular(self.plist) or self.plist.is_symlink():
            raise ValueError("Dedicated LaunchAgent is absent; perform explicit server setup first")
        data = self.plist.read_bytes()
        value = plistlib.loads(data)
        arguments = value.get("ProgramArguments", [])
        if value.get("Label") != self.label or not isinstance(arguments, list):
            raise ValueError("Refusing an unrelated LaunchAgent")
        scripts = [Path(arg) for arg in arguments if isinstance(arg, str) and arg.endswith("/site_server.py")]
        if len(scripts) != 1:
            raise ValueError("LaunchAgent does not run one dedicated site server")
        script = scripts[0]
        relative = script.relative_to(self.root)
        if (len(relative.parts) != 3 or relative.parts[0] != "releases" or
                relative.parts[-1] != "site_server.py"):
            raise ValueError("LaunchAgent script is outside owned releases")
        release = directory(script.parent)
        if not regular(script) or self.profile["remotePython"] not in arguments:
            raise ValueError("LaunchAgent runtime does not match the explicit profile")
        root_flags = [flag for flag in ("--root", "--release") if flag in arguments]
        if len(root_flags) != 1 or arguments[arguments.index(root_flags[0]) + 1] != str(release):
            raise ValueError("LaunchAgent release argument differs from its owned script")
        for flag, expected in (("--bind", self.profile["bind"]), ("--port", str(self.profile["port"]))):
            if flag not in arguments or arguments[arguments.index(flag) + 1] != expected:
                raise ValueError("LaunchAgent binding differs from the explicit profile")
        current = self.root / "current"
        if current.exists() or current.is_symlink():
            if not current.is_symlink() or current.resolve() != release:
                raise ValueError("Current release and LaunchAgent disagree")
        return {"plist": data, "agent": value, "release": release,
                "current": os.readlink(str(current)) if current.is_symlink() else None}

    def updated(self, state, release):
        agent = dict(state["agent"])
        arguments = list(agent["ProgramArguments"])
        old = str(state["release"])
        arguments = [str(release / "site_server.py") if item == old + "/site_server.py" else item for item in arguments]
        for flag in ("--root", "--release"):
            if flag in arguments:
                arguments[arguments.index(flag) + 1] = str(release)
        agent["ProgramArguments"] = arguments
        if agent.get("WorkingDirectory") == old:
            agent["WorkingDirectory"] = str(release)
        return plistlib.dumps(agent)

    def restart(self, plist_bytes):
        # Address only this owned label, then wait for its listener to exit.
        subprocess.run(["/bin/launchctl", "bootout", self.domain + "/" + self.label],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            loaded = subprocess.run(["/bin/launchctl", "print", self.domain + "/" + self.label],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode == 0
            listener = subprocess.run(["/usr/sbin/lsof", "-nP", "-iTCP:" + str(self.profile["port"]),
                                       "-sTCP:LISTEN", "-t"], stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, timeout=5).stdout.strip()
            if not loaded and not listener:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("Owned LaunchAgent or listener did not stop; no other process was killed")
        atomic_write(self.plist, plist_bytes)
        subprocess.run(["/bin/launchctl", "bootstrap", self.domain, str(self.plist)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)

    def health(self, expected, public=True):
        deadline = time.monotonic() + self.profile.get("healthTimeoutSeconds", 90)
        error = None
        while time.monotonic() < deadline:
            try:
                local = fetch_health("http://127.0.0.1:" + str(self.profile["port"]), expected,
                                     min(10, max(1, deadline - time.monotonic())))
                break
            except Exception as problem:
                error = problem
                time.sleep(.3)
        else:
            raise RuntimeError("Local release health failed: " + str(error))
        if public:
            fetch_health(self.profile["publicUrl"], expected, 20)
        return {"local": local, "public": "passed" if public else "not-requested"}


def inspect(profile, service=None):
    service = service or LaunchdService(profile)
    state = service.owned()
    root = state["release"]
    release = json.loads((root / "release.json").read_text(encoding="utf-8"))
    return {"release": str(root), "current": state["current"],
            "files": list(inventory(root).values()), "revisions": revisions(release)}


class ProjectLock:
    def __init__(self, root):
        self.path = root / "shared/.site-sync.lock"

    def __enter__(self):
        directory(self.path.parent)
        try:
            self.path.mkdir(mode=0o700)
        except FileExistsError:
            raise RuntimeError("Another site deployment holds the project lock; inspect it before removing a stale lock")
        (self.path / "owner.json").write_text(json.dumps({"pid": os.getpid(), "started": time.time()}))
        return self

    def __exit__(self, kind, value, traceback):
        (self.path / "owner.json").unlink()
        self.path.rmdir()


def unpack_changed(archive, staging, changed, entries):
    seen = set()
    with tarfile.open(str(archive), "r:") as stream:
        for member in stream:
            name = safe_name(member.name)
            if name not in changed or name in seen or not member.isfile() or member.size != entries[name]["bytes"]:
                raise ValueError("Unexpected, duplicate or non-regular transfer member")
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with stream.extractfile(member) as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
            if digest(destination) != entries[name]["sha256"]:
                raise ValueError("Transferred member SHA-256 mismatch")
            seen.add(name)
    if seen != set(changed):
        raise ValueError("Transfer omitted expected changed files")


def apply_release(profile, request, archive, service=None):
    service = service or LaunchdService(profile)
    root = directory(Path(profile["remoteRoot"]))
    entries = file_entries(request["files"])
    release_id = request["releaseId"]
    if not re.fullmatch(r"release-[0-9a-f]{20}", release_id):
        raise ValueError("Invalid content-addressed release ID")
    if release_id != "release-" + entries["file-manifest.json"]["sha256"][:20]:
        raise ValueError("Release ID is not bound to its manifest")
    with ProjectLock(root):
        state = service.owned()
        if str(state["release"]) != request["baseRelease"]:
            raise RuntimeError("Current release changed since planning; retry the sync")
        old_inventory = inventory(state["release"])
        changed = {name for name, entry in entries.items() if old_inventory.get(name) != entry}
        if changed != set(request["changed"]):
            raise RuntimeError("Base release content changed since planning; retry the sync")
        old_expected = revisions(json.loads((state["release"] / "release.json").read_text()))
        if not changed and old_inventory == entries:
            return {"changed": False, "releaseId": state["release"].name, "transferBytes": 0,
                    "health": service.health(old_expected)}
        releases = directory(root / "releases")
        staging = releases / (".staging-" + uuid.uuid4().hex)
        staging.mkdir(mode=0o700)
        switched = False
        new_release = releases / release_id
        try:
            unpack_changed(archive, staging, changed, entries)
            for name, entry in entries.items():
                if name in changed:
                    continue
                source, target = state["release"] / name, staging / name
                if not regular(source) or source.is_symlink() or digest(source) != entry["sha256"]:
                    raise ValueError("Reusable source changed during staging")
                target.parent.mkdir(parents=True, exist_ok=True)
                # Read-only files can share storage.  Existing writable files
                # are copied so chmod on the new release cannot edit the old
                # release's inode metadata.
                if source.stat().st_mode & 0o222:
                    shutil.copyfile(str(source), str(target))
                else:
                    os.link(str(source), str(target))
            verify_release(staging, list(entries.values()))
            new_expected = revisions(json.loads((staging / "release.json").read_text()))
            if new_expected != request["revisions"]:
                raise ValueError("Staged revisions differ from the requested release")
            if new_release.exists() or new_release.is_symlink():
                verify_release(new_release, list(entries.values()))
                shutil.rmtree(staging)
            else:
                # Retained releases are never edited or garbage-collected here.
                for path in staging.rglob("*"):
                    if path.is_file():
                        path.chmod(0o444)
                staging.rename(new_release)
            updated = service.updated(state, new_release)
            switched = True
            replace_current(root, "releases/" + release_id)
            service.restart(updated)
            health = service.health(new_expected)
            receipt = {"schemaVersion": 1, "releaseId": release_id, "release": str(new_release),
                       "previousRelease": str(state["release"]), "previousCurrent": state["current"],
                       "previousPlist": state["plist"].hex(), "previousFiles": list(old_inventory.values()),
                       "revisions": new_expected,
                       "manifestSha256": entries["file-manifest.json"]["sha256"],
                       "changedFiles": len(changed), "reusedFiles": len(entries) - len(changed),
                       "transferBytes": sum(entries[name]["bytes"] for name in changed),
                       "health": health, "changed": True}
            atomic_write(root / "shared/site-sync-last.json", json.dumps(receipt).encode("utf-8"))
            return {key: value for key, value in receipt.items() if key not in ("previousPlist", "previousFiles")}
        except BaseException as error:
            if switched:
                try:
                    if state["current"] is None:
                        (root / "current").unlink()
                    else:
                        replace_current(root, state["current"])
                    service.restart(state["plist"])
                    service.health(old_expected)
                except BaseException as restoration:
                    raise RuntimeError("Deployment failed ({0}); rollback also needs attention ({1})".format(error, restoration)) from error
                raise RuntimeError("Deployment failed; previous release restored and verified: " + str(error)) from error
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def rollback(profile, service=None):
    service = service or LaunchdService(profile)
    root = directory(Path(profile["remoteRoot"]))
    with ProjectLock(root):
        state = service.owned()
        receipt_path = root / "shared/site-sync-last.json"
        if receipt_path.is_symlink() or not regular(receipt_path):
            raise ValueError("No intact deployment rollback receipt")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if str(state["release"]) != receipt["release"]:
            raise ValueError("Rollback receipt no longer describes the active release")
        previous = Path(receipt["previousRelease"])
        if previous.parent != root / "releases":
            raise ValueError("Rollback target is outside owned releases")
        directory(previous)
        previous_current = receipt["previousCurrent"]
        if previous_current is not None and (root / previous_current).resolve() != previous:
            raise ValueError("Rollback current symlink does not identify the retained release")
        if inventory(previous) != {entry["path"]: entry for entry in receipt.get("previousFiles", [])}:
            raise ValueError("Rollback target exact file set or SHA-256 changed")
        previous_plist = bytes.fromhex(receipt["previousPlist"])
        # The saved plist must still describe the same owned target and binding,
        # rather than introducing a different executable from an edited receipt.
        if plistlib.loads(previous_plist) != plistlib.loads(service.updated(state, previous)):
            raise ValueError("Rollback plist differs from the dedicated service configuration")
        expected = revisions(json.loads((previous / "release.json").read_text()))
        active_expected = revisions(json.loads((state["release"] / "release.json").read_text()))
        try:
            if receipt["previousCurrent"] is None:
                (root / "current").unlink()
            else:
                replace_current(root, receipt["previousCurrent"])
            service.restart(previous_plist)
            health = service.health(expected)
        except BaseException as error:
            replace_current(root, "releases/" + state["release"].name)
            service.restart(state["plist"])
            service.health(active_expected)
            raise RuntimeError("Rollback failed; active release restored: " + str(error)) from error
        receipt["rolledBack"] = True
        atomic_write(receipt_path, json.dumps(receipt).encode("utf-8"))
        return {"rolledBack": True, "release": str(previous), "revisions": expected, "health": health}


def main():
    raw = sys.stdin.buffer.readline(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024 or not raw.endswith(b"\n"):
        raise ValueError("Oversized receiver request")
    request = json.loads(raw)
    action, profile = request["action"], request["profile"]
    archive = None
    try:
        if action == "inspect":
            result = inspect(profile)
        elif action == "check":
            service = LaunchdService(profile)
            state = service.owned()
            expected = revisions(json.loads((state["release"] / "release.json").read_text()))
            if (("baseRelease" in request and str(state["release"]) != request["baseRelease"]) or
                    ("revisions" in request and expected != request["revisions"])):
                raise RuntimeError("Active release changed during the no-op sync; retry")
            health = service.health(expected)
            if service.owned()["release"] != state["release"]:
                raise RuntimeError("Active release changed during the health check; retry")
            result = {"release": str(state["release"]), "revisions": expected, "health": health}
        elif action == "rollback":
            result = rollback(profile)
        elif action == "sync":
            size = request["archiveBytes"]
            maximum = sum(entry["bytes"] for entry in request["files"] if entry["path"] in request["changed"]) + len(request["changed"]) * 4096 + 10240
            if not isinstance(size, int) or not 0 <= size <= maximum:
                raise ValueError("Transfer size exceeds its bounded plan")
            root = directory(Path(profile["remoteRoot"]))
            descriptor, name = tempfile.mkstemp(prefix=".site-transfer-", suffix=".tar", dir=str(directory(root / "shared")))
            archive = Path(name)
            hasher = hashlib.sha256()
            with os.fdopen(descriptor, "wb") as output:
                remaining = size
                while remaining:
                    chunk = sys.stdin.buffer.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("Truncated transfer")
                    remaining -= len(chunk)
                    output.write(chunk)
                    hasher.update(chunk)
            if hasher.hexdigest() != request["archiveSha256"] or sys.stdin.buffer.read(1):
                raise ValueError("Transfer digest or framing mismatch")
            result = apply_release(profile, request, archive)
        else:
            raise ValueError("Unknown receiver action")
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    finally:
        if archive is not None:
            archive.unlink()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        sys.exit(1)
