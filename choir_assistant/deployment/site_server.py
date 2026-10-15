"""Serve an exported, immutable choir library on one origin (Python 3.8+).

Only mido and the copied playback/validation modules are needed at runtime.
Source recognition, score editing, and engraving stay on the authoring machine.
"""
from __future__ import annotations

import argparse
import base64
import hmac
import ipaddress
import secrets
from http.cookies import SimpleCookie
from collections import OrderedDict
from contextlib import closing
from hashlib import sha256, pbkdf2_hmac
import importlib.util
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

# Imported frozen runtime modules must never add unlisted bytecode assets.
sys.dont_write_bytecode = True


UI_FILES = {
    "index.html", "entry.js", "entry.css", "calibration_ui.js", "calibration_ui.css",
    "app.js", "style.css", "interactive.js", "interactive.css", "console.js",
    "console.css", "metronome.js", "pitch_core.js", "pitch.js", "pitch.css",
    "pitch-worklet.js", "pitch-worker.js", "pitch-reference-worklet.js",
}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
GENERATED_PATH = re.compile(r"[a-f0-9]{32}/mix\.(?:wav|mid|mp3)\Z")


class AuthenticationRequired(ValueError):
    pass


class ResourceBusy(ValueError):
    """A public, retryable resource budget refusal, not an authorization error."""


def file_hash(path):
    result = sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(data)
    return result.hexdigest()


def verify_release(root):
    """Reject missing, changed, unlisted or symlinked release files on startup."""
    manifest = read_json(root / "file-manifest.json")
    if manifest.get("schemaVersion") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError("Invalid release file manifest")
    expected = set()
    for item in manifest["files"]:
        name = item.get("path")
        if not isinstance(name, str) or not name or name in expected or name == "file-manifest.json":
            raise ValueError("Invalid or duplicate release path")
        target = inside(root, name)
        if not target.is_file() or target.stat().st_size != item.get("bytes") or file_hash(target) != item.get("sha256"):
            raise ValueError("Release file hash/size mismatch")
        expected.add(name)
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Release must not contain symlinks")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != expected | {"file-manifest.json"}:
        raise ValueError("Release contains unlisted or missing files")


def public_projection(value):
    """Remove authoring filesystem locations from public JSON views.

    Canonical frozen files keep their original hashes inside the release. The
    HTTP model is explicitly a public projection of that musical structure.
    """
    if isinstance(value, dict):
        return {key: public_projection(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_projection(item) for item in value]
    if isinstance(value, str) and (value.startswith("file://") or re.match(r"^[A-Za-z]:[\\/]", value)
                                   or (value.startswith("/") and not value.startswith(("/assets/", "/original/", "/generated/")))):
        return "[authoring-path-redacted]"
    return value


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def inside(root, relative):
    """Resolve manifest/static paths, rejecting traversal and escaping symlinks."""
    root = Path(root).resolve()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("文件路径无效")
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError("文件路径无效")
    return target


def web_projection(value, prefix):
    """Change only browser URLs; source provenance and frozen bytes stay intact."""
    if isinstance(value, dict):
        return {key: web_projection(item, prefix) for key, item in value.items()}
    if isinstance(value, list):
        return [web_projection(item, prefix) for item in value]
    if isinstance(value, str) and value.startswith(("/assets/", "/original/", "/generated/")):
        return prefix + value[1:]
    return value


def derive_ui(content, prefix, filename):
    """Scope the existing UI's quoted absolute URLs without changing its files."""
    root_names = "|".join(re.escape(name) for name in UI_FILES)
    pattern = r"([\"'`])/(?=(?:assets/|original/|generated/|api/|(?:" + root_names + r")(?=[\"'`?#])))"
    content = re.sub(pattern, lambda match: match.group(1) + prefix, content)
    if filename == "index.html":
        content = content.replace("本地工作空间", "已发布曲库")
        content = content.replace("</head>", "<style>.notation-editor{display:none!important}</style></head>")
    return content.encode("utf-8")


def fit_wav_duration(path, duration):
    """Trim/pad PCM frames without silence detection or loss of leading rests."""
    source = Path(path)
    target = source.with_name(source.stem + ".duration.wav")
    with wave.open(str(source), "rb") as reader:
        if reader.getcomptype() != "NONE":
            raise ValueError("合成音频必须为 PCM WAV")
        params = reader.getparams()
        wanted = max(0, round(float(duration) * params.framerate))
        remaining = wanted
        frame_size = params.nchannels * params.sampwidth
        with wave.open(str(target), "wb") as writer:
            writer.setparams(params)
            while remaining:
                count = min(65536, remaining)
                data = reader.readframes(count)
                frames = len(data) // frame_size
                writer.writeframesraw(data)
                remaining -= frames
                if frames < count:
                    # PCM unsigned 8-bit silence is 128; other supported PCM is signed.
                    zero = b"\x80" if params.sampwidth == 1 else b"\0"
                    while remaining:
                        frames = min(65536, remaining)
                        writer.writeframesraw(zero * (frames * frame_size))
                        remaining -= frames
    target.replace(source)


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module



class SiteAccess:
    """Private, deployment-owned identity settings, never part of an export."""
    def __init__(self, config, runtime, release_root):
        self.config = {}
        if config:
            path = Path(config).resolve()
            try:
                path.relative_to(release_root)
            except ValueError:
                pass
            else:
                raise ValueError("站点私有配置必须位于冻结发布目录之外")
            if not path.is_file() or Path(config).is_symlink() or path.stat().st_mode & 0o037:
                raise ValueError("站点私有配置须为独立普通文件，权限为 0600 或可信服务组只读的 0640")
            self.config = read_json(path)
            if not isinstance(self.config, dict) or self.config.get("schemaVersion") != 1:
                raise ValueError("站点私有配置版本无效")
        self.title = self.config.get("siteTitle", "合唱曲库")
        self.github = self.config.get("githubUrl", "")
        if not isinstance(self.title, str) or not 1 <= len(self.title) <= 80:
            raise ValueError("站点标题无效")
        parsed = urlsplit(self.github)
        if self.github and (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password):
            raise ValueError("项目链接须为 HTTPS 地址")
        auth = self.config.get("auth")
        self.enabled = auth is not None
        self.internal_health_token = self.config.get("internalHealthToken", "")
        if self.enabled and (not isinstance(self.internal_health_token, str) or not 32 <= len(self.internal_health_token) <= 256):
            raise ValueError("启用认证时须配置独立的内部健康检查令牌")
        self.key = secrets.token_bytes(32)
        self.session_seconds = 43200
        if self.enabled:
            try:
                self.salt = bytes.fromhex(auth["salt"])
                self.password_hash = bytes.fromhex(auth["passwordHash"])
                self.key = bytes.fromhex(auth["cookieSigningKey"])
                self.iterations = auth.get("iterations", 310000)
                self.session_seconds = auth.get("sessionSeconds", 43200)
                assert 16 <= len(self.salt) <= 64 and len(self.password_hash) == 32 and len(self.key) == 32
                assert type(self.iterations) is int and 100000 <= self.iterations <= 2000000
                assert type(self.session_seconds) is int and 300 <= self.session_seconds <= 604800
            except (KeyError, TypeError, ValueError, AssertionError):
                raise ValueError("站点认证配置无效") from None
        feedback = self.config.get("feedback", {})
        self.feedback_bytes = feedback.get("maxBytes", 10 * 1024 * 1024)
        self.feedback_entries = feedback.get("maxEntries", 2000)
        if type(self.feedback_bytes) is not int or not 65536 <= self.feedback_bytes <= 100 * 1024 * 1024:
            raise ValueError("反馈容量无效")
        if type(self.feedback_entries) is not int or not 1 <= self.feedback_entries <= 100000:
            raise ValueError("反馈数量上限无效")
        self.feedback_path = runtime / "feedback.sqlite3"
        self.guard = threading.Lock()
        self.rates = OrderedDict()
        self.password_slots = threading.BoundedSemaphore(2)
        self.feedback_enabled = bool(config)
        if self.feedback_enabled:
            # No score data, cookies, passwords, request headers or client IPs are stored.
            with closing(sqlite3.connect(str(self.feedback_path))) as db:
                db.execute("CREATE TABLE IF NOT EXISTS feedback (id TEXT PRIMARY KEY, created INTEGER NOT NULL, payload TEXT NOT NULL, bytes INTEGER NOT NULL)")
                db.commit()
            os.chmod(str(self.feedback_path), 0o600)

    @staticmethod
    def cookie_name(work):
        return "__Host-choir-work-" + sha256(work.encode()).hexdigest()[:20]

    def token(self, purpose, subject, seconds):
        value = json.dumps([purpose, subject, int(time.time()) + seconds, secrets.token_hex(16)], separators=(",", ":")).encode()
        payload = base64.urlsafe_b64encode(value).rstrip(b"=").decode()
        return payload + "." + hmac.new(self.key, payload.encode(), sha256).hexdigest()

    def valid_token(self, value, purpose, subject):
        try:
            payload, signature = value.split(".", 1)
            expected = hmac.new(self.key, payload.encode(), sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return False
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            return (isinstance(data, list) and len(data) == 4 and isinstance(data[3], str) and len(data[3]) == 32 and data[0] == purpose and data[1] == subject
                    and type(data[2]) is int and time.time() < data[2] <= time.time() + 604801)
        except (ValueError, TypeError, UnicodeError):
            return False

    def rate(self, bucket, limit, window):
        now = time.monotonic()
        with self.guard:
            while self.rates and (len(self.rates) >= 4096 or next(iter(self.rates.values()))[0] <= now - 3600):
                self.rates.popitem(last=False)
            old, count = self.rates.get(bucket, (now, 0))
            if now - old >= window:
                old, count = now, 0
            if count >= limit:
                raise ResourceBusy("请求过于频繁，请稍后再试")
            self.rates[bucket] = old, count + 1
            self.rates.move_to_end(bucket)

    def password_matches(self, password, client):
        self.rate("login-ip:" + client, 8, 900)
        self.rate("login-global", 120, 60)
        if not self.password_slots.acquire(blocking=False):
            raise ResourceBusy("认证繁忙，请稍后重试")
        try:
            if not isinstance(password, str) or not 1 <= len(password) <= 256:
                return False
            candidate = pbkdf2_hmac("sha256", password.encode("utf-8"), self.salt, self.iterations)
            return hmac.compare_digest(candidate, self.password_hash)
        finally:
            self.password_slots.release()

    def feedback(self, data, client, works):
        if not self.feedback_enabled:
            raise ValueError("站点尚未启用反馈")
        self.rate("feedback-ip:" + client, 5, 600)
        self.rate("feedback-global", 60, 600)
        text = data.get("text")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000:
            raise ValueError("反馈内容须为 1–4000 个字符")
        payload = {"text": text.strip()}
        for key, length in (("work", 128), ("movement", 128), ("measure", 80), ("position", 80)):
            value = data.get(key, "")
            if not isinstance(value, str) or len(value) > length or any(ord(c) < 32 for c in value):
                raise ValueError("反馈位置无效")
            payload[key] = value
        if payload["work"] and payload["work"] not in works:
            raise ValueError("反馈曲目不存在")
        raw = json.dumps(payload, ensure_ascii=False)
        size = len(raw.encode("utf-8"))
        ident = uuid.uuid4().hex
        with self.guard, closing(sqlite3.connect(str(self.feedback_path))) as db:
            db.execute("PRAGMA max_page_count={}".format(self.feedback_bytes // 4096))
            count, total = db.execute("SELECT count(*), coalesce(sum(bytes), 0) FROM feedback").fetchone()
            if count >= self.feedback_entries or total + size > self.feedback_bytes // 2:
                raise ResourceBusy("反馈箱已满，请稍后重试")
            try:
                db.execute("INSERT INTO feedback VALUES (?, ?, ?, ?)", (ident, int(time.time()), raw, size))
                db.commit()
            except sqlite3.OperationalError:
                raise ResourceBusy("反馈箱暂不可用，请稍后重试") from None
        return ident


class ScoreSite:
    def __init__(self, root, runtime, fluidsynth=None, soundfont=None, ffmpeg=None, origins=(),
                 max_render_seconds=600, cache_bytes=512 * 1024 * 1024, cache_ttl=3600,
                 cache_grace=120, min_render_interval=2, site_config=None):
        self.root = Path(root).resolve()
        verify_release(self.root)
        self.runtime = Path(runtime).resolve()
        try:
            self.runtime.relative_to(self.root)
        except ValueError:
            pass
        else:
            raise ValueError("运行缓存必须位于冻结站点目录之外")
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.access = SiteAccess(site_config, self.runtime, self.root)
        for label, value, minimum, maximum in (
            ("max_render_seconds", max_render_seconds, 1, 1800),
            ("cache_bytes", cache_bytes, 1024 * 1024, 10 * 1024 ** 3),
            ("cache_ttl", cache_ttl, 60, 86400),
            ("cache_grace", cache_grace, 0, 3600),
            ("min_render_interval", min_render_interval, 0, 60),
        ):
            if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError("Invalid resource limit: " + label)
        self.max_render_seconds = max_render_seconds
        self.cache_bytes, self.cache_ttl, self.cache_grace = int(cache_bytes), cache_ttl, cache_grace
        self.min_render_interval, self.last_render = min_render_interval, 0
        self.release = read_json(self.root / "release.json")
        if self.release.get("schemaVersion") != 1 or not isinstance(self.release.get("works"), dict):
            raise ValueError("站点清单格式无效")
        self.origins = set(origins)
        for origin in self.origins:
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
                raise ValueError("Origin 需为完整的 HTTP(S) 站点来源，不含路径")
        self.fluidsynth = self._binary(fluidsynth) if fluidsynth else None
        self.ffmpeg = self._binary(ffmpeg) if ffmpeg else None
        self.soundfont = Path(soundfont).resolve() if soundfont else None
        if self.soundfont and not self.soundfont.is_file():
            raise ValueError("音色库不存在")
        self.lock = threading.Lock()
        modules = self.root / "runtime_modules"
        self.playback = load_module(modules / "playback.py", "frozen_site_playback")
        self.validation = load_module(modules / "validation.py", "frozen_site_validation")
        self.movements = {}
        for wid, work in self.release["works"].items():
            if not IDENTIFIER.fullmatch(wid) or work.get("id") != wid:
                raise ValueError("曲目 ID 无效")
            for mid, movement in work.get("movements", {}).items():
                if not IDENTIFIER.fullmatch(mid) or movement.get("id") != mid:
                    raise ValueError("乐章 ID 无效")
                bundle = inside(self.root, movement["bundle"])
                model = read_json(bundle / "assets/interactive.json")
                if model.get("revision") != movement.get("revision"):
                    raise ValueError("曲谱版本不一致")
                if not isinstance(model.get("revision"), str) or not IDENTIFIER.fullmatch(model["revision"]):
                    raise ValueError("曲谱版本标识无效")
                projection = read_json(inside(self.root, movement["projection"]))
                if projection.get("revision") != model["revision"]:
                    raise ValueError("曲谱投影版本不一致")
                self.movements[(wid, mid)] = {
                    "work": work, "movement": movement, "bundle": bundle,
                    "model": model, "projection": projection,
                    "original": inside(self.root, movement["original"]),
                }
        self.prune_cache()

    def prune_cache(self, required=0):
        """Prune only owned UUID output directories, never active/recent audio.

        A failed reservation refuses a request. It does not remove a fresh clip
        another browser may still be playing. Cache capacity and render time
        limit disk use even when clients intentionally make repeated requests.
        """
        now, records, total = time.time(), [], 0
        for path in self.runtime.glob("*/*/*/*"):
            if not path.is_dir() or path.is_symlink() or not re.fullmatch(r"[a-f0-9]{32}", path.name):
                continue
            children = list(path.iterdir())
            if any(p.is_symlink() or not p.is_file() or p.name not in {"mix.mid", "mix.wav", "mix.mp3", ".complete"} for p in children):
                continue
            marker = path / ".complete"
            if not marker.is_file():
                # There is only one synthesis worker. This runs before it
                # allocates a target, so an old incomplete target is abandoned.
                age = now - path.stat().st_mtime
                if age >= self.cache_grace:
                    shutil.rmtree(path)
                else:
                    total += sum(p.stat().st_size for p in children)
                continue
            size = sum(p.stat().st_size for p in children)
            age = now - marker.stat().st_mtime
            if age >= self.cache_ttl and age >= self.cache_grace:
                shutil.rmtree(path)
                continue
            total += size
            records.append((marker.stat().st_mtime, path, size, age))
        for _, path, size, age in sorted(records):
            if total + required <= self.cache_bytes:
                break
            if age >= self.cache_grace:
                shutil.rmtree(path)
                total -= size
        if total + required > self.cache_bytes:
            raise ResourceBusy("音频缓存已达容量上限，请稍后重试")

    @staticmethod
    def _binary(value):
        path = Path(value)
        if not path.is_absolute() or not path.is_file() or not os.access(str(path), os.X_OK):
            raise ValueError("可执行程序必须为有效的绝对路径")
        return path.resolve()

    def select(self, wid, mid=None):
        work = self.release["works"].get(wid)
        if not work:
            raise ValueError("曲目不存在")
        if not mid:
            mid = next(iter(work["movements"]), None)
        selected = self.movements.get((wid, mid))
        if not selected:
            raise ValueError("曲目段落不存在")
        return selected, "/w/{}/{}/".format(wid, mid)

    def health(self):
        works = []
        for wid, work in self.release["works"].items():
            movements = [{"id": mid, "revision": item["model"]["revision"], "loaded": True}
                         for (work_id, mid), item in self.movements.items() if work_id == wid]
            works.append({"id": wid, "title": work["title"], "loaded": bool(movements), "movements": movements})
        return {"service": "choir-score-library-site", "readOnly": True, "works": works,
                "loadedWorks": sum(work["loaded"] for work in works),
                "synthesisAvailable": bool(self.fluidsynth and self.soundfont)}

    def symbols(self, query):
        self.select(query.get("work", ""), query.get("movement"))
        clauses, values = ["work_id=?"], [query["work"]]
        for column, key in (("movement_id", "movement"), ("part", "part"), ("measure_index", "measure")):
            if key in query:
                clauses.append(column + "=?")
                values.append(int(query[key]) if key == "measure" else query[key])
        limit, offset = max(1, min(int(query.get("limit", 100)), 500)), max(0, int(query.get("offset", 0)))
        db = (self.root / "library.sqlite3").as_uri() + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(db, uri=True)) as connection:
            where = " AND ".join(clauses)
            count = connection.execute("SELECT count(*) FROM symbols WHERE " + where, values).fetchone()[0]
            rows = connection.execute("SELECT payload_json FROM symbols WHERE " + where +
                                      " ORDER BY movement_id,measure_index,offset_ql,id LIMIT ? OFFSET ?",
                                      values + [limit, offset]).fetchall()
        return {"total": count, "offset": offset, "limit": limit, "symbols": [public_projection(json.loads(row[0])) for row in rows]}

    def review_path(self, selected):
        relative = selected["movement"].get("reviewReport") or selected["work"].get("reviewReport")
        if not relative:
            raise ValueError("此曲目尚无校对报告")
        return inside(self.root, relative)

    def render(self, selected, prefix, settings, selection):
        if not self.fluidsynth or not self.soundfont:
            raise RuntimeError("服务器尚未配置音频合成")
        model = selected["model"]
        note_ids = settings.get("noteIds") if selection else None
        if selection and note_ids is None:
            raise ValueError("请先选择音符")
        validation = self.validation.require_rhythm(model, settings.get("rhythmPolicy", "strict"), note_ids)
        self.playback.validate_settings(settings)
        notes = self.playback._selected_notes(model, note_ids)
        start = min(n["start"] for n in notes) if selection else 0
        end = max(n["end"] for n in notes) if selection else model["duration"]
        duration = (end - start) / settings.get("practiceRate", 1) + (0.35 if selection else 0)
        if not math.isfinite(duration) or not 0 < duration <= self.max_render_seconds:
            raise ValueError("所选音频超过站点时长限制；请缩小选区或提高练习速度")
        if time.monotonic() - self.last_render < self.min_render_interval:
            raise ResourceBusy("生成音频过于频繁，请稍后重试")
        # Stereo signed-16 PCM plus FluidSynth's release tail, a second
        # duration-fitting WAV and optional MP3. Conservative reservation is
        # deliberate: actual output is checked again before publishing it.
        reservation = math.ceil((duration + 10) * 44100 * 4 * 2 + duration * 32000 + 2 * 1024 ** 2)
        self.prune_cache(reservation)
        self.last_render = time.monotonic()
        wid, mid = selected["work"]["id"], selected["movement"]["id"]
        # Revision is a frozen identifier, not a trusted filesystem path.
        revision = model["revision"]
        if not IDENTIFIER.fullmatch(revision):
            raise ValueError("曲谱版本标识无效")
        ident = uuid.uuid4().hex
        target = inside(self.runtime, Path(wid) / mid / revision / ident)
        target.mkdir(parents=True)
        try:
            info = self.playback.make_midi(model, target / "mix.mid", settings, note_ids)
            subprocess.run([str(self.fluidsynth), "-F", str(target / "mix.wav"), "-T", "wav", "-O", "s16",
                            "-r", "44100", "-ni", "-g", "0.5", str(self.soundfont), str(target / "mix.mid")],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
            fit_wav_duration(target / "mix.wav", info["duration"] + (0.35 if selection else 0))
            audio_name = "mix.wav"
            if self.ffmpeg:
                subprocess.run([str(self.ffmpeg), "-nostdin", "-y", "-loglevel", "error", "-i", str(target / "mix.wav"),
                                "-b:a", "192k", str(target / "mix.mp3")], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
                audio_name = "mix.mp3"
            if sum(p.stat().st_size for p in target.iterdir()) > reservation:
                raise ResourceBusy("生成音频超过缓存预算")
            (target / ".complete").touch()
            url = prefix + "generated/" + ident + "/"
            return dict(info, audio=url + audio_name, wav=url + "mix.wav", midi=url + "mix.mid",
                        revision=revision, selection=selection, audioValidation=validation)
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise


READ_ONLY_CALIBRATION = """(() => {
  'use strict';
  window.initCalibrationUI = function () {
    const host = document.getElementById('calibration-host');
    if (!host || host.calibrationController) return host?.calibrationController;
    host.classList.add('panel', 'calibration-panel');
    const title = document.createElement('h2'); title.textContent = '曲谱校对记录';
    const text = document.createElement('p'); text.className = 'hint';
    text.textContent = '此排练站点提供已发布版本。曲谱修订由维护者在校对工作台完成后发布。';
    const report = document.createElement('a'); report.textContent = '下载原谱校对报告 ↗';
    report.href = 'review'; report.target = '_blank'; report.rel = 'noopener';
    host.replaceChildren(title, text, report);
    host.calibrationController = {refresh() {}, dispose() {}, state: {readOnly:true}};
    return host.calibrationController;
  };
  window.initCalibrationUI();
})();
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def site(self):
        return self.server.site

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Permissions-Policy", "camera=(), geolocation=(), microphone=(self)")
        for cookie in getattr(self, "response_cookies", []):
            self.send_header("Set-Cookie", cookie)
        super().end_headers()

    def cookies(self):
        result = SimpleCookie()
        try:
            result.load(self.headers.get("Cookie", ""))
        except Exception:
            return {}
        return {name: item.value for name, item in result.items()}

    def set_cookie(self, name, value, seconds):
        if not hasattr(self, "response_cookies"):
            self.response_cookies = []
        self.response_cookies.append("{}={}; Path=/; Max-Age={}; HttpOnly; Secure; SameSite=Strict".format(name, value, seconds))

    def client_identity(self):
        # Cloudflare headers are trusted only at the loopback tunnel origin.
        host = self.client_address[0]
        if ipaddress.ip_address(host).is_loopback and self.headers.get("X-Forwarded-Proto") == "https":
            candidate = self.headers.get("CF-Connecting-IP", "")
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                pass
        return host

    def authenticated(self, work):
        access = self.site.access
        return not access.enabled or access.valid_token(self.cookies().get(access.cookie_name(work), ""), "work", work)

    def require_work(self, work):
        if not self.authenticated(work):
            raise AuthenticationRequired("请先输入此曲目的访问密码")

    def local_maintenance(self):
        value = self.headers.get("Authorization", "")
        secret = self.site.access.internal_health_token
        return (ipaddress.ip_address(self.client_address[0]).is_loopback and bool(secret)
                and len(value) < 512 and hmac.compare_digest(value, "Bearer " + secret)
                and not any(self.headers.get(k) for k in ("CF-Connecting-IP", "CF-Ray", "Forwarded", "X-Forwarded-For", "X-Forwarded-Proto")))

    def csrf_valid(self, data):
        token = data.get("csrfToken", "")
        client = self.cookies().get("__Host-choir-client", "")
        return (isinstance(token, str) and len(token) < 1024 and len(client) < 1024
                and self.site.access.valid_token(client, "client", "site")
                and self.site.access.valid_token(token, "csrf", sha256(client.encode()).hexdigest()))

    def public_config(self):
        client = self.cookies().get("__Host-choir-client", "")
        if not self.site.access.valid_token(client, "client", "site"):
            client = self.site.access.token("client", "site", 3600)
            self.set_cookie("__Host-choir-client", client, 3600)
        token = self.site.access.token("csrf", sha256(client.encode()).hexdigest(), 3600)
        return {"siteTitle": self.site.access.title, "githubUrl": self.site.access.github,
                "authRequired": self.site.access.enabled, "feedbackEnabled": self.site.access.feedback_enabled,
                "csrfToken": token}

    def public_catalog(self):
        value = read_json(self.site.root / "catalog.json")
        # Only homepage metadata is public; score content and source/download
        # routes are always authorized independently at their final endpoint.
        allowed = {"id", "title", "parts", "status", "measures", "notes", "rests", "duration",
                   "sourceReviewed", "blockingMeasures"}
        works = []
        for work in value.get("works", []):
            item = {key: work[key] for key in allowed if key in work}
            item["movements"] = [{key: movement[key] for key in ("id", "title", "revision") if key in movement}
                                 for movement in work.get("movements", [])]
            works.append(item)
        return {"schemaVersion": value.get("schemaVersion", 1), "totals": value.get("totals", {}), "works": works}

    def bytes_response(self, data, mime="application/json; charset=utf-8", status=200, download=None):
        self.close_connection = True
        size = len(data)
        start, end, partial = self._range(size) if status == 200 else (0, size - 1, False)
        if start is None:
            return
        self.send_response(206 if partial else status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("Referrer-Policy", "same-origin")
        if status in (409, 429, 503):
            self.send_header("Retry-After", "5")
        if partial:
            self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
        if download:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(download))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data[start:end + 1])

    def json_response(self, data, status=200):
        self.bytes_response(json.dumps(data, ensure_ascii=False).encode("utf-8"), status=status)

    def _range(self, size):
        header = self.headers.get("Range") if self.command in ("GET", "HEAD") else None
        if not header:
            return 0, size - 1, False
        try:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
            if not match or not size:
                raise ValueError()
            first, last = match.groups()
            if not first:
                if not last or int(last) <= 0:
                    raise ValueError()
                start, end = max(0, size - int(last)), size - 1
            else:
                start, end = int(first), min(int(last), size - 1) if last else size - 1
            if start >= size or start > end:
                raise ValueError()
            return start, end, True
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */{}".format(size))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None, None, None

    def file_response(self, path, download=False):
        self.close_connection = True
        path = Path(path)
        if not path.is_file():
            return self.json_response({"error": "文件不存在"}, 404)
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            start, end, partial = self._range(size)
            if start is None:
                return
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(max(0, end - start + 1)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Cache-Control", "private, no-store")
            if partial:
                self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
            if download:
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(path.name))
            self.end_headers()
            if self.command == "HEAD":
                return
            stream.seek(start)
            remaining = max(0, end - start + 1)
            while remaining:
                data = stream.read(min(65536, remaining))
                if not data:
                    break
                self.wfile.write(data)
                remaining -= len(data)

    def route(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if "\0" in path or ".." in path.split("/") or "\\" in path:
            raise ValueError("文件路径无效")
        query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
        if path.startswith("/w/"):
            bits = path.split("/", 4)
            if len(bits) != 5:
                raise ValueError("工作台地址无效")
            selected, prefix = self.site.select(bits[2], bits[3])
            return "/" + bits[4], query, selected, prefix
        return path, query, None, None

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            path, query, selected, prefix = self.route()
            if selected is None:
                if path == "/api/site-config":
                    return self.json_response(self.public_config())
                if path == "/api/health/internal":
                    if not self.local_maintenance():
                        return self.json_response({"error": "不存在的页面"}, 404)
                    return self.json_response(self.site.health())
                if path == "/api/health":
                    if self.site.access.enabled:
                        return self.json_response({"service": "choir-score-library-site", "readOnly": True, "status": "ok"})
                    return self.json_response(self.site.health())
                if path in ("/api/catalog", "/catalog.json"):
                    return self.json_response(self.public_catalog())
                if path == "/api/symbols":
                    self.require_work(query.get("work", ""))
                    return self.json_response(self.site.symbols(query))
                if path == "/api/download":
                    item, _ = self.site.select(query.get("work", ""), query.get("movement"))
                    self.require_work(item["work"]["id"])
                    kind = query.get("type")
                    if kind == "pdf":
                        target = inside(self.site.root, item["work"]["sourcePdf"])
                    elif kind == "review":
                        target = self.site.review_path(item)
                    elif kind == "model":
                        return self.bytes_response(json.dumps(public_projection(item["model"]), ensure_ascii=False).encode("utf-8"),
                                                   download="interactive-public.json")
                    elif kind == "validation":
                        return self.json_response(public_projection(read_json(item["bundle"] / "assets/validation.json")))
                    else:
                        names = {"musicxml": "canonical.musicxml", "midi": "base.mid", "wav": "base.wav",
                                 }
                        if kind not in names:
                            raise ValueError("不支持的下载类型")
                        target = inside(item["bundle"] / "assets", names[kind])
                    return self.file_response(target, True)
                if path == "/library.sqlite3":
                    if self.site.access.enabled:
                        return self.json_response({"error": "整库下载未对外开放；请按曲目查询"}, 403)
                    return self.file_response(self.site.root / "library.sqlite3", True)
                root_files = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/app.css": "app.css"}
                if path in root_files:
                    return self.file_response(self.site.root / root_files[path])
            else:
                self.require_work(selected["work"]["id"])
                if path == "/api/health":
                    return self.json_response({"service": "sheet-to-audio-workbench", "revision": selected["model"]["revision"], "readOnly": True})
                if path == "/api/calibration":
                    return self.json_response({"status": "read-only", "revision": selected["model"]["revision"], "canApply": False, "reportUrl": prefix + "review", "candidates": []})
                if path == "/review":
                    return self.file_response(self.site.review_path(selected), True)
                if path in ("/assets/interactive.json", "/assets/score.json"):
                    value = selected["model"] if path.endswith("interactive.json") else selected["projection"]
                    return self.json_response(web_projection(public_projection(value), prefix))
                if path == "/assets/notation-overlay.json":
                    relative = selected["movement"].get("notationOverlay")
                    if not relative:
                        return self.json_response({"error": "此曲目暂无谱面附注"}, 404)
                    overlay = read_json(inside(self.site.root, relative))
                    return self.json_response(web_projection(public_projection(overlay), prefix))
                if path == "/assets/validation.json":
                    return self.json_response(public_projection(read_json(selected["bundle"] / "assets/validation.json")))
                filename = "index.html" if path == "/" else path[1:]
                if filename in UI_FILES:
                    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                    if filename == "calibration_ui.js":
                        return self.bytes_response(READ_ONLY_CALIBRATION.encode("utf-8"), mime)
                    source = (self.site.root / "ui/workbench" / filename).read_text(encoding="utf-8")
                    return self.bytes_response(derive_ui(source, prefix, filename), mime)
                for url, directory in (("/assets/", selected["bundle"] / "assets"), ("/original/", selected["original"])):
                    if path.startswith(url):
                        relative = path[len(url):]
                        # Browser-readable JSON is projected above; arbitrary
                        # bundle JSON/HTML/configs never become a download route.
                        if Path(relative).suffix.lower() not in {".svg", ".png", ".jpg", ".jpeg", ".webp", ".pdf", ".mp3", ".wav", ".mid", ".musicxml"}:
                            return self.json_response({"error": "不存在的资源"}, 404)
                        return self.file_response(inside(directory, relative))
                if path.startswith("/generated/"):
                    cache = self.site.runtime / selected["work"]["id"] / selected["movement"]["id"] / selected["model"]["revision"]
                    relative = path[len("/generated/"):]
                    if not GENERATED_PATH.fullmatch(relative):
                        return self.json_response({"error": "不存在的资源"}, 404)
                    return self.file_response(inside(cache, relative))
            return self.json_response({"error": "不存在的页面"}, 404)
        except AuthenticationRequired as error:
            return self.json_response({"error": str(error), "authenticationRequired": True}, 401)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return self.json_response({"error": "请求参数或路径无效"}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, sqlite3.Error):
            return self.json_response({"error": "站点资源暂不可用"}, 500)

    def allowed_origin(self):
        # This only prevents browser cross-origin writes. It is not identity or
        # access control: non-browser callers can omit/spoof Origin entirely.
        origin = self.headers.get("Origin")
        if self.site.access.enabled and not origin:
            return False
        if origin:
            if self.site.origins:
                return origin in self.site.origins
            return origin == "http://" + self.headers.get("Host", "")
        return self.headers.get("Sec-Fetch-Site") != "cross-site"

    def do_POST(self):
        if not self.allowed_origin():
            self.close_connection = True
            return self.json_response({"error": "请求来源不被允许"}, 403)
        try:
            path, _, selected, prefix = self.route()
            if path in ("/api/correction", "/api/calibration"):
                self.close_connection = True
                return self.json_response({"error": "排练网站为只读版本；请联系维护者发布曲谱修订"}, 403)
            if not ((selected is None and path in ("/api/open", "/api/login", "/api/feedback")) or (selected and path in ("/api/render", "/api/selection"))):
                self.close_connection = True
                return self.json_response({"error": "不存在的操作"}, 404)
            if self.headers.get_content_type() != "application/json":
                self.close_connection = True
                return self.json_response({"error": "需要 JSON 参数"}, 415)
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                self.close_connection = True
                raise ValueError("请求长度无效")
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 300000:
                self.close_connection = True
                raise ValueError("请求大小无效")
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("请求正文不完整")
            data = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("参数必须为有限数值")))
            if not isinstance(data, dict):
                raise ValueError("参数必须为对象")
            if selected is None and path in ("/api/login", "/api/feedback"):
                if not self.csrf_valid(data):
                    return self.json_response({"error": "页面验证已过期，请刷新后重试"}, 403)
                if path == "/api/feedback":
                    ident = self.site.access.feedback(data, self.client_identity(), self.site.release["works"])
                    return self.json_response({"receipt": ident, "message": "反馈已保存，感谢帮助改进曲库"}, 201)
                work = data.get("work", "")
                self.site.select(work)
                if self.site.access.enabled:
                    if not self.site.access.password_matches(data.get("password"), self.client_identity()):
                        return self.json_response({"error": "密码不正确"}, 401)
                    cookie = self.site.access.cookie_name(work)
                    self.set_cookie(cookie, self.site.access.token("work", work, self.site.access.session_seconds), self.site.access.session_seconds)
                return self.json_response({"authenticated": True, "work": work})
            if selected is None:
                item, url = self.site.select(data.get("work", ""), data.get("movement"))
                self.require_work(item["work"]["id"])
                return self.json_response({"url": url, "revision": item["model"]["revision"], "work": item["work"]["id"]})
            self.require_work(selected["work"]["id"])
            if not self.site.lock.acquire(blocking=False):
                return self.json_response({"error": "正在生成音频，请稍后再试"}, 409)
            try:
                result = self.site.render(selected, prefix, data, path == "/api/selection")
                return self.json_response(result)
            finally:
                self.site.lock.release()
        except AuthenticationRequired as error:
            return self.json_response({"error": str(error), "authenticationRequired": True}, 401)
        except ResourceBusy as error:
            return self.json_response({"error": str(error)}, 429)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            # Playback/validation errors contain musical facts, never host paths.
            return self.json_response({"error": str(error) if isinstance(error, ValueError) else "请求参数无效"}, 400)
        except (RuntimeError, OSError, sqlite3.Error, subprocess.SubprocessError):
            return self.json_response({"error": "音频生成失败，请稍后重试或联系维护者"}, 503)


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, handler, max_connections=32, read_timeout=15):
        if type(max_connections) is not int or not 1 <= max_connections <= 256:
            raise ValueError("max_connections must be between 1 and 256")
        if type(read_timeout) not in (int, float) or not math.isfinite(read_timeout) or not 1 <= read_timeout <= 60:
            raise ValueError("read_timeout must be between 1 and 60 seconds")
        self.slots = threading.BoundedSemaphore(max_connections)
        self.read_timeout = read_timeout
        super().__init__(address, handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.read_timeout)
        return connection, address

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\nRetry-After: 5\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


def make_server(site, bind="127.0.0.1", port=8780, max_connections=32, read_timeout=15):
    server = BoundedHTTPServer((bind, port), Handler, max_connections, read_timeout)
    server.site = site
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", "--release", dest="root", required=True, help="冻结网站导出目录")
    parser.add_argument("--runtime", required=True, help="站点目录之外的可写音频缓存")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--fluidsynth", help="FluidSynth 绝对路径；省略则仅提供已生成音频")
    parser.add_argument("--soundfont", help="SF2 音色库路径；与 FluidSynth 配合使用")
    parser.add_argument("--ffmpeg", help="可选 ffmpeg 绝对路径；未配置时使用 WAV")
    parser.add_argument("--site-config", help="发布目录之外、权限 0600/0640 的站点私有 JSON 配置")
    parser.add_argument("--origin", action="append", default=[], help="允许的外部 HTTPS 来源，可重复")
    parser.add_argument("--max-render-seconds", type=float, default=600)
    parser.add_argument("--cache-mib", type=int, default=512)
    parser.add_argument("--cache-ttl", type=float, default=3600)
    parser.add_argument("--cache-grace", type=float, default=120)
    parser.add_argument("--min-render-interval", type=float, default=2)
    parser.add_argument("--max-connections", type=int, default=32)
    parser.add_argument("--read-timeout", type=float, default=15)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 范围")
    try:
        site = ScoreSite(args.root, args.runtime, args.fluidsynth, args.soundfont, args.ffmpeg, args.origin,
                         args.max_render_seconds, args.cache_mib * 1024 ** 2, args.cache_ttl,
                         args.cache_grace, args.min_render_interval, args.site_config)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        parser.error(str(error))
    server = make_server(site, args.bind, args.port, args.max_connections, args.read_timeout)
    print("合唱曲库站点：http://{}:{}/（{} 首）".format(args.bind, args.port, len(site.release["works"])), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
