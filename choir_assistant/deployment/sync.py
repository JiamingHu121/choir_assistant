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
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", value.get("launchdLabel", "")):
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
    library, output = Path(value["libraryRoot"]), Path(value["outputRoot"])
    if library == output or library in output.parents and (output / "catalog.json").exists():
        raise ValueError("Output cannot replace the library itself")
    return value


def _ready(profile, ready):
    if profile.get("requireReady", False) and not ready:
        raise ValueError("This deployment profile requires --ready before any server connection")


def _remote_profile(profile):
    return {key: profile[key] for key in ("remoteRoot", "remotePython", "launchdLabel", "publicUrl",
                                         "bind", "port", "healthTimeoutSeconds")}


def _call(profile, action, payload=None, archive=None):
    """Run only the bundled receiver; request data travels on stdin, never shell interpolation."""
    source = Path(remote.__file__).read_bytes()
    program = "import base64;exec(compile(base64.b64decode(" + repr(base64.b64encode(source).decode("ascii")) + "),'<site-sync-receiver>','exec'))"
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
                                   timeout=profile["transferTimeoutSeconds"])
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
    return {"releaseId": release.name, "baseRelease": state["release"], "files": list(target.values()),
            "revisions": expected, "changed": changed, "removed": removed,
            "transferBytes": sum(target[name]["bytes"] for name in changed),
            "changedFiles": len(changed), "reusedFiles": len(target) - len(changed)}


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


def sync_site(profile_path, check_only=False, ready=False, prepare_original=None):
    profile = load_profile(profile_path)
    _ready(profile, ready)
    release, entries, expected, exported = _export(profile, prepare_original)
    state = _call(profile, "inspect")
    plan = _plan(release, entries, expected, state)
    summary = {key: value for key, value in plan.items() if key not in ("files", "changed")}
    summary.update({"localRelease": str(release), "checkOnly": bool(check_only)})
    if check_only:
        return summary
    if not plan["changed"] and not plan["removed"]:
        result = _call(profile, "check", {"baseRelease": state["release"], "revisions": expected})
        if result.get("release") != state["release"] or result.get("revisions") != expected:
            raise RuntimeError("Active release changed during the no-op sync; retry")
        return dict(summary, changed=False, health=result["health"])
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
    return result


def check_site(profile_path, ready=False):
    profile = load_profile(profile_path)
    _ready(profile, ready)
    return _call(profile, "check")


def rollback_site(profile_path, ready=False):
    profile = load_profile(profile_path)
    _ready(profile, ready)
    return _call(profile, "rollback")
