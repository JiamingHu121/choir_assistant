"""Reviewed display annotations survive music pulls without changing bundles."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import shutil
from unittest import TestCase, mock

from choir_assistant.deployment import catalog, editor_sync, export
import test_editor_sync as pull_fixtures


class EditorOverlayPullTests(TestCase):
    def setUp(self):
        self.base = pull_fixtures.EditorPullTests('runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.library, self.root = self.base.library, self.base.root
        self.entry_path = self.base.entry_path
        self.overlay_path = self.root / 'reviewed-lyrics.json'
        self.overlay = {
            'schemaVersion': 1, 'revision': 'fixture-v1', 'sourceSha256': self.base.source_hash,
            'replaceUnverifiedLyrics': True,
            'coverage': {'status': 'reviewed', 'method': 'original PDF reading', 'sourcePages': [1]},
            'measureLyrics': [{'measure': 1, 'part': 'S', 'lines': ['已校对的唱词'],
                              'source': {'page': 1, 'imageSha256': 'c' * 64, 'method': 'direct reading',
                                         'evidence': str(self.root / 'private-reviewed.png')}}],
        }
        catalog.write_json(self.overlay_path, self.overlay)
        entry = catalog.read_json(self.entry_path)
        entry['movements'][0]['notationOverlay'] = str(self.overlay_path)
        entry['movements'][0]['sourceReviewed'] = True
        entry['movements'][0]['reviewReport'] = 'old report'
        catalog.write_json(self.entry_path, entry)
        self.original_entry = self.entry_path.read_bytes()
        self.original_overlay = self.overlay_path.read_bytes()
        self.original_manifest = (self.base.fixture.bundle / 'manifest.json').read_bytes()

    def pull(self, archive=None, head=None):
        def download(work, movement, actual_head, destination):
            shutil.copyfile(archive or self.base.archive, destination)
        return editor_sync.pull_online_edits(self.base.profile, {'study/main': head or self.base.head},
                                            mock.Mock(download=download))

    def movement(self):
        return catalog.entries(self.library)['study']['movements'][0]

    def assert_unchanged(self, watermark=None, cached=None):
        self.assertEqual(self.entry_path.read_bytes(), self.original_entry)
        path = self.library / '.online-editor-heads.json'
        self.assertEqual(path.read_bytes() if path.exists() else None, watermark)
        if cached is not None:
            self.assertEqual((self.library / 'catalog.json').read_bytes(), cached)
        self.assertEqual(self.overlay_path.read_bytes(), self.original_overlay)

    def test_pitch_then_duration_edits_rebind_exact_reviewed_fields_and_export(self):
        self.pull()
        first = self.movement()
        first_path = Path(first['notationOverlay'])
        first_bytes = first_path.read_bytes()
        self.assertEqual(first_path.parent, Path(first['bundle']).parent)
        self.assertFalse(first_path.is_relative_to(Path(first['bundle'])))
        value = catalog.read_json(first_path)
        expected = deepcopy(self.overlay)
        for key in self.overlay:
            if key != 'revision': self.assertEqual(value[key], expected[key])
        self.assertEqual(value['revision'], 'online-v2')
        self.assertEqual(value['derivedFromRevision'], 'fixture-v1')
        self.assertEqual(value['sourceOverlaySha256'], sha256(self.original_overlay).hexdigest())
        self.assertIn('measure-bound', value['derivation'])
        self.assertFalse(first['sourceReviewed'])
        self.assertNotIn('reviewReport', first)

        next_root = self.root / 'duration-edit'; next_root.mkdir()
        archive, head, _ = pull_fixtures.snapshot(self.base.fixture.bundle, next_root, self.base.source_hash,
                                    revision='online-v3', model_transform=lambda model: model['notes'][0].update(duration=1))
        self.pull(archive, head)
        final = self.movement(); final_overlay = catalog.read_json(final['notationOverlay'])
        self.assertEqual(final_overlay['revision'], 'online-v3')
        self.assertEqual(final_overlay['derivedFromRevision'], 'online-v2')
        self.assertEqual(final_overlay['sourceOverlaySha256'], sha256(first_bytes).hexdigest())
        self.assertEqual(final_overlay['measureLyrics'], self.overlay['measureLyrics'])
        self.assertEqual(first_path.read_bytes(), first_bytes)
        self.assertEqual(self.overlay_path.read_bytes(), self.original_overlay)
        self.assertEqual((self.base.fixture.bundle / 'manifest.json').read_bytes(), self.original_manifest)
        self.assertEqual(self.base.pdf.read_bytes(), self.base.original_pdf)
        self.assertFalse((Path(final['bundle']) / 'notation-overlay.json').exists())
        destination = self.root / 'export-with-lyrics'
        export.export_site(destination, self.library)
        published = catalog.read_json(destination / 'data/study/main/notation-overlay.json')
        self.assertEqual(published['revision'], 'online-v3')
        self.assertEqual(published['measureLyrics'][0]['lines'], ['已校对的唱词'])
        self.assertEqual(published['measureLyrics'][0]['source']['imageSha256'], 'c' * 64)

    def test_changed_topology_fails_before_entry_catalog_or_watermark(self):
        catalog.write_json(self.library / 'catalog.json', {'works': []})
        cached = (self.library / 'catalog.json').read_bytes()
        mutations = [lambda model: model['notes'][0].update(id='other-note'),
                     lambda model: model['notes'][0].update(staff=2),
                     lambda model: model['notes'][0].update(voice='2'),
                     lambda model: model['measures'][0].update(number='2'),
                     lambda model: model['parts'][0].update(sourcePart='P2')]
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                folder = self.root / ('topology-%s' % index); folder.mkdir()
                archive, head, _ = pull_fixtures.snapshot(self.base.fixture.bundle, folder, self.base.source_hash,
                                            model_transform=mutation)
                with self.assertRaisesRegex(ValueError, 'topology changed'):
                    self.pull(archive, head)
                self.assert_unchanged(cached=cached)

    def test_revision_pdf_or_bundle_binding_mismatch_is_not_silently_dropped(self):
        for field, value in (('revision', 'unrelated'), ('sourceSha256', '0' * 64)):
            with self.subTest(field=field):
                catalog.write_json(self.overlay_path, dict(self.overlay, **{field: value}))
                with self.assertRaisesRegex(ValueError, 'identity mismatch'):
                    self.pull()
                self.assertEqual(self.entry_path.read_bytes(), self.original_entry)
                self.assertFalse((self.library / '.online-editor-heads.json').exists())
        self.overlay_path.write_bytes(self.original_overlay)
        model_path = self.base.fixture.bundle / 'assets/interactive.json'
        value = catalog.read_json(model_path); value['notes'][0]['voice'] = 'changed-without-manifest'
        catalog.write_json(model_path, value)
        with self.assertRaisesRegex(ValueError, 'base model'):
            self.pull()
        self.assert_unchanged()

    def test_existing_import_rehydrates_only_exact_nonlinked_sidecar(self):
        self.pull()
        sidecar = Path(self.movement()['notationOverlay'])
        expected, timestamp = sidecar.read_bytes(), sidecar.stat().st_mtime_ns
        self.entry_path.write_bytes(self.original_entry)
        (self.library / '.online-editor-heads.json').unlink()
        self.pull()
        self.assertEqual(sidecar.read_bytes(), expected)
        self.assertEqual(sidecar.stat().st_mtime_ns, timestamp)

        self.entry_path.write_bytes(self.original_entry)
        (self.library / '.online-editor-heads.json').unlink()
        sidecar.unlink()
        self.pull()
        self.assertEqual(sidecar.read_bytes(), expected)

        self.entry_path.write_bytes(self.original_entry)
        (self.library / '.online-editor-heads.json').unlink()
        sidecar.write_bytes(b'conflicting reviewed text')
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            self.pull()
        self.assert_unchanged()
        sidecar.unlink(); sidecar.symlink_to(self.overlay_path)
        with self.assertRaisesRegex(ValueError, 'links'):
            self.pull()
        self.assert_unchanged()

    def test_no_overlay_keeps_ordinary_pull_behavior(self):
        entry = catalog.read_json(self.entry_path)
        entry['movements'][0].pop('notationOverlay')
        catalog.write_json(self.entry_path, entry)
        self.pull()
        self.assertNotIn('notationOverlay', self.movement())
        self.assertFalse((Path(self.movement()['bundle']).parent / 'notation-overlay.json').exists())
