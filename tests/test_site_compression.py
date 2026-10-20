"""HTTP compression boundaries using synthetic, locally exported score fixtures."""
import gzip
import json
from http.client import HTTPConnection
import time
import unittest
from unittest import mock
from urllib.error import HTTPError

from choir_assistant.deployment import site_server
import test_site_auth_feedback as access_fixtures
import test_site_export as export_fixtures


class GzipNegotiationTests(unittest.TestCase):
    def test_accepted_codings_and_conservative_quality_values(self):
        cases = (
            ("", False), ("identity", False), ("br", False),
            ("gzip", True), ("GZip", True), ("br, gzip", True),
            (" gzip ; q=0.500 ", True), ("gzip;q=1.000", True),
            ("gzip;q=0.001", True), ("gzip;q=0", False),
            ("gzip;q=0.000", False), ("*;q=0.5", True),
            ("*;q=0", False), ("gzip;q=0, *;q=1", False),
            ("gzip;q=0.5, *;q=0", True),
            ("gzip;q=1,gzip;q=0", False),
            ("gzip;q=0,gzip;q=1", False),
            ("gzip;q=-1", False), ("gzip;q=1.001", False),
            ("gzip;q=0.0001", False), ("gzip;q=NaN", False),
            ("gzip;q=inf", False), ("gzip;q=oops", False),
            ("gzip;q=0.١", False),
            ("gzip;q=", False),
        )
        for header, expected in cases:
            with self.subTest(header=header):
                self.assertIs(site_server.accepts_gzip(header), expected)


class SiteCompressionHTTPTests(unittest.TestCase):
    # Reuse setup helpers without inheriting and rediscovering their test cases.
    refresh_manifest = export_fixtures.SiteExportTests.refresh_manifest
    register = export_fixtures.SiteExportTests.register
    exported = export_fixtures.SiteExportTests.exported
    serve = access_fixtures.SiteAccessHTTPTests.serve
    request = access_fixtures.SiteAccessHTTPTests.request
    config = access_fixtures.SiteAccessHTTPTests.config
    login = access_fixtures.SiteAccessHTTPTests.login

    def setUp(self):
        export_fixtures.SiteExportTests.setUp(self)
        assets = self.bundle / "assets"
        self.svg = ('<svg xmlns="http://www.w3.org/2000/svg"><desc>'
                    + "Synthetic notation fixture. " * 250 + "</desc></svg>").encode()
        (assets / "notation-page-001.svg").write_bytes(self.svg)
        (assets / "tiny.svg").write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        self.refresh_manifest()

    def authenticated_site(self, **options):
        self.serve(**options)
        with self.login():
            pass

    def response(self, path, *, encoding=None, method=None, headers=None, **options):
        request_headers = dict(headers or {})
        if encoding is not None:
            request_headers["Accept-Encoding"] = encoding
        with self.request(path, headers=request_headers, method=method, **options) as response:
            return response.status, response.headers, response.read()

    def assert_identity(self, headers, body=None):
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertIn("no-store", headers.get("Cache-Control", ""))
        if body is not None:
            self.assertEqual(int(headers["Content-Length"]), len(body))

    def assert_vary_encoding(self, headers):
        values = {value.strip().lower() for value in headers.get("Vary", "").split(",")}
        self.assertIn("accept-encoding", values)

    def test_immutable_models_and_workbench_assets_round_trip(self):
        self.authenticated_site()
        for relative in ("assets/interactive.json", "assets/score.json", "",
                         "app.js", "style.css", "assets/notation-page-001.svg"):
            path = "/w/study/main/" + relative
            with self.subTest(path=path):
                status, identity_headers, identity = self.response(path, encoding="identity")
                self.assertEqual(status, 200)
                self.assert_identity(identity_headers, identity)
                self.assert_vary_encoding(identity_headers)
                status, headers, compressed = self.response(path, encoding="gzip")
                self.assertEqual(status, 200)
                self.assertEqual(headers.get("Content-Encoding"), "gzip")
                self.assertEqual(gzip.decompress(compressed), identity)
                self.assertEqual(int(headers["Content-Length"]), len(compressed))
                self.assertLess(len(compressed), len(identity))
                self.assertEqual(headers["Content-Type"], identity_headers["Content-Type"])
                self.assertIn("no-store", headers["Cache-Control"])
                self.assert_vary_encoding(headers)
        _, _, compressed = self.response("/w/study/main/assets/interactive.json", encoding="gzip")
        model = json.loads(gzip.decompress(compressed))
        self.assertEqual(model["audio"], "/w/study/main/assets/base.wav")
        self.assertEqual(model["provenance"]["source"], "[authoring-path-redacted]")
        self.assertNotIn(str(self.root), gzip.decompress(compressed).decode())

    def test_identity_negotiation_and_small_files_remain_unencoded(self):
        self.authenticated_site()
        path = "/w/study/main/assets/interactive.json"
        _, _, expected = self.response(path, encoding="identity")
        for encoding in (None, "", "br", "gzip;q=0", "gzip;q=0,*;q=1", "gzip;q=NaN"):
            with self.subTest(encoding=encoding):
                status, headers, body = self.response(path, encoding=encoding)
                self.assertEqual(status, 200)
                self.assertEqual(body, expected)
                self.assert_identity(headers, body)
                self.assert_vary_encoding(headers)
        _, headers, body = self.response("/w/study/main/assets/tiny.svg", encoding="gzip")
        self.assertLess(len(body), 256)
        self.assert_identity(headers, body)
        self.assert_vary_encoding(headers)

    def test_head_matches_get_representation_headers_with_no_body(self):
        self.authenticated_site()
        for relative in ("assets/interactive.json", "app.js", "assets/notation-page-001.svg"):
            for encoding in ("gzip", "identity"):
                path = "/w/study/main/" + relative
                with self.subTest(path=path, encoding=encoding):
                    status, get_headers, body = self.response(path, encoding=encoding)
                    head_status, head_headers, empty = self.response(path, encoding=encoding, method="HEAD")
                    self.assertEqual(head_status, status)
                    self.assertEqual(empty, b"")
                    self.assertEqual(int(head_headers["Content-Length"]), len(body))
                    for name in ("Content-Type", "Content-Encoding", "Cache-Control", "Vary"):
                        self.assertEqual(head_headers.get(name), get_headers.get(name), name)

    def test_ranges_use_identity_offsets_even_when_gzip_is_accepted(self):
        self.authenticated_site()
        for relative in ("assets/interactive.json", "assets/notation-page-001.svg", "app.js"):
            path = "/w/study/main/" + relative
            _, _, identity = self.response(path, encoding="identity")
            for value, start, end in (("bytes=0-15", 0, 15), ("bytes=-16", len(identity) - 16, len(identity) - 1)):
                with self.subTest(path=path, range=value):
                    status, headers, body = self.response(path, encoding="gzip", headers={"Range": value})
                    self.assertEqual(status, 206)
                    self.assertEqual(body, identity[start:end + 1])
                    self.assertEqual(headers["Content-Range"], "bytes {}-{}/{}".format(start, end, len(identity)))
                    self.assert_identity(headers, body)
                    self.assert_vary_encoding(headers)
                    head_status, head_headers, empty = self.response(path, encoding="gzip", method="HEAD", headers={"Range": value})
                    self.assertEqual(head_status, 206)
                    self.assertEqual(empty, b"")
                    self.assertEqual(head_headers["Content-Range"], headers["Content-Range"])
                    self.assertEqual(head_headers["Content-Length"], headers["Content-Length"])
                    self.assertIsNone(head_headers.get("Content-Encoding"))

    def test_invalid_ranges_report_identity_length_without_compression(self):
        self.authenticated_site()
        path = "/w/study/main/assets/interactive.json"
        _, _, identity = self.response(path, encoding="identity")
        for range_value in ("bytes=999999-", "bytes=0-1,4-5", "nonsense"):
            with self.subTest(range=range_value):
                with self.assertRaises(HTTPError) as caught:
                    self.request(path, headers={"Accept-Encoding": "gzip", "Range": range_value})
                with caught.exception as response:
                    self.assertEqual(response.status, 416)
                    self.assertEqual(response.headers["Content-Range"], "bytes */{}".format(len(identity)))
                    self.assertEqual(response.headers["Content-Length"], "0")
                    self.assertIsNone(response.headers.get("Content-Encoding"))
                    self.assertEqual(response.read(), b"")

    def test_token_login_and_feedback_responses_never_compress(self):
        self.serve()
        _, headers, body = self.response("/api/site-config", encoding="gzip")
        self.assert_identity(headers, body)
        config = json.loads(body)
        self.assertIn("csrfToken", config)
        with self.login(headers={"Accept-Encoding": "gzip"}) as response:
            body = response.read()
            self.assert_identity(response.headers, body)
            self.assertTrue(json.loads(body)["authenticated"])
        data = {"csrfToken": self.config()["csrfToken"], "text": "Synthetic feedback " * 40, "work": "study"}
        with self.request("/api/feedback", data, headers={"Accept-Encoding": "gzip"}) as response:
            body = response.read()
            self.assertEqual(response.status, 201)
            self.assert_identity(response.headers, body)
            self.assertIn("receipt", json.loads(body))

    def test_other_dynamic_json_routes_do_not_enter_static_compression(self):
        self.authenticated_site(overlay=True)
        for path in ("/api/health", "/api/catalog", "/api/symbols?work=study",
                     "/w/study/main/api/health", "/w/study/main/api/calibration",
                     "/w/study/main/assets/validation.json", "/w/study/main/assets/notation-overlay.json"):
            with self.subTest(path=path):
                _, headers, body = self.response(path, encoding="gzip")
                self.assert_identity(headers, body)
                json.loads(body)

    def test_binary_download_and_generated_routes_remain_identity(self):
        self.authenticated_site()
        generated = self.site.runtime / "study/main/fixture-v1" / ("a" * 32) / "mix.wav"
        generated.parent.mkdir(parents=True)
        generated.write_bytes(b"synthetic generated audio " * 1000)
        for path in ("/w/study/main/assets/base.wav", "/w/study/main/assets/base.mid",
                     "/w/study/main/original/page-001.png",
                     "/api/download?work=study&type=pdf", "/api/download?work=study&type=model",
                     "/api/download?work=study&type=musicxml", "/w/study/main/generated/" + "a" * 32 + "/mix.wav"):
            with self.subTest(path=path):
                _, headers, body = self.response(path, encoding="gzip")
                self.assert_identity(headers, body)

    def test_compressed_cache_never_bypasses_work_or_session_authorization(self):
        self.authenticated_site()
        path = "/w/study/main/assets/interactive.json"
        self.assertEqual(self.response(path, encoding="gzip")[1].get("Content-Encoding"), "gzip")
        for denied_path, cookies in ((path, {}), ("/w/second-study/main/assets/interactive.json", self.cookies)):
            with self.subTest(path=denied_path):
                with self.assertRaises(HTTPError) as caught:
                    self.request(denied_path, headers={"Accept-Encoding": "gzip"}, cookies=cookies)
                with caught.exception as response:
                    body = response.read()
                    self.assertEqual(response.status, 401)
                    self.assert_identity(response.headers, body)
                    self.assertNotIn(b"fixture-v1", body)
        with mock.patch.object(site_server.time, "time", return_value=time.time() + 301):
            with self.assertRaises(HTTPError) as caught:
                self.request(path, headers={"Accept-Encoding": "gzip"}, method="HEAD")
            with caught.exception as response:
                self.assertEqual(response.status, 401)
                self.assertIsNone(response.headers.get("Content-Encoding"))
                self.assertIn("no-store", response.headers.get("Cache-Control", ""))
                self.assertEqual(response.read(), b"")

    def test_immutable_ui_builder_runs_once_per_work_for_both_encodings(self):
        self.authenticated_site()
        with self.login("second-study"):
            pass
        original = site_server.derive_ui
        with mock.patch.object(site_server, "derive_ui", wraps=original) as derive:
            bodies = {}
            for work in ("study", "second-study"):
                path = "/w/" + work + "/main/app.js"
                _, _, identity = self.response(path, encoding="identity")
                _, _, compressed = self.response(path, encoding="gzip")
                self.assertEqual(gzip.decompress(compressed), identity)
                self.response(path, encoding="gzip", method="HEAD")
                self.response(path, encoding="identity")
                bodies[work] = identity
            self.assertEqual(derive.call_count, 2)
        self.assertIn(b"/w/study/main/", bodies["study"])
        self.assertIn(b"/w/second-study/main/", bodies["second-study"])
        self.assertNotEqual(bodies["study"], bodies["second-study"])

    def test_split_accept_encoding_headers_use_conservative_explicit_quality(self):
        self.authenticated_site()
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        self.addCleanup(connection.close)
        connection.putrequest("GET", "/w/study/main/assets/interactive.json", skip_accept_encoding=True)
        connection.putheader("Cookie", "; ".join(name + "=" + value for name, value in self.cookies.items()))
        connection.putheader("Accept-Encoding", "gzip;q=0")
        connection.putheader("Accept-Encoding", "gzip;q=1,*;q=1")
        connection.endheaders()
        with connection.getresponse() as response:
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assert_identity(response.headers, body)
            self.assert_vary_encoding(response.headers)

    def test_representation_cache_lru_and_oversized_entries_respect_budget(self):
        release, _ = self.exported()
        self.site = site_server.ScoreSite(release, self.root / "runtime")
        builders = {name: mock.Mock(return_value=name.encode() * 4) for name in ("a", "b", "c")}
        with mock.patch.object(site_server, "RESPONSE_CACHE_BYTES", 8):
            for name in ("a", "b", "a", "c", "a", "b"):
                self.assertEqual(self.site.cached_bytes(name, builders[name]), name.encode() * 4)
                self.assertLessEqual(sum(len(value) for value in self.site._response_cache.values()), 8)
            self.assertEqual(builders["a"].call_count, 1, "Recent entry should survive LRU eviction")
            self.assertEqual(builders["b"].call_count, 2, "Least recent entry must be rebuilt after eviction")
            oversized = mock.Mock(return_value=b"oversized")
            self.assertEqual(self.site.cached_bytes("large", oversized), b"oversized")
            self.assertEqual(self.site.cached_bytes("large", oversized), b"oversized")
            self.assertEqual(oversized.call_count, 2, "Oversized entries must not be retained")
            self.assertLessEqual(sum(len(value) for value in self.site._response_cache.values()), 8)

    def test_representation_cache_does_not_store_failures_or_cross_site_instances(self):
        release, _ = self.exported()
        self.site = site_server.ScoreSite(release, self.root / "runtime")
        builder = mock.Mock(side_effect=[ValueError("synthetic builder failure"), b"first site"])
        with self.assertRaisesRegex(ValueError, "synthetic builder failure"):
            self.site.cached_bytes("same-key", builder)
        self.assertEqual(self.site.cached_bytes("same-key", builder), b"first site")
        self.assertEqual(self.site.cached_bytes("same-key", builder), b"first site")
        self.assertEqual(builder.call_count, 2)
        separate = site_server.ScoreSite(self.site.root, self.root / "separate-runtime")
        separate_builder = mock.Mock(return_value=b"second site")
        self.assertEqual(separate.cached_bytes("same-key", separate_builder), b"second site")
        self.assertEqual(separate_builder.call_count, 1)
        self.assertEqual(self.site.cached_bytes("same-key", builder), b"first site")


if __name__ == "__main__":
    unittest.main()
