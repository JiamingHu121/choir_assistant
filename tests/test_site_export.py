"""Portable library/export and standalone HTTP boundaries, without private scores."""
from hashlib import sha256
from contextlib import closing
from http.client import HTTPConnection
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import wave

from choir_assistant.deployment import catalog
from choir_assistant.deployment import export
from choir_assistant.deployment import site_server


PLUGIN = Path(__file__).resolve().parents[1]


class SiteExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.library, self.project = self.root / "library", self.root / "project/project.json"
        self.project.parent.mkdir()
        self.bundle = self.project.parent / "bundles/accepted"
        assets = self.bundle / "assets"
        assets.mkdir(parents=True)
        self.pdf = self.bundle / "inputs/source.pdf"
        self.pdf.parent.mkdir()
        shutil.copyfile(PLUGIN / "examples/practice-study.pdf", self.pdf)
        catalog.write_json(self.project, {"sourcePdf": str(self.root / "old-original.pdf")})
        catalog.write_json(self.project.parent / "run-state.json", {"latestBundle": str(self.bundle)})
        note = {"id": "n1", "kind": "note", "part": "S", "sourcePart": "P1", "staff": 1,
                "voice": "1", "pitch": 60, "pitchName": "C4", "start": 0, "end": 1,
                "duration": 2, "offset": 0, "measureIndex": 1, "measure": "1",
                "performanceId": "p1", "notation": {"type": "half"},
                "source": {"calibration": {"evidence": {"path": str(self.root / "private-evidence/page.png")}}}}
        self.model = {"schemaVersion": 2, "revision": "fixture-v1", "title": "Original public study",
                      "notes": [note], "rests": [], "parts": [{"sourcePart": "P1", "part": "S", "name": "Soprano"}],
                      "pages": [{"width": 1000, "height": 1400}], "measures": [{"index": 1, "number": "1", "start": 0, "end": 1, "duration": 2}],
                      "duration": 1, "audio": "/assets/base.wav", "playbackSettings": {"program": 52},
                      "rhythmValidation": {"status": "validated", "blockingMeasureIndices": []},
                      "provenance": {"source": str(self.root / "authoring/source.musicxml"), "sourceSha256": "a" * 64},
                      "performanceEvents": [{"id": "p1", "noteIds": ["n1"], "part": "S", "pitch": 60, "start": 0, "end": 1}]}
        catalog.write_json(assets / "interactive.json", self.model)
        catalog.write_json(assets / "validation.json", {"source": str(self.root / "authoring/config"), "scoreStatus": "draft"})
        (assets / "notation-page-001.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        (assets / "canonical.musicxml").write_text("<score-partwise/>")
        with wave.open(str(assets / "base.wav"), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\0\0" * 8000)
        (assets / "base.mid").write_bytes(b"MIDI-fixture")
        self.refresh_manifest()
        self.cache = self.project.parent / "preview/original" / catalog.digest(self.pdf)
        self.cache.mkdir(parents=True)
        png = self.cache / "page-001.png"
        png.write_bytes(b"source-page-fixture")
        catalog.write_json(self.cache / "render-manifest.json", {"sourceSha256": catalog.digest(self.pdf), "dpi": 300,
                "renderer": "pdftoppm", "pages": [{"page": 1, "path": png.name, "width": 1000, "height": 1400, "sha256": catalog.digest(png)}]})
        self.register()
        entry_path = self.library / "works/study/entry.json"
        entry = catalog.read_json(entry_path)
        report = self.project.parent / "review.md"
        report.write_text("Reviewed from `" + str(self.project) + "` and " + str(self.root / "source.pdf") + "\nhttps://example.org/evidence\n")
        entry["reviewReport"] = str(report)
        catalog.write_json(entry_path, entry)

    def refresh_manifest(self):
        artifacts = [{"path": p.relative_to(self.bundle).as_posix(), "sha256": catalog.digest(p)}
                     for p in sorted(self.bundle.rglob("*")) if p.is_file() and p.name != "manifest.json"]
        catalog.write_json(self.bundle / "manifest.json", {"status": "complete", "revision": "fixture-v1",
                "sourcePdf": {"path": "inputs/source.pdf", "sha256": catalog.digest(self.pdf)},
                "config": {"playback": {"program": 52}}, "validation": {"scoreStatus": "draft"}, "artifacts": artifacts})

    def register(self, **kwargs):
        return catalog.register_project(self.library, self.project, "study", **kwargs)

    def exported(self, name="site", **kwargs):
        output = self.root / name
        result = export.export_site(output, self.library, **kwargs)
        return output, result

    def test_register_and_export_are_independent_and_repeatable(self):
        first, result = self.exported()
        second, repeated = self.exported("second")
        self.assertEqual(result["fileManifestSha256"], repeated["fileManifestSha256"])
        self.assertEqual((first / "file-manifest.json").read_bytes(), (second / "file-manifest.json").read_bytes())
        manifest = catalog.read_json(first / "file-manifest.json")
        for item in manifest["files"]:
            self.assertEqual(catalog.digest(first / item["path"]), item["sha256"])
        self.assertTrue((first / "site_server.py").is_file())
        self.assertTrue((first / "runtime_modules/playback.py").is_file())
        self.assertNotIn(str(self.root), (first / "catalog.json").read_text())
        with closing(sqlite3.connect(first / "library.sqlite3")) as db:
            self.assertIsNone(db.execute("SELECT project FROM movements").fetchone()[0])
            self.assertEqual(db.execute("SELECT count(*) FROM symbols").fetchone()[0], 1)
            self.assertNotIn(str(self.root), "".join(str(row) for row in db.iterdump()))
        self.assertEqual((first / "data/study/main/bundle/assets/interactive.json").read_bytes(),
                         (self.bundle / "assets/interactive.json").read_bytes())
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.register()
        self.assertEqual(self.register(overwrite=True)["revision"], "fixture-v1")

    def test_bundle_mutation_fails_atomically(self):
        (self.bundle / "assets/base.mid").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.exported()
        self.assertFalse((self.root / "site").exists())

    def test_legacy_hash_bound_entry_and_new_project_revision(self):
        # Old catalogs need no migration/rewrite. Their entry fixes a bundle,
        # original-PDF identity and optional preview pointer explicitly.
        standard = self.library / "works/study/entry.json"
        entry = catalog.read_json(standard)
        entry.update(sourcePdf=str(self.pdf), sourceSha256=catalog.digest(self.pdf))
        entry["movements"][0]["bundle"] = str(self.bundle)
        legacy = self.library / "作品/study/entry.json"
        catalog.write_json(legacy, entry)
        standard.unlink()
        manifest = catalog.read_json(self.bundle / "manifest.json")
        manifest.pop("sourcePdf")
        catalog.write_json(self.bundle / "manifest.json", manifest)
        site, _ = self.exported()
        self.assertEqual(catalog.read_json(site / "release.json")["works"]["study"]["sourceSha256"], catalog.digest(self.pdf))
        # A later source-reviewed preview has a new immutable bundle. Export
        # follows only a correctly bound pointer, never rebuilds either bundle.
        revised = self.project.parent / "preview/reviewed"
        shutil.copytree(self.bundle, revised)
        model = catalog.read_json(revised / "assets/interactive.json")
        model["revision"] = "fixture-v2"
        catalog.write_json(revised / "assets/interactive.json", model)
        manifest["revision"] = "fixture-v2"
        for item in manifest["artifacts"]:
            item["sha256"] = catalog.digest(revised / item["path"])
        catalog.write_json(revised / "manifest.json", manifest)
        catalog.write_json(self.project.parent / "preview/current.json", {
            "baseBundle": str(self.bundle), "configPath": str(self.project), "bundle": str(revised)})
        _, exported = self.exported("updated")
        self.assertEqual(exported["revisions"]["study/main"], "fixture-v2")

    def test_unindexed_bundle_asset_is_rejected(self):
        (self.bundle / "assets/secret.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "Unindexed bundle asset"):
            self.exported()

    def test_source_cache_requires_explicit_offline_preparation(self):
        shutil.rmtree(self.cache)
        with self.assertRaisesRegex(ValueError, "prepare-original"):
            self.exported()
        if not shutil.which("pdftoppm"):
            self.skipTest("pdftoppm is an optional offline preparation dependency")
        site, _ = self.exported(prepare_original=True)
        pages = catalog.read_json(site / "data/study/main/original/render-manifest.json")
        self.assertEqual(pages["sourceSha256"], catalog.digest(self.pdf))
        self.assertGreater(len(pages["pages"]), 0)
        self.assertFalse(self.cache.exists(), "offline export must not modify authoring page caches")

    def test_invalid_original_cache_never_reuses_changed_pages(self):
        (self.cache / "page-001.png").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "No intact original-PDF cache"):
            self.exported()

    def test_release_startup_checks_exact_file_manifest(self):
        site, _ = self.exported()
        (site / "app.js").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "hash/size mismatch"):
            site_server.ScoreSite(site, self.root / "runtime")

    def serve(self, **limits):
        release, _ = self.exported()
        self.site = site_server.ScoreSite(release, self.root / "runtime", **limits)
        self.server = site_server.make_server(self.site, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])
        return release

    def request(self, path, data=None, headers=None):
        request = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                          headers={"Content-Type": "application/json", **(headers or {})})
        return urlopen(request, timeout=5)

    def test_http_site_projection_selection_origin_range_and_sql(self):
        self.serve()
        with self.request("/api/health") as response:
            self.assertEqual(json.load(response)["loadedWorks"], 1)
        with self.request("/api/open", {"work": "study"}) as response:
            self.assertEqual(json.load(response)["url"], "/w/study/main/")
        with self.request("/w/study/main/") as response:
            self.assertIn(b"/w/study/main/assets/base.wav", response.read())
        with self.request("/w/study/main/assets/interactive.json") as response:
            value = json.load(response)
            self.assertEqual(value["provenance"]["source"], "[authoring-path-redacted]")
            self.assertEqual(value["audio"], "/w/study/main/assets/base.wav")
        with self.request("/api/download?work=study&type=model") as response:
            self.assertNotIn(str(self.root), response.read().decode())
        with self.request("/api/download?work=study&type=review") as response:
            report = response.read().decode()
            self.assertNotIn(str(self.root), report)
            self.assertIn("https://example.org/evidence", report)
        with self.request("/api/symbols?work=study&part=S") as response:
            value = json.load(response)
            self.assertEqual(value["total"], 1)
            self.assertEqual(value["symbols"][0]["source"]["calibration"]["evidence"]["path"], "[authoring-path-redacted]")
        with self.request("/w/study/main/assets/base.wav", headers={"Range": "bytes=0-15"}) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(len(response.read()), 16)
        for path, data, headers, expected in (
            ("/w/study/main/assets/../../manifest.json", None, {}, 400),
            ("/w/study/main/assets/notation-normalization.json", None, {}, 404),
            ("/api/open", {"work": "study"}, {"Origin": "https://other.example"}, 403),
            ("/w/study/main/api/correction", {}, {}, 403),
        ):
            with self.assertRaises(HTTPError) as context:
                self.request(path, data, headers)
            self.assertEqual(context.exception.code, expected)
            context.exception.close()

    def fake_synth(self):
        executable = self.root / "fake-fluidsynth"
        executable.write_text("#!" + sys.executable + "\nimport sys,wave\np=sys.argv[sys.argv.index('-F')+1]\nwith wave.open(p,'wb') as w:\n w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(b'\\0\\0'*16000)\n")
        executable.chmod(0o700)
        font = self.root / "fixture.sf2"
        font.write_bytes(b"fixture")
        return {"fluidsynth": executable, "soundfont": font}

    def test_real_midi_selection_wav_delivery_and_render_budget(self):
        self.serve(**self.fake_synth(), min_render_interval=0)
        with self.request("/w/study/main/api/selection", {"noteIds": ["n1"], "rhythmPolicy": "strict"}) as response:
            result = json.load(response)
        self.assertEqual(result["duration"], 1)
        with self.request(result["wav"]) as response:
            self.assertEqual(response.read(4), b"RIFF")
        output = list((self.root / "runtime").rglob("mix.wav"))[0]
        with wave.open(str(output)) as audio:
            self.assertAlmostEqual(audio.getnframes() / audio.getframerate(), 1.35, places=3)
        self.site.max_render_seconds = 1
        with self.assertRaises(HTTPError) as context:
            self.request("/w/study/main/api/selection", {"noteIds": ["n1"], "practiceRate": .25})
        self.assertEqual(context.exception.code, 400)
        context.exception.close()
        self.site.max_render_seconds = 600
        self.site.cache_bytes = 1024 ** 2
        with self.assertRaises(HTTPError) as context:
            self.request("/w/study/main/api/selection", {"noteIds": ["n1"]})
        self.assertEqual(context.exception.code, 429)
        context.exception.close()
        self.assertTrue(output.is_file(), "budget refusal must preserve a fresh playing clip")

    def test_owned_expired_cache_prunes_but_unrelated_files_remain(self):
        self.serve()
        owned = self.root / "runtime/study/main/fixture-v1" / ("a" * 32)
        owned.mkdir(parents=True)
        (owned / "mix.wav").write_bytes(b"audio")
        marker = owned / ".complete"
        marker.touch()
        import os
        os.utime(marker, (time.time() - 4000,) * 2)
        other = owned.with_name("unrelated")
        other.mkdir()
        (other / "keep").write_text("keep")
        self.site.prune_cache()
        self.assertFalse(owned.exists())
        self.assertTrue((other / "keep").is_file())

    def test_standalone_server_help_without_importing_installed_plugin(self):
        release, _ = self.exported()
        run = subprocess.run([sys.executable, "-I", str(release / "site_server.py"), "--help"],
                             cwd=self.root, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("--release", run.stdout)

    def test_connection_limit_and_read_timeout(self):
        release, _ = self.exported()
        site = site_server.ScoreSite(release, self.root / "runtime")
        server = site_server.make_server(site, port=0, max_connections=1, read_timeout=1)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        address = server.server_address
        slow = socket.create_connection(address, timeout=3)
        self.addCleanup(slow.close)
        slow.sendall(b"GET / HTTP/1.1\r\nHost: example\r\n")
        # Wait for the socket slot, not an arbitrary fixed networking delay.
        deadline = time.monotonic() + 2
        while server.slots.acquire(blocking=False):
            server.slots.release()
            if time.monotonic() > deadline:
                self.fail("first connection was not accepted")
            time.sleep(.01)
        extra = socket.create_connection(address, timeout=3)
        self.addCleanup(extra.close)
        extra.sendall(b"GET /api/health HTTP/1.1\r\nHost: example\r\n\r\n")
        self.assertIn(b"503 Service Unavailable", extra.recv(1024))
        self.assertEqual(slow.recv(1024), b"")
        with closing(HTTPConnection(*address, timeout=3)) as connection:
            connection.request("GET", "/api/health")
            self.assertEqual(connection.getresponse().status, 200)


if __name__ == "__main__":
    unittest.main()
