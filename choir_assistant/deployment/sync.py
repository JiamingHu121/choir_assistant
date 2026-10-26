"""Profile-driven incremental publishing over an existing SSH connection.

Profiles belong beside a deployment, outside the distributable plugin.  The
plugin never provisions a domain, tunnel, SSH key, synthesis engine or account.
"""
import base64
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from urllib.parse import urlsplit

from . import remote


def _absolute(value, field):
    if not isinstance(value, str) or not value or any(char in value for char in ("\x00", "\n", "\r")):
        raise ValueError(field + " must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(field + " must be an absolute path without parent traversal")
    return str(path)


def load_profile(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError("Deployment profile does not exist")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schemaVersion") != 1:
        raise ValueError("Deployment profile schemaVersion must be 1")
    for field in ("libraryRoot", "outputRoot", "remoteRoot", "remotePython"):
        value[field] = _absolute(value.get(field), field)
    for field in ("libraryRoot", "outputRoot"):
        value[field] = str(Path(value[field]).resolve())
    plugin_root = Path(__file__).resolve().parents[2]
    for field in ("libraryRoot", "outputRoot"):
        candidate = Path(value[field])
        if candidate == plugin_root or plugin_root in candidate.parents:
            raise ValueError(field + " must be outside the installed plugin")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value.get("sshHost", "")):
        raise ValueError("sshHost must be an existing SSH alias, without options or user syntax")
    mode = value.get("publisherMode", "user-launchagent")
    if mode not in ("user-launchagent", "isolated-manual", "isolated-auto"):
        raise ValueError("Unsupported publisherMode")
    value["publisherMode"] = mode
    if mode == "isolated-manual":
        value["isolatedPublishHelper"] = _absolute(value.get("isolatedPublishHelper"), "isolatedPublishHelper")
        value["isolatedAdminPython"] = _absolute(value.get("isolatedAdminPython", "/usr/bin/python3"), "isolatedAdminPython")
        try:
            Path(value["isolatedPublishHelper"]).relative_to(value["remoteRoot"])
        except ValueError:
            raise ValueError("isolatedPublishHelper must belong to remoteRoot") from None
    elif mode == "user-launchagent" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value.get("launchdLabel", "")):
        raise ValueError("Invalid dedicated launchdLabel")
    if value.get("bind", "127.0.0.1") != "127.0.0.1":
        raise ValueError("The publishing profile must keep the backend on loopback")
    value["bind"] = "127.0.0.1"
    if isinstance(value.get("port"), bool) or not isinstance(value.get("port"), int) or not 1024 <= value["port"] <= 65535:
        raise ValueError("port must be an unprivileged TCP port")
    parsed = urlsplit(value.get("publicUrl", ""))
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or
            parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("publicUrl must be an HTTPS origin without credentials, path or query")
    value["publicUrl"] = value["publicUrl"].rstrip("/")
    for field, default, low, high in (("sshTimeoutSeconds", 20, 2, 120), ("healthTimeoutSeconds", 90, 5, 300),
                                      ("transferTimeoutSeconds", 1800, 30, 7200)):
        number = value.get(field, default)
        if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
            raise ValueError("Invalid " + field)
        value[field] = number
    for field in ("requireReady", "prepareOriginal"):
        if field in value and not isinstance(value[field], bool):
            raise ValueError(field + " must be a boolean")
    if "editorSync" in value:
        config = value["editorSync"]
        if not isinstance(config, dict) or set(config) - {"enabled", "passwordFile"} or type(config.get("enabled")) is not bool:
            raise ValueError("editorSync must contain enabled and an optional private passwordFile")
        if config["enabled"]:
            from .editor_sync import validate_password_file
            config["passwordFile"] = str(validate_password_file(_absolute(config.get("passwordFile"), "editorSync passwordFile")))
    library, output = Path(value["libraryRoot"]), Path(value["outputRoot"])
    if library == output or library in output.parents and (output / "catalog.json").exists():
        raise ValueError("Output cannot replace the library itself")
    return value


def _ready(profile, ready):
    if profile.get("requireReady", False) and not ready:
        raise ValueError("This deployment profile requires --ready before any server connection")


def _remote_profile(profile):
    keys = ["remoteRoot", "remotePython", "publicUrl", "bind", "port", "healthTimeoutSeconds", "publisherMode"]
    mode = profile.get("publisherMode")
    if mode == "isolated-manual":
        keys += ["isolatedPublishHelper", "isolatedAdminPython"]
    elif mode != "isolated-auto":
        keys += ["launchdLabel"]
    return {key: profile[key] for key in keys if key in profile}


def _call(profile, action, payload=None, archive=None, timeout=None):
    """Run only the bundled receiver; request data travels on stdin, never shell interpolation."""
    source = Path(remote.__file__).read_bytes()
    program = "import base64;"
    if profile.get("publisherMode") in ("isolated-manual", "isolated-auto"):
        from . import isolated
        isolated_source = base64.b64encode(Path(isolated.__file__).read_bytes()).decode("ascii")
        program += ("import types,sys;_receiver=types.ModuleType('_choir_isolated_receiver');"
                    "exec(compile(base64.b64decode(" + repr(isolated_source) + "),'<isolated-site-receiver>','exec'),_receiver.__dict__);"
                    "sys.modules['_choir_isolated_receiver']=_receiver;")
    program += "exec(compile(base64.b64decode(" + repr(base64.b64encode(source).decode("ascii")) + "),'<site-sync-receiver>','exec'))"
    command = ["/usr/bin/ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=" + str(profile["sshTimeoutSeconds"]), "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=2", profile["sshHost"],
               shlex.quote(profile["remotePython"]) + " -c " + shlex.quote(program)]
    request = {"action": action, "profile": _remote_profile(profile)}
    if payload:
        request.update(payload)
    # A temporary input stream bounds RAM even on the first full publication.
    with tempfile.TemporaryFile() as stream:
        stream.write(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
        if archive is not None:
            with Path(archive).open("rb") as source_stream:
                shutil.copyfileobj(source_stream, stream, 1024 * 1024)
        stream.seek(0)
        completed = subprocess.run(command, stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=profile["transferTimeoutSeconds"] if timeout is None else timeout)
    lines = completed.stdout.decode("utf-8", "replace").strip().splitlines()
    try:
        response = json.loads(lines[-1]) if lines else {}
    except ValueError:
        response = {}
    if completed.returncode or response.get("ok") is not True:
        detail = response.get("error") or completed.stderr.decode("utf-8", "replace")[-2000:] or "No receiver result"
        raise RuntimeError("Site receiver failed: " + detail)
    return response["result"]


def _export(profile, prepare_original=None):
    from .export import export_site
    output_root = Path(profile["outputRoot"])
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = output_root / (".export-" + uuid.uuid4().hex)
    try:
        exported = export_site(temporary, Path(profile["libraryRoot"]),
                               prepare_original=profile.get("prepareOriginal", False) if prepare_original is None else prepare_original)
        manifest_file = temporary / "file-manifest.json"
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if manifest.get("schemaVersion") != 1:
            raise ValueError("Unsupported exported file manifest")
        entries = list(manifest["files"])
        entries.append({"path": "file-manifest.json", "bytes": manifest_file.stat().st_size,
                        "sha256": remote.digest(manifest_file)})
        remote.verify_release(temporary, entries)
        release_id = "release-" + remote.digest(manifest_file)[:20]
        destination = output_root / release_id
        if destination.exists() or destination.is_symlink():
            remote.verify_release(destination, entries)
            shutil.rmtree(temporary)
        else:
            temporary.rename(destination)
        expected = remote.revisions(json.loads((destination / "release.json").read_text(encoding="utf-8")))
        return destination, entries, expected, exported
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _plan(release, entries, expected, state):
    source = {entry["path"]: entry for entry in state["files"]}
    target = remote.file_entries(entries)
    changed = sorted(name for name, entry in target.items() if source.get(name) != entry)
    removed = sorted(set(source) - set(target))
    result = {"releaseId": release.name, "baseRelease": state["release"], "files": list(target.values()),
            "revisions": expected, "changed": changed, "removed": removed,
            "transferBytes": sum(target[name]["bytes"] for name in changed),
            "changedFiles": len(changed), "reusedFiles": len(target) - len(changed)}
    if state.get("publisherMode") == "isolated-auto":
        result["expectedGeneration"] = state["publisher"]["generation"]
    return result


def _archive(release, changed, destination):
    with tarfile.open(str(destination), "w", format=tarfile.PAX_FORMAT) as stream:
        for name in changed:
            remote.safe_name(name)
            source = release / name
            if source.is_symlink() or not remote.regular(source):
                raise ValueError("Transfer source is not a regular exported file")
            info = stream.gettarinfo(str(source), arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            info.mode = 0o444
            with source.open("rb") as content:
                stream.addfile(info, content)


def verify_editor_heads(local, remote_heads):
    """Fail closed when a local export has not incorporated online music edits."""
    from .export import validate_content_heads
    local = validate_content_heads(local)
    remote_heads = validate_content_heads(remote_heads)
    stale = sorted(key for key, head in remote_heads.items() if local.get(key) != head)
    if stale:
        raise RuntimeError("Local library has not incorporated online editor heads: " + ", ".join(stale) + "; pull and review online edits before syncing")
    return True


def sync_site(profile_path, check_only=False, ready=False, prepare_original=None, retry_failed=False):
    profile = load_profile(profile_path)
    _ready(profile, ready)
    if retry_failed and profile["publisherMode"] != "isolated-auto":
        raise ValueError("--retry-failed requires an isolated-auto publisher")
    pull = None
    if profile.get("editorSync", {}).get("enabled") and not check_only:
        from .editor_sync import pull_online_edits
        before = _call(profile, "inspect")
        if before.get("editorHeads"):
            pull = pull_online_edits(profile, before["editorHeads"])
    release, entries, expected, exported = _export(profile, prepare_original)
    state = _call(profile, "inspect")
    if "editorHeads" in state:
        protocol_path = release / "editor-protocol.json"
        protocol = json.loads(protocol_path.read_text(encoding="utf-8")) if protocol_path.is_file() else {"schemaVersion": 1, "contentHeads": {}}
        if protocol.get("schemaVersion") != 1 or set(protocol) != {"schemaVersion", "contentHeads"}:
            raise ValueError("Export has invalid editor protocol metadata")
        verify_editor_heads(protocol["contentHeads"], state["editorHeads"])
    plan = _plan(release, entries, expected, state)
    summary = {key: value for key, value in plan.items() if key not in ("files", "changed")}
    summary.update({"localRelease": str(release), "checkOnly": bool(check_only)})
    if pull is not None:
        summary["editorPull"] = pull
    if profile["publisherMode"] == "isolated-auto":
        summary.update(publisherMode="isolated-auto", publisherStatus=state["publisherStatus"],
                       pendingRequest=state["pendingRequest"])
    if check_only:
        return summary
    pending = None
    if profile["publisherMode"] == "isolated-auto":
        plan["retryFailed"] = bool(retry_failed)
        pending = _existing_auto_request(state, release.name, remote.digest(release / "file-manifest.json"), retry_failed)
        if pending is not None:
            result = dict(summary, publisherMode="isolated-auto", requestId=pending["requestId"],
                          manifestSha256=pending["manifestSha256"], pendingActivation=True,
                          requestSha256=state["pendingRequestSha256"],
                          activated=False, changed=True, reusedPendingRequest=True)
            return _wait_for_publisher(profile, result, expected)
    if not plan["changed"] and not plan["removed"]:
        guard = {"baseRelease": state["release"], "revisions": expected}
        if profile["publisherMode"] == "isolated-auto":
            guard["expectedGeneration"] = state["publisher"]["generation"]
        result = _call(profile, "check", guard)
        if result.get("release") != state["release"] or result.get("revisions") != expected:
            raise RuntimeError("Active release changed during the no-op sync; retry")
        summary.update(changed=False, health=result["health"])
        if profile["publisherMode"] == "isolated-auto":
            summary["publisherStatus"] = result.get("publisherStatus", state["publisherStatus"])
            summary["pendingRequest"] = result.get("pendingRequest", state["pendingRequest"])
        return summary
    # Export and receiver both validate the exact immutable file set.  This
    # second check closes the gap between a local plan and tar creation.
    remote.verify_release(release, entries)
    with tempfile.TemporaryDirectory(prefix="choir-site-transfer-") as temporary:
        archive = Path(temporary) / "changed.tar"
        _archive(release, plan["changed"], archive)
        plan["archiveBytes"] = archive.stat().st_size
        plan["archiveSha256"] = remote.digest(archive)
        result = _call(profile, "sync", plan, archive)
    result["localRelease"] = str(release)
    if profile["publisherMode"] == "isolated-auto":
        return _wait_for_publisher(profile, result, expected)
    return result


def _existing_auto_request(state, target, manifest_hash, retry_failed):
    from .isolated import terminal_receipt
    pending = state.get("pendingRequest")
    if pending is None:
        return None
    status = state["publisherStatus"]
    recorded = terminal_receipt(status, pending, state["pendingRequestSha256"])
    if recorded is None:
        if pending["releaseId"] != target or pending["manifestSha256"] != manifest_hash:
            raise RuntimeError("Another unresolved publisher request exists; inspect its status before replacing it")
        return pending
    if recorded["status"] in ("failed", "blocked") and not retry_failed:
        raise RuntimeError("Publisher request " + pending["requestId"] + " ended " + recorded["status"] +
                           " (" + str(recorded["errorCode"]) + "); inspect it and use --retry-failed explicitly")
    return None


def _wait_for_publisher(profile, result, expected):
    """Poll data-only receipts; never delegate service control or sudo to SSH."""
    from .isolated import terminal_receipt
    deadline = time.monotonic() + min(profile["healthTimeoutSeconds"], 90)
    request_id = result["requestId"]
    while True:
        try:
            snapshot = _call(profile, "status", timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return dict(result, pendingActivation=True, activated=False, pollTimedOut=True)
        status = snapshot["publisherStatus"]
        result.update(publisherStatus=status, pendingActivation=True, activated=False)
        if (status["requestId"] == request_id and status["status"] in ("failed", "blocked")
                and not any(row["requestId"] == request_id for row in status["recent"])):
            # Capacity/recovery failures may be unable to create a terminal
            # receipt. Report the root-owned error, but never allow that missing
            # receipt to authorize replacing the unresolved request.
            return dict(result, pendingActivation=False, publicationFailed=True,
                        errorCode=status["errorCode"], terminalReceiptMissing=True)
        recorded = terminal_receipt(status, {"requestId": request_id, "releaseId": result["releaseId"]}, result["requestSha256"])
        if recorded is not None:
            result["requestReceipt"] = recorded
            if recorded["status"] in ("failed", "blocked"):
                result.update(pendingActivation=False, publicationFailed=True, errorCode=recorded["errorCode"])
                return result
            if recorded["status"] == "succeeded":
                if (status["currentRelease"] != result["releaseId"] or
                        status["manifestSha256"] != result["manifestSha256"]):
                    raise RuntimeError("Publisher success receipt describes another target")
                checked = _call(profile, "check", {"baseRelease": str(Path(profile["remoteRoot"]) / "releases" / result["releaseId"]),
                                                     "revisions": expected, "expectedGeneration": status["generation"]})
                result.update(pendingActivation=False, activated=True, changed=recorded["changed"],
                              release=checked["release"], health=checked["health"])
                return result
        if time.monotonic() >= deadline:
            return dict(result, pollTimedOut=True)
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def check_site(profile_path, ready=False):
    profile = load_profile(profile_path)
    _ready(profile, ready)
    return _call(profile, "check")


def rollback_site(profile_path, ready=False):
    profile = load_profile(profile_path)
    _ready(profile, ready)
    return _call(profile, "rollback")
