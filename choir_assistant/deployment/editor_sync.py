"""Pull authenticated online music snapshots before exporting a local library.

Passwords are read from a private file and sent only in a same-origin HTTPS POST.
Imported bundles are verified and retained beside backups; publication is separate.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from http.client import IncompleteRead
from http.cookiejar import CookieJar
import io
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPCookieProcessor, HTTPRedirectHandler

from . import catalog
from .export import digest, local_content_heads, local_editor_metadata, validate_content_heads

MAX_ARCHIVE = 256 * 1024 * 1024
MAX_FILES = 20000
MAX_JSON = 32 * 1024 * 1024
EXPORT_RANGE_BYTES = 8 * 1024 * 1024
EXPORT_RATE_WINDOW = 61
EXPORT_TOTAL_TIMEOUT = 1200
EXPORT_SEGMENT_TIMEOUT = 120


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()


def atomic_json(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Local editor metadata must not be a link")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".editor-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded(value)); stream.flush(); os.fsync(stream.fileno())
        Path(temporary).replace(path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def validate_password_file(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        raise ValueError("Editor sync requires an absolute private password file without links")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_uid != os.getuid() or not 1 <= info.st_size <= 4096:
        raise ValueError("Editor sync password file must be owner-only (0600), regular and bounded")
    return path


def password_from_file(path):
    path = validate_password_file(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("Editor sync password file changed permissions")
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("Editor sync password is too long")
    try: password = raw.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError: raise ValueError("Editor sync password file must use UTF-8") from None
    if not password or any(c in password for c in ("\r", "\n", "\x00")):
        raise ValueError("Editor sync password file must contain one password")
    return password


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise HTTPError(request.full_url, code, "Editor sync redirects are forbidden", headers, fp)


class HTTPSPullClient:
    def __init__(self, origin, password, timeout=90, opener=None):
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Editor sync requires a credential-free HTTPS origin")
        self.origin, self.password, self.timeout = origin.rstrip("/"), password, timeout
        self.opener = opener or build_opener(NoRedirect(), HTTPCookieProcessor(CookieJar()))
        self.logged_in = set()
        self.export_requests = []

    def request(self, path, body=None, maximum=MAX_JSON):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Editor sync request must stay on its HTTPS origin")
        url = self.origin + path
        request = Request(url, data=encoded(body) if body is not None else None,
                          headers={"Accept": "application/json, application/x-tar", "Content-Type": "application/json", "Origin": self.origin,
                                   "User-Agent": "ChoirAssistant-EditorSync/1"}, method="POST" if body is not None else "GET")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.geturl() != url or response.status != 200:
                    raise ValueError("Editor sync response changed origin or status")
                lengths = response.headers.get_all("Content-Length", [])
                if len(lengths) > 1:
                    raise ValueError("Editor sync response has ambiguous Content-Length")
                declared = None
                if lengths:
                    value = lengths[0].strip()
                    if not value or not value.isascii() or not value.isdigit():
                        raise ValueError("Editor sync response has invalid Content-Length")
                    declared = int(value)
                    if declared > maximum:
                        raise ValueError("Editor sync response exceeds its size limit")
                deadline = time.monotonic() + (300 if maximum == MAX_ARCHIVE else self.timeout)
                output = io.BytesIO()
                total = 0
                read_block = getattr(response, "read1", response.read)
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Editor sync response deadline exceeded")
                    # read1 returns after one underlying buffered read. A peer
                    # trickling bytes cannot keep us waiting to fill 256 KiB
                    # without another check of the complete-transfer deadline.
                    block = read_block(min(256 * 1024, maximum + 1 - total))
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Editor sync response deadline exceeded")
                    if not block:
                        break
                    total += len(block)
                    if total > maximum:
                        raise ValueError("Editor sync response exceeds its size limit")
                    output.write(block)
                if declared is not None and total != declared:
                    raise RuntimeError("Editor sync HTTPS response was truncated (expected %s bytes, received %s)" % (declared, total))
                raw = output.getvalue()
        except (HTTPError, URLError, IncompleteRead, TimeoutError) as error:
            # Do not stringify requests, request bodies, cookies or credentials.
            code = getattr(error, "code", None)
            raise RuntimeError("Editor sync HTTPS request failed" + (" (HTTP " + str(code) + ")" if code else "")) from None
        if len(raw) > maximum:
            raise ValueError("Editor sync response exceeds its size limit")
        return raw

    def download(self, work, movement, head, destination):
        validate_content_heads({work + "/" + movement: head})
        if work not in self.logged_in:
            config = json.loads(self.request("/api/site-config"))
            token = config.get("csrfToken")
            if not isinstance(token, str) or not token:
                raise ValueError("Editor sync did not receive a CSRF token")
            result = json.loads(self.request("/api/login", {"work": work, "password": self.password, "csrfToken": token}))
            if result.get("authenticated") is not True:
                raise RuntimeError("Editor sync login was not accepted")
            self.logged_in.add(work)
        path = "/w/" + quote(work, safe="") + "/" + quote(movement, safe="") + "/api/editor/export?head=" + head
        destination = Path(destination)
        if destination.is_symlink():
            raise ValueError("Editor snapshot destination must not be a link")
        deadline = time.monotonic() + EXPORT_TOTAL_TIMEOUT
        descriptor, temporary = tempfile.mkstemp(prefix=".editor-export-", dir=destination.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                received, total = 0, None
                while total is None or received < total:
                    end = received + EXPORT_RANGE_BYTES - 1 if total is None else min(received + EXPORT_RANGE_BYTES, total) - 1
                    block, observed = self.range_segment(path, received, end, total, deadline)
                    total = observed
                    output.write(block); received += len(block)
                    print(json.dumps({"editorSyncBytes": received, "totalBytes": total}), file=sys.stderr, flush=True)
                output.flush(); os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink()

    @staticmethod
    def wait_export(seconds, deadline):
        until = time.monotonic() + seconds
        while True:
            now = time.monotonic()
            if now >= until:
                return
            remaining = deadline - now
            if remaining <= 0:
                raise RuntimeError("Editor sync export exceeded its complete-download deadline")
            time.sleep(min(5, until - now, remaining))

    def reserve_export(self, deadline):
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise RuntimeError("Editor sync export exceeded its complete-download deadline")
            self.export_requests = [stamp for stamp in self.export_requests if now - stamp < EXPORT_RATE_WINDOW]
            if len(self.export_requests) < 3:
                self.export_requests.append(now)
                return
            self.wait_export(EXPORT_RATE_WINDOW - (now - self.export_requests[0]), deadline)

    def range_segment(self, path, start, end, expected_total, deadline):
        """Retry only a discarded segment; never accept a changed immutable head."""
        url, rate_retried = self.origin + path, False
        for attempt in range(3):
            self.reserve_export(deadline)
            segment_deadline = min(deadline, time.monotonic() + EXPORT_SEGMENT_TIMEOUT)
            request = Request(url, headers={"Accept": "application/x-tar", "Origin": self.origin,
                "User-Agent": "ChoirAssistant-EditorSync/1", "Range": "bytes=%s-%s" % (start, end)})
            try:
                with self.opener.open(request, timeout=max(.001, min(EXPORT_SEGMENT_TIMEOUT, deadline - time.monotonic()))) as response:
                    if response.geturl() != url or response.status != 206:
                        raise ValueError("Editor sync server did not honor the fixed-head byte range")
                    ranges = response.headers.get_all("Content-Range", [])
                    lengths = response.headers.get_all("Content-Length", [])
                    if len(ranges) != 1 or len(lengths) != 1:
                        raise ValueError("Editor sync range has missing or ambiguous framing")
                    import re
                    match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", ranges[0].strip())
                    if not match or not lengths[0].strip().isascii() or not lengths[0].strip().isdigit():
                        raise ValueError("Editor sync range framing is invalid")
                    first, last, total = map(int, match.groups())
                    if (not 0 < total <= MAX_ARCHIVE or first != start or last != min(end, total - 1)
                            or last < first or (expected_total is not None and total != expected_total)
                            or int(lengths[0]) != last - first + 1):
                        raise ValueError("Editor sync range or archive total changed")
                    if expected_total is None:
                        expected_total = total
                    wanted, output = last - first + 1, io.BytesIO()
                    read = getattr(response, "read1", response.read)
                    while output.tell() < wanted:
                        if time.monotonic() >= segment_deadline:
                            raise TimeoutError()
                        block = read(min(256 * 1024, wanted - output.tell()))
                        if time.monotonic() >= segment_deadline:
                            raise TimeoutError()
                        if not block:
                            raise IncompleteRead(b"")
                        if len(block) > wanted - output.tell():
                            raise ValueError("Editor sync range body exceeds its declared length")
                        output.write(block)
                    if output.tell() != wanted:
                        raise ValueError("Editor sync range body exceeds its declared length")
                    return output.getvalue(), total
            except HTTPError as error:
                code = error.code; error.close()
                if code == 409:
                    raise RuntimeError("Online editor head changed during download (HTTP 409); refresh before syncing") from None
                if code == 429 and not rate_retried and attempt < 2:
                    rate_retried = True
                    self.wait_export(EXPORT_RATE_WINDOW, deadline)
                    continue
                raise RuntimeError("Editor sync HTTPS range request failed (HTTP %s)" % code) from None
            except (URLError, OSError, IncompleteRead):
                if attempt == 2:
                    raise RuntimeError("Editor sync HTTPS range segment failed after bounded retries") from None
        raise RuntimeError("Editor sync HTTPS range segment could not complete")


def safe_member(name):
    if not isinstance(name, str):
        raise ValueError("Unsafe editor snapshot archive path")
    path = PurePosixPath(name)
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name or path.is_absolute() or any(p in (".", "..") for p in path.parts) or path.as_posix() != name:
        raise ValueError("Unsafe editor snapshot archive path")
    if name not in ("editor-export.json", "head.json", "commit.json", "ledger.json") and not name.startswith("bundle/"):
        raise ValueError("Unexpected editor snapshot archive member")
    return path


def load_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_JSON:
        raise ValueError("Editor snapshot JSON exceeds its size limit")
    return json.loads(path.read_text(encoding="utf-8"))


def unpack_snapshot(archive_path, destination, work, movement, expected_head, source_hash):
    """Extract only bounded regular files, then bind descriptor -> commit -> bundle."""
    validate_content_heads({work + "/" + movement: expected_head})
    destination = Path(destination); destination.mkdir(parents=True, exist_ok=False)
    if Path(archive_path).stat().st_size > MAX_ARCHIVE:
        raise ValueError("Editor snapshot archive is too large")
    seen, total = set(), 0
    with tarfile.open(archive_path, mode="r:") as archive:
        for item in archive:
            path = safe_member(item.name)
            if item.name in seen or not item.isfile() or item.issym() or item.islnk() or item.size < 0:
                raise ValueError("Editor archive contains duplicate or nonregular members")
            seen.add(item.name); total += item.size
            if len(seen) > MAX_FILES or total > MAX_ARCHIVE:
                raise ValueError("Editor snapshot extraction exceeds its limit")
            target = destination.joinpath(*path.parts); target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(item) as incoming, target.open("xb") as output:
                shutil.copyfileobj(incoming, output, 1024 * 1024)
            if target.stat().st_size != item.size:
                raise ValueError("Truncated editor archive member")
    descriptor = load_json(destination / "editor-export.json")
    if descriptor.get("schemaVersion") != 1 or any(descriptor.get(k) != v for k, v in {"work":work,"movement":movement,"editHead":expected_head,"sourceSha256":source_hash}.items()):
        raise ValueError("Editor snapshot descriptor does not match requested score, head or source PDF")
    indexed = {}
    for entry in descriptor.get("files", []):
        if not isinstance(entry, dict): raise ValueError("Invalid editor snapshot index")
        name = entry.get("path"); safe_member(name)
        if name == "editor-export.json" or name in indexed or type(entry.get("bytes")) is not int or entry["bytes"] < 0:
            raise ValueError("Invalid or duplicate editor snapshot index")
        target = destination / name
        if name not in seen or target.stat().st_size != entry["bytes"] or digest(target) != entry.get("sha256"):
            raise ValueError("Editor snapshot file size or SHA-256 mismatch")
        indexed[name] = entry
    if set(indexed) != seen - {"editor-export.json"} or not {"head.json","commit.json","ledger.json","bundle/manifest.json","bundle/assets/interactive.json"} <= seen:
        raise ValueError("Editor snapshot has missing or unindexed files")
    head, commit, ledger = (load_json(destination / name) for name in ("head.json","commit.json","ledger.json"))
    unsigned = {key:value for key,value in commit.items() if key != "editHead"}
    if (head != {key:value for key,value in commit.items() if key != "ledger"} or commit.get("editHead") != expected_head
            or sha256(encoded(unsigned)).hexdigest() != expected_head or commit.get("work") != work or commit.get("movement") != movement
            or commit.get("ledger") != ledger or sha256(encoded(ledger)).hexdigest() != commit.get("ledgerSha256")):
        raise ValueError("Editor snapshot head, commit or ledger binding mismatch")
    bundle = destination / "bundle"; manifest = load_json(bundle / "manifest.json"); model = load_json(bundle / "assets/interactive.json")
    manifest_hash = digest(bundle / "manifest.json")
    if (manifest_hash != descriptor.get("bundleManifestSha256") or manifest_hash != commit.get("bundleManifestSha256")
            or model.get("revision") != manifest.get("revision") or model.get("revision") != descriptor.get("revision") or model.get("revision") != commit.get("revision")
            or commit.get("sourceSha256") != source_hash or manifest.get("status") not in ("complete", "complete-with-open-issues")):
        raise ValueError("Editor snapshot bundle, revision or source binding mismatch")
    artifacts = {}
    for item in manifest.get("artifacts", []):
        relative = item.get("path")
        if not isinstance(relative, str): raise ValueError("Invalid bundle artifact path")
        safe_member("bundle/" + relative)
        if relative in artifacts or "bundle/" + relative not in indexed or digest(bundle / relative) != item.get("sha256"):
            raise ValueError("Editor bundle artifact index mismatch")
        artifacts[relative] = item
    assets = {name[len("bundle/"):] for name in seen if name.startswith("bundle/assets/")}
    if not assets <= set(artifacts):
        raise ValueError("Editor bundle contains unindexed musical assets")
    frozen = manifest.get("sourcePdf")
    if frozen:
        relative = frozen.get("path")
        if not isinstance(relative,str): raise ValueError("Invalid frozen PDF path")
        safe_member("bundle/" + relative)
        if frozen.get("sha256") != source_hash or relative not in artifacts or digest(bundle / relative) != source_hash:
            raise ValueError("Editor snapshot frozen PDF differs from original")
    # Model provenance.sourceSha256 identifies MusicXML, not the original PDF.
    return {"descriptor":descriptor,"commit":commit,"model":model,"manifest":manifest,"bundle":bundle}


@contextmanager
def library_lock(library):
    path = library / ".online-editor-sync.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor,"a+") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise ValueError("Invalid editor sync lock")
        try: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError("Another editor library pull is running") from None
        try: yield
        finally: fcntl.flock(stream, fcntl.LOCK_UN)


def notation_topology(model):
    """Fields which give measure-bound annotations their musical address."""
    fields = ("id", "part", "sourcePart", "measureIndex", "staff", "voice")
    if any(not isinstance(model.get(name), list) for name in ("measures", "parts", "notes", "rests")):
        raise ValueError("Notation overlay needs an explicit musical topology")
    if any(not isinstance(row, dict) for name in ("measures", "parts", "notes", "rests") for row in model[name]):
        raise ValueError("Invalid notation overlay musical topology")
    return {"measures": [{key: row.get(key) for key in ("index", "number")} for row in model["measures"]],
            "parts": model["parts"], "sourceParts": model.get("sourceParts"),
            "notes": [{key: row.get(key) for key in fields} for row in model["notes"]],
            "rests": [{key: row.get(key) for key in fields} for row in model["rests"]]}


def rebound_notation_overlay(target, library, current_bundle, model, source_hash):
    """Retain reviewed display evidence only across the same musical addresses.

    The local sidecar is deliberately outside the immutable musical bundle.
    Site export still performs its full lyric/page validation for the new revision.
    """
    value = target.get("notationOverlay")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("Notation overlay path is invalid")
    source = Path(value).expanduser()
    if not source.is_absolute(): source = library / source
    if source.resolve() != source or source.is_symlink():
        raise ValueError("Notation overlay must not redirect through links")
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4 * 1024 * 1024:
            raise ValueError("Notation overlay must be a bounded regular file without links")
        original = stream.read(4 * 1024 * 1024 + 1)
    if len(original) > 4 * 1024 * 1024:
        raise ValueError("Notation overlay exceeds its size limit")
    overlay = json.loads(original)
    old_model_path = current_bundle / "assets/interactive.json"
    old_model, manifest = load_json(old_model_path), load_json(current_bundle / "manifest.json")
    indexed = {item.get("path"): item.get("sha256") for item in manifest.get("artifacts", [])}
    if old_model.get("revision") != manifest.get("revision") or indexed.get("assets/interactive.json") != digest(old_model_path):
        raise ValueError("Notation overlay base model does not match its immutable bundle")
    if (not isinstance(overlay, dict) or overlay.get("schemaVersion") != 1
            or overlay.get("revision") != old_model.get("revision") or overlay.get("sourceSha256") != source_hash):
        raise ValueError("Notation overlay revision or original-PDF identity mismatch")
    topology = notation_topology(old_model)
    if topology != notation_topology(model):
        raise ValueError("Notation overlay musical topology changed; review annotations before syncing")
    rebound = deepcopy(overlay)
    rebound.update(revision=model["revision"], derivedFromRevision=overlay["revision"],
                   derivation="measure-bound display annotations; source bytes preserved",
                   sourceOverlaySha256=sha256(original).hexdigest(),
                   measureTopologySha256=sha256(encoded(topology)).hexdigest())
    return encoded(rebound)


def retain_overlay(path, content):
    """Rehydrate an import without replacing a conflicting or linked sidecar."""
    if path.resolve() != path or path.is_symlink():
        raise ValueError("Existing online notation overlay must not redirect through links")
    if path.exists() or path.is_symlink():
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != len(content)
                    or stream.read(len(content) + 1) != content):
                raise ValueError("Existing online notation overlay conflicts with its immutable derivation")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content); stream.flush(); os.fsync(stream.fileno())


def pull_online_edits(profile, remote_heads, client=None):
    """Import complete score snapshots, switch entry pointers, then write watermarks."""
    remote_heads = validate_content_heads(remote_heads)
    library = Path(profile["libraryRoot"]).resolve()
    with library_lock(library):
        local, bindings = local_editor_metadata(library)
        changed = {key:head for key,head in remote_heads.items() if local.get(key) != head}
        if not changed: return {"pulled":[]}
        records = catalog.entries(library)
        initial_entry_hashes = {}
        for scope in changed:
            work = scope.split("/")[0]
            paths = [library/folder/work/"entry.json" for folder in ("works","作品") if (library/folder/work/"entry.json").exists()]
            if len(paths) != 1 or paths[0].is_symlink() or paths[0].resolve() != paths[0]:
                raise ValueError("Local work entry path is ambiguous or redirects")
            initial_entry_hashes[work] = digest(paths[0])
        if client is None:
            password = password_from_file(profile["editorSync"]["passwordFile"])
            client = HTTPSPullClient(profile["publicUrl"], password, profile.get("healthTimeoutSeconds",90))
        root = library / ".online-edits"
        if root.is_symlink() or (root.exists() and root.resolve() != root): raise ValueError("Online import directory must not be a link")
        root.mkdir(exist_ok=True)
        staged, updates = [], {}
        with tempfile.TemporaryDirectory(prefix=".pull-",dir=root) as temporary:
            temporary = Path(temporary)
            for key,head in sorted(changed.items()):
                work,movement = key.split("/")
                entry = records.get(work)
                if not entry: raise ValueError("Online editor work is not registered locally: " + work)
                target = next((m for m in entry["movements"] if m["id"] == movement),None)
                if target is None: raise ValueError("Online editor movement is not registered locally: " + key)
                current_bundle, project = catalog.active_bundle(target,library)
                _, source_hash = catalog.source_pdf(current_bundle,project,entry,library)
                archive = temporary / (work + "--" + movement + ".tar")
                client.download(work,movement,head,archive)
                extracted = temporary / (work + "--" + movement)
                result = unpack_snapshot(archive,extracted,work,movement,head,source_hash)
                overlay = rebound_notation_overlay(target,library,current_bundle,result["model"],source_hash)
                destination = root / work / movement / head
                if destination.resolve() != destination: raise ValueError("Online import path redirects")
                # Existing immutable imports are verified again, never overwritten.
                if destination.exists() or destination.is_symlink():
                    if destination.is_symlink() or digest(destination / "bundle/manifest.json") != result["descriptor"]["bundleManifestSha256"]:
                        raise ValueError("Existing online snapshot conflicts with its immutable identity")
                    if overlay is not None: retain_overlay(destination / "notation-overlay.json",overlay)
                else:
                    destination.parent.mkdir(parents=True,exist_ok=True)
                    if destination.parent.resolve() != destination.parent: raise ValueError("Online import path redirects")
                    if overlay is not None: retain_overlay(extracted / "notation-overlay.json",overlay)
                    extracted.rename(destination)
                updated = updates.setdefault(work,deepcopy(entry))
                m = next(m for m in updated["movements"] if m["id"] == movement)
                m.update(bundle=str(destination / "bundle"),revision=result["model"]["revision"],onlineEditHead=head,sourceReviewed=False,sourceReviewStatus="online-edited-needs-source-review")
                if overlay is not None: m["notationOverlay"] = str(destination / "notation-overlay.json")
                m.pop("reviewReport",None)
                updated.update(sourceReviewed=False,status="draft-needs-review")
                updated.pop("reviewReport",None)
                staged.append((key,head,result,destination))
            backup = root / "backups" / uuid.uuid4().hex
            if backup.resolve() != backup: raise ValueError("Online backup path redirects")
            backup.mkdir(parents=True)
            entry_paths = {}
            for work in updates:
                paths = [library/folder/work/"entry.json" for folder in ("works","作品") if (library/folder/work/"entry.json").exists()]
                if len(paths) != 1 or paths[0].is_symlink() or paths[0].resolve() != paths[0]: raise ValueError("Local work entry path is ambiguous or redirects")
                if digest(paths[0]) != initial_entry_hashes[work]:
                    raise RuntimeError("Local library entry changed while pulling online edits; retry")
                entry_paths[work] = paths[0]
                target = backup / paths[0].relative_to(library);target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(paths[0],target)
            cached_catalog = library / "catalog.json"
            cache = None
            if cached_catalog.exists():
                if cached_catalog.is_symlink(): raise ValueError("Local catalog must not be a link")
                cache = load_json(cached_catalog)
                if (not isinstance(cache,dict) or not isinstance(cache.get("works"),list)
                        or any(not isinstance(w,dict) or not isinstance(w.get("movements",[]),list)
                               or any(not isinstance(m,dict) for m in w.get("movements",[])) for w in cache["works"])):
                    raise ValueError("Local cached catalog has an unsupported schema")
                shutil.copyfile(cached_catalog,backup/"catalog.json")
            watermark = library / ".online-editor-heads.json"
            if watermark.exists(): shutil.copyfile(watermark,backup/"online-editor-heads.json")
            atomic_json(backup/"pull.json",{"schemaVersion":1,"heads":changed,"entries":list(updates)})
            if cache is not None:
                for work in cache.get("works",[]):
                    if work.get("id") not in updates: continue
                    for key,head,result,destination in staged:
                        wid,mid=key.split("/")
                        if wid != work["id"]: continue
                        row=next((m for m in work.get("movements",[]) if m.get("id")==mid),None)
                        if row is not None:
                            source=next(m for m in updates[wid]["movements"] if m["id"]==mid)
                            row.update(catalog.movement_summary(source,result["manifest"],result["model"]));row.update(bundle=str(destination/"bundle"),onlineEditHead=head,sourceReviewed=False)
                            row.pop("reviewReport",None)
                    for field in ("notes","rests","measures","duration","blockingMeasures","openIssues"):
                        if field in work:
                            work[field]=sum(m.get(field,0) for m in work.get("movements",[]) if isinstance(m.get(field,0),(int,float)))
                    if "parts" in work:
                        work["parts"]=list(dict.fromkeys(part for m in work.get("movements",[]) for part in m.get("parts",[])))
                    work.pop("reviewReport",None)
                    work["sourceReviewed"]=False;work["status"]="draft-needs-review"
                for field in ("notes","rests","measures","duration","blockingMeasures","openIssues"):
                    if field in cache.get("totals",{}):
                        cache["totals"][field]=sum(w.get(field,0) for w in cache.get("works",[]) if isinstance(w.get(field,0),(int,float)))
            for work,entry in updates.items(): atomic_json(entry_paths[work],entry)
            if cache is not None: atomic_json(cached_catalog,cache)
            local.update(changed)
            for key,head,result,destination in staged:
                bindings[key] = {"revision":result["model"]["revision"],"bundleManifestSha256":result["descriptor"]["bundleManifestSha256"]}
            atomic_json(watermark,{"schemaVersion":1,"contentHeads":local,"bindings":bindings})
        return {"pulled":list(changed),"backup":str(backup)}
