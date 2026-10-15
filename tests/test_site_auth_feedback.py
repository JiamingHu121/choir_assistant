"""Real HTTP authorization boundaries, independent of any private deployment."""
from contextlib import closing
from hashlib import pbkdf2_hmac
from http.cookies import SimpleCookie
import json
import os
import sqlite3
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest import mock

from choir_assistant.deployment import catalog, site_server
import test_site_export as fixtures


class SiteAccessHTTPTests(unittest.TestCase):
    setUp = fixtures.SiteExportTests.setUp
    refresh_manifest = fixtures.SiteExportTests.refresh_manifest
    register = fixtures.SiteExportTests.register
    exported = fixtures.SiteExportTests.exported

    def serve(self, feedback_entries=20, overlay=False):
        catalog.register_project(self.library, self.project, "second-study")
        release, _ = self.exported()
        if overlay:
            relative = "data/study/main/notation-overlay.json"
            catalog.write_json(release / relative, {"schemaVersion": 1, "measures": [{"index": 1, "number": "1"}], "lyrics": [{"noteId": "n1", "text": "la"}]})
            descriptor = catalog.read_json(release / "release.json")
            descriptor["works"]["study"]["movements"]["main"]["notationOverlay"] = relative
            catalog.write_json(release / "release.json", descriptor)
            manifest = catalog.read_json(release / "file-manifest.json")
            manifest["files"] = [{"path": path.relative_to(release).as_posix(), "bytes": path.stat().st_size, "sha256": catalog.digest(path)}
                                 for path in sorted(release.rglob("*")) if path.is_file() and path.name != "file-manifest.json"]
            catalog.write_json(release / "file-manifest.json", manifest)
        salt = bytes.fromhex("ab" * 24)
        self.secret = "synthetic-fixture-password"
        config = {"schemaVersion": 1, "siteTitle": "Fixture Choir Library", "githubUrl": "https://github.com/example/project",
                  "internalHealthToken": "internal-fixture-health-secret-" + "c" * 32,
                  "auth": {"salt": salt.hex(), "passwordHash": pbkdf2_hmac("sha256", self.secret.encode(), salt, 100000).hex(),
                           "iterations": 100000, "cookieSigningKey": "ce" * 32, "sessionSeconds": 300},
                  "feedback": {"maxBytes": 65536, "maxEntries": feedback_entries}}
        self.config_path = self.root / "private-config.json"
        self.config_path.write_text(json.dumps(config))
        os.chmod(self.config_path, 0o600)
        self.site = site_server.ScoreSite(release, self.root / "runtime", site_config=self.config_path)
        self.server = site_server.make_server(self.site, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.cookies = {}
        return release

    def request(self, path, data=None, headers=None, cookies=None, method=None):
        current = self.cookies if cookies is None else cookies
        request = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                          headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:" + str(self.server.server_address[1]),
                                   "Cookie": "; ".join(k + "=" + v for k, v in current.items()), **(headers or {})}, method=method)
        response = urlopen(request, timeout=5)
        for value in response.headers.get_all("Set-Cookie", []):
            cookie = SimpleCookie(value)
            for key, item in cookie.items():
                current[key] = item.value
        return response

    def failure(self, code, path, data=None, **kwargs):
        with self.assertRaises(HTTPError) as context:
            self.request(path, data, **kwargs)
        self.assertEqual(context.exception.code, code, path)
        with context.exception as response:
            return json.load(response) if kwargs.get("method") != "HEAD" else {}

    def config(self):
        with self.request("/api/site-config") as response:
            return json.load(response)

    def login(self, work="study", password=None, **kwargs):
        config = self.config()
        return self.request("/api/login", {"work": work, "password": password or self.secret, "csrfToken": config["csrfToken"]}, **kwargs)

    def test_public_home_and_closed_direct_routes(self):
        self.serve()
        with self.request("/") as response:
            self.assertIn(b"password-dialog", response.read())
        config = self.config()
        self.assertEqual(config["siteTitle"], "Fixture Choir Library")
        self.assertTrue(config["authRequired"])
        self.assertNotIn(self.secret, json.dumps(config))
        with self.request("/api/catalog") as response:
            value = json.load(response)
            self.assertEqual(len(value["works"]), 2)
            self.assertNotIn("sourcePdf", json.dumps(value))
            self.assertNotIn("download", json.dumps(value))
        with self.request("/api/health") as response:
            self.assertEqual(json.load(response), {"service": "choir-score-library-site", "readOnly": True, "status": "ok"})
        for path in ("/w/study/main/", "/w/study/main/app.js", "/w/study/main/assets/interactive.json",
                     "/w/study/main/assets/score.json", "/w/study/main/assets/notation-overlay.json", "/w/study/main/assets/base.wav", "/w/study/main/assets/notation-page-001.svg",
                     "/w/study/main/original/page-001.png", "/w/study/main/generated/" + "a" * 32 + "/mix.wav",
                     "/api/download?work=study&type=pdf", "/api/download?work=study&type=model", "/api/symbols?work=study"):
            self.failure(401, path)
        self.failure(401, "/w/study/main/assets/base.wav", headers={"Range": "bytes=0-15"})
        self.failure(401, "/w/study/main/assets/base.wav", method="HEAD")
        self.failure(401, "/api/download?work=study&type=pdf", method="HEAD")
        self.failure(403, "/library.sqlite3")
        self.failure(401, "/api/open", {"work": "study"})
        self.failure(401, "/w/study/main/api/selection", {"noteIds": ["n1"]})

    def test_password_cookie_is_work_scoped_secure_and_tamper_proof(self):
        self.serve()
        self.failure(401, "/api/login", {"work": "study", "password": "wrong", "csrfToken": self.config()["csrfToken"]})
        with self.login() as response:
            self.assertTrue(json.load(response)["authenticated"])
            cookie = response.headers.get("Set-Cookie")
            for attribute in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/", "Max-Age=300"):
                self.assertIn(attribute, cookie)
        with self.request("/w/study/main/assets/interactive.json") as response:
            self.assertEqual(json.load(response)["revision"], "fixture-v1")
        with self.request("/api/download?work=study&type=wav", headers={"Range": "bytes=0-15"}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(len(response.read()), 16)
            self.assertIn("no-store", response.headers["Cache-Control"])
        self.failure(401, "/w/second-study/main/assets/interactive.json")
        self.failure(401, "/api/symbols?work=second-study")
        self.failure(403, "/library.sqlite3")
        name = self.site.access.cookie_name("study")
        token = self.cookies[name]
        self.cookies[name] = token[:-1] + ("1" if token[-1] != "1" else "2")
        self.failure(401, "/w/study/main/")
        self.cookies[name] = token
        with mock.patch.object(site_server.time, "time", return_value=time.time() + 301):
            self.failure(401, "/w/study/main/")

    def test_csrf_is_browser_bound_and_origin_is_required(self):
        self.serve()
        config = self.config()
        body = {"work": "study", "password": self.secret, "csrfToken": config["csrfToken"]}
        self.failure(403, "/api/login", body, cookies={})
        other = {}
        with self.request("/api/site-config", cookies=other):
            pass
        self.failure(403, "/api/login", body, cookies=other)
        self.failure(403, "/api/login", body, headers={"Origin": "https://attacker.invalid"})
        self.failure(403, "/api/login", body, headers={"Origin": ""})
        with self.login():
            pass
        self.failure(403, "/w/study/main/api/selection", {"noteIds": ["n1"]}, headers={"Origin": "https://attacker.invalid"})

    def test_login_rate_limit_and_internal_health_bearer(self):
        self.serve()
        config = self.config()
        body = {"work": "study", "password": "wrong", "csrfToken": config["csrfToken"]}
        for _ in range(8):
            self.failure(401, "/api/login", body)
        self.failure(429, "/api/login", body)
        self.failure(404, "/api/health/internal")
        self.failure(404, "/api/health/internal", headers={"Authorization": "Bearer wrong"})
        headers = {"Authorization": "Bearer " + self.site.access.internal_health_token}
        with self.request("/api/health/internal", headers=headers) as response:
            self.assertEqual(json.load(response)["loadedWorks"], 2)
        self.failure(404, "/api/health/internal", headers={**headers, "CF-Connecting-IP": "192.0.2.1"})

    def test_feedback_is_plain_private_bounded_data_with_receipt(self):
        self.serve()
        text = '<img src=x onerror="alert(1)"><script>bad()</script> spelling issue'
        data = {"csrfToken": self.config()["csrfToken"], "text": text, "work": "study", "movement": "main", "measure": "48", "position": "2:36"}
        with self.request("/api/feedback", data) as response:
            self.assertEqual(response.status, 201)
            receipt = json.load(response)["receipt"]
            self.assertEqual(len(receipt), 32)
        with closing(sqlite3.connect(self.site.access.feedback_path)) as db:
            row = db.execute("SELECT payload FROM feedback WHERE id=?", (receipt,)).fetchone()
            self.assertEqual(json.loads(row[0])["text"], text)
            self.assertNotIn("csrf", row[0])
        self.assertEqual(self.site.access.feedback_path.stat().st_mode & 0o777, 0o600)
        for path in ("/feedback.sqlite3", "/runtime/feedback.sqlite3", "/api/feedback", "/site-config.json"):
            self.failure(404, path)
        self.failure(403, "/api/feedback", {**data, "csrfToken": "invalid"})
        for _ in range(4):
            with self.request("/api/feedback", data):
                pass
        self.failure(429, "/api/feedback", data)

    def test_feedback_quota_and_length_limits(self):
        self.serve(feedback_entries=1)
        data = {"csrfToken": self.config()["csrfToken"], "text": "first", "work": "study"}
        self.failure(400, "/api/feedback", {**data, "text": "x" * 4001})
        self.failure(400, "/api/feedback", {**data, "measure": "x" * 81})
        with self.request("/api/feedback", data):
            pass
        self.failure(429, "/api/feedback", data)
        with closing(sqlite3.connect(self.site.access.feedback_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM feedback").fetchone()[0], 1)

    def test_sidecar_overlay_is_authorized_and_missing_overlay_is_404(self):
        self.serve(overlay=True)
        self.failure(401, "/w/study/main/assets/notation-overlay.json")
        with self.login():
            pass
        with self.request("/w/study/main/assets/notation-overlay.json") as response:
            value = json.load(response)
            self.assertEqual(value["lyrics"][0]["text"], "la")
        self.failure(401, "/w/second-study/main/assets/notation-overlay.json")
        with self.login("second-study"):
            pass
        self.failure(404, "/w/second-study/main/assets/notation-overlay.json")

    def test_config_must_stay_private_and_outside_release(self):
        release = self.serve()
        os.chmod(self.config_path, 0o644)
        with self.assertRaisesRegex(ValueError, "权限"):
            site_server.SiteAccess(self.config_path, self.site.runtime, release)
        os.chmod(self.config_path, 0o640)
        site_server.SiteAccess(self.config_path, self.site.runtime, release)
        with self.assertRaisesRegex(ValueError, "之外"):
            site_server.SiteAccess(release / "release.json", self.site.runtime, release)
