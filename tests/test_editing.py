"""User-edit contracts without OMR, a server, or live publication."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from workbench.editing import (DURATIONS, _baseline_migration, read_frozen_bundle, validate_edit_delta,
                               validate_patch)
from workbench.score_model import build_model
from workbench.workflow import file_hash, prepare_model, write_json


def note(step="C", duration=1, type_name="quarter", extra="", chord=False, voice="1"):
    return ('<note>' + ('<chord/>' if chord else '') + '<pitch><step>' + step +
            '</step><octave>4</octave></pitch><duration>' + str(duration * 8) +
            '</duration><voice>' + voice + '</voice><type>' + type_name + '</type>' + extra + '</note>')


def score(body, meter="4/4"):
    beats, denominator = meter.split('/')
    return ('<score-partwise><part-list><score-part id="P1"><part-name>Soprano</part-name>'
            '</score-part></part-list><part id="P1"><measure number="1"><attributes>'
            '<divisions>8</divisions><time><beats>' + beats + '</beats><beat-type>' + denominator +
            '</beat-type></time></attributes>' + body + '</measure></part></score-partwise>')


class EditingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source.musicxml'

    def tearDown(self):
        self.temporary.cleanup()

    def model(self, body, meter="4/4", corrections=None):
        self.source.write_text(score(body, meter), encoding='utf-8')
        model = build_model(corrections, source_path=self.source, omr_path='')
        model['revision'] = model['version']
        return model

    def patch(self, model, operations, **kwargs):
        return validate_patch(model, operations, expected_revision=model['revision'], **kwargs)

    def rebuild(self, model, patch):
        ledger = {ident: {**values, 'reason': 'Explicit user edit'}
                  for ident, values in patch['corrections'].items()}
        return build_model(ledger, source_path=self.source, omr_path='')

    def test_pitch_only_preserves_tuplet_notation_and_original_model(self):
        ratio = '<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification>'
        model = self.model(note(duration=1/3, type_name='eighth', extra=ratio), meter='1/4')
        original = deepcopy(model)
        ident = model['notes'][0]['id']
        patch = self.patch(model, [{'op': 'pitch.delta', 'noteIds': [ident], 'semitones': 1}])
        self.assertEqual(set(patch['corrections'][ident]), {'pitch', 'pitchName'})
        after = self.rebuild(model, patch)
        self.assertEqual(after['notes'][0]['pitchName'], 'C#4')
        self.assertEqual(after['notes'][0]['notation']['timeModification'], original['notes'][0]['notation']['timeModification'])
        self.assertEqual(after['notes'][0]['duration'], original['notes'][0]['duration'])
        self.assertEqual(model, original)

    def test_two_ordered_semitones_accumulate_and_keep_letter_spelling(self):
        model = self.model(note(duration=4, type_name='whole'))
        ident = model['notes'][0]['id']
        operation = {'op': 'pitch.delta', 'noteIds': [ident], 'semitones': 1}
        patch = self.patch(model, [operation, operation])
        self.assertEqual(patch['corrections'][ident], {'pitch': 62, 'pitchName': 'C##4'})
        self.assertEqual(self.rebuild(model, patch)['notes'][0]['pitch'], 62)

    def test_repeated_pitch_steps_respells_beyond_double_accidentals(self):
        cases = [('F', 1, 1, 2, 68, 'G#4'),
                 ('B', 0, 1, 3, 74, 'D5'),
                 ('C', 0, -1, 3, 57, 'A3'),
                 ('G', -1, -1, 2, 64, 'E4')]
        for step, alter, direction, count, target, spelling in cases:
            with self.subTest(step=step, alter=alter, direction=direction):
                body = note(step, duration=4, type_name='whole').replace(
                    '</step>', '</step><alter>' + str(alter) + '</alter>')
                model = self.model(body)
                original = deepcopy(model)
                ident = model['notes'][0]['id']
                operation = {'op': 'pitch.delta', 'noteIds': [ident], 'semitones': direction}
                patch = self.patch(model, [operation] * count)
                self.assertEqual(patch['corrections'][ident], {'pitch': target, 'pitchName': spelling})
                self.assertEqual(patch['respellings'][-1]['after'], spelling)
                after = self.rebuild(model, patch)
                self.assertEqual(after['notes'][0]['pitch'], target)
                self.assertEqual(after['notes'][0]['pitchName'], spelling)
                for field in ('onset', 'duration', 'end', 'source'):
                    self.assertEqual(after['notes'][0].get(field), original['notes'][0].get(field))
                self.assertEqual(after['provenance']['sourceSha256'], original['provenance']['sourceSha256'])
                self.assertEqual(model, original)

    def test_stale_revision_before_value_unknown_or_extra_fields_rejected(self):
        model = self.model(note(duration=4, type_name='whole'))
        ident = model['notes'][0]['id']
        operation = {'op': 'pitch.delta', 'noteIds': [ident], 'semitones': 1}
        with self.assertRaisesRegex(ValueError, 'revision'):
            validate_patch(model, [operation], expected_revision='old')
        with self.assertRaisesRegex(ValueError, 'Before-value'):
            self.patch(model, [operation], before_values={ident: {'pitch': 61}})
        for changed in ({**operation, 'noteIds': ['unknown']}, {**operation, 'duration': 2},
                        {**operation, 'semitones': 12}, {**operation, 'noteIds': []}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.patch(model, [changed])

    def test_before_values_are_base_snapshot_across_multiple_operations(self):
        model = self.model(note(duration=4, type_name='whole'))
        ident = model['notes'][0]['id']
        operation = {'op': 'pitch.delta', 'noteIds': [ident], 'delta': 1}
        envelope = {'baseModelRevision': model['revision'], 'selectedNoteIds': [ident],
                    'beforeValues': {ident: {'pitch': 60, 'duration': 4}},
                    'typedOperations': [operation, operation]}
        self.assertEqual(validate_patch(model, envelope)['corrections'][ident]['pitch'], 62)

    def test_all_duration_presets_and_dot_are_exact(self):
        for type_name, length in DURATIONS.items():
            for dots in (0, 1):
                with self.subTest(type_name=type_name, dots=dots):
                    model = self.model(note(duration=4, type_name='whole'))
                    ident = model['notes'][0]['id']
                    patch = self.patch(model, [{'op': 'duration.set', 'noteIds': [ident], 'type': type_name, 'dots': dots}])
                    after = self.rebuild(model, patch)
                    expected = length * (1.5 if dots else 1)
                    self.assertEqual(after['notes'][0]['duration'], float(expected))
                    self.assertEqual(after['notes'][0]['notation']['type'], type_name)
                    self.assertEqual(after['notes'][0]['notation']['dots'], dots)

    def test_partial_tie_chain_rejected_whole_chain_pitch_changes(self):
        start = '<tie type="start"/><notations><tied type="start"/></notations>'
        stop = '<tie type="stop"/><notations><tied type="stop"/></notations>'
        model = self.model(note(duration=2, type_name='half', extra=start) + note(duration=2, type_name='half', extra=stop))
        ids = [n['id'] for n in model['notes']]
        with self.assertRaisesRegex(ValueError, 'Partial tie'):
            self.patch(model, [{'op': 'pitch.delta', 'noteIds': ids[:1], 'semitones': 1}])
        patch = self.patch(model, [{'op': 'pitch.delta', 'noteIds': ids, 'semitones': 1}])
        after = self.rebuild(model, patch)
        self.assertEqual(len(after['performanceEvents']), 1)
        self.assertTrue(validate_edit_delta(model, after, patch)['canPublish'])

    def test_beam_group_requires_complete_selection_and_rebeams_shorter_values(self):
        body = note(duration=.5, type_name='eighth', extra='<beam number="1">begin</beam>')
        body += note(step='D', duration=.5, type_name='eighth', extra='<beam number="1">end</beam>')
        model = self.model(body, meter='1/4')
        ids = [n['id'] for n in model['notes']]
        with self.assertRaisesRegex(ValueError, 'Partial beam'):
            self.patch(model, [{'op': 'duration.set', 'noteIds': ids[:1], 'type': 'quarter'}])
        for type_name, levels in [('quarter', 0), ('16th', 2), ('32nd', 3)]:
            patch = self.patch(model, [{'op': 'duration.set', 'noteIds': ids, 'type': type_name}])
            after = self.rebuild(model, patch)
            self.assertEqual([len(n['notation']['beams']) for n in after['notes']], [levels, levels])
            self.assertNotIn('beam-type-contradiction', after['audit']['summary']['byKind'])

    def test_chord_duration_requires_explicit_whole_chord_and_pitch_does_not(self):
        model = self.model(note(duration=4, type_name='whole') + note(step='E', duration=4, type_name='whole', chord=True))
        ids = [n['id'] for n in model['notes']]
        with self.assertRaisesRegex(ValueError, 'Partial chord'):
            self.patch(model, [{'op': 'duration.set', 'noteIds': ids[:1], 'type': 'half'}])
        patch = self.patch(model, [{'op': 'pitch.delta', 'noteIds': ids[:1], 'semitones': 1}])
        after = self.rebuild(model, patch)
        self.assertEqual([n['pitch'] for n in after['notes']], [61, 64])
        self.assertEqual([n['offset'] for n in after['notes']], [0, 0])

    def test_plain_duration_replaces_a_complete_tuplet_but_not_unbounded_ratio(self):
        ratio = '<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification>'
        start = ratio + '<notations><tuplet type="start" number="1"/></notations>'
        end = ratio + '<notations><tuplet type="stop" number="1"/></notations>'
        model = self.model(note(duration=1/3, type_name='eighth', extra=start) +
                           note(step='D', duration=1/3, type_name='eighth', extra=ratio) +
                           note(step='E', duration=1/3, type_name='eighth', extra=end), meter='1/4')
        ids = [n['id'] for n in model['notes']]
        with self.assertRaisesRegex(ValueError, 'Partial tuplet'):
            self.patch(model, [{'op': 'duration.set', 'noteIds': ids[:1], 'type': 'eighth'}])
        patch = self.patch(model, [{'op': 'duration.set', 'noteIds': ids, 'type': 'eighth'}])
        after = self.rebuild(model, patch)
        self.assertTrue(all(not n['notation']['tuplets'] and n['notation']['timeModification'] is None for n in after['notes']))
        unbounded = self.model(note(duration=1/3, type_name='eighth', extra=ratio), meter='1/4')
        with self.assertRaisesRegex(ValueError, 'Unbounded tuplet'):
            self.patch(unbounded, [{'op': 'duration.set', 'noteIds': [unbounded['notes'][0]['id']], 'type': 'eighth'}])

    def test_duration_reflows_later_notes_and_new_underfill_is_unpublishable(self):
        model = self.model(note(duration=2, type_name='half') + note(step='D', duration=2, type_name='half'))
        ident = model['notes'][0]['id']
        patch = self.patch(model, [{'op': 'duration.set', 'noteIds': [ident], 'type': 'quarter'}])
        after = self.rebuild(model, patch)
        self.assertEqual(after['notes'][1]['offset'], 1)
        delta = validate_edit_delta(model, after, patch)
        self.assertFalse(delta['canPublish'])
        self.assertIn('staff-underfill', [i['kind'] for i in delta['newIssues']])

    def test_existing_conflicts_preserved_but_increased_overflow_rejected(self):
        model = self.model(note(duration=4, type_name='whole'), meter='3/4')
        ident = model['notes'][0]['id']
        pitch = self.patch(model, [{'op': 'pitch.delta', 'noteIds': [ident], 'semitones': 1}])
        delta = validate_edit_delta(model, self.rebuild(model, pitch), pitch)
        self.assertTrue(delta['canPublish'])
        self.assertTrue(delta['remainingBaselineIssues'])
        lengthen = self.patch(model, [{'op': 'duration.set', 'noteIds': [ident], 'type': 'whole', 'dots': 1}])
        self.assertFalse(validate_edit_delta(model, self.rebuild(model, lengthen), lengthen)['canPublish'])

    def test_invalid_reviewed_beams_are_rejected(self):
        self.source.write_text(score(note(duration=4, type_name='whole')), encoding='utf-8')
        for value in ([{'number': 1, 'value': 'unsafe'}], [{'number': 0, 'value': 'begin'}], 'begin'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_model({'n-P1-m1-o1': {'beams': value, 'reason': 'Bad beam'}}, source_path=self.source, omr_path='')

    def test_policy_migration_requires_all_musical_objects_exactly_equivalent(self):
        model = self.model(note(duration=4, type_name='whole'))
        old = deepcopy(model)
        old['modelPolicy'] = 'music-semantics-v2.3'
        old['revision'] = 'old-revision'
        old['notes'][0]['svgPage'] = 1
        migration = _baseline_migration(old, model)
        self.assertTrue(migration['musicalObjectsExactlyEquivalent'])
        self.assertFalse(migration['sourceReviewed'])
        old['notes'][0]['pitch'] += 1
        with self.assertRaisesRegex(ValueError, 'changes notes'):
            _baseline_migration(old, model)

    def test_policy_migration_only_ignores_rest_placeholder_and_coverage_metadata(self):
        model = self.model('<note><rest measure="yes"/><duration>32</duration></note>')
        old = deepcopy(model)
        old['revision'] = 'old-policy-revision'
        old['rests'][0]['placeholder'] = True
        old['measures'][0]['voiceCoverage'] = []
        migration = _baseline_migration(old, model)
        self.assertEqual(migration['metadataChanges']['restPlaceholderIds'], ['n-P1-m1-o1'])
        self.assertTrue(migration['metadataChanges']['measureCoverageRecomputed'])
        old['rests'][0]['duration'] = 3
        with self.assertRaisesRegex(ValueError, 'changes rests'):
            _baseline_migration(old, model)

    def test_unrelated_ambiguous_beams_do_not_block_pitch_edits(self):
        model = self.model(note(duration=1, extra='<beam number="1">begin</beam>') +
                           note(step='D', duration=1, extra='<beam number="1">begin</beam>') +
                           note(step='E', duration=2, type_name='half'))
        ident = model['notes'][-1]['id']
        patch = self.patch(model, [{'op': 'pitch.delta', 'noteIds': [ident], 'semitones': -1}])
        self.assertEqual(patch['corrections'][ident]['pitch'], 63)


class FrozenBundleTests(unittest.TestCase):
    def test_modified_or_external_frozen_inputs_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'assets').mkdir()
            (root / 'inputs').mkdir()
            write_json(root / 'assets/interactive.json', {'revision': 'r1'})
            write_json(root / 'inputs/corrections.json', {})
            (root / 'inputs/source.musicxml').write_text('<score-partwise/>')
            artifacts = [{'path': p.relative_to(root).as_posix(), 'sha256': file_hash(p)}
                         for p in root.rglob('*') if p.is_file()]
            manifest = {'status': 'complete', 'revision': 'r1', 'artifacts': artifacts,
                        'input': {'musicxml': 'inputs/source.musicxml'}, 'config': {}}
            write_json(root / 'manifest.json', manifest)
            self.assertEqual(read_frozen_bundle(root)['model']['revision'], 'r1')
            (root / 'inputs/source.musicxml').write_text('tampered')
            with self.assertRaisesRegex(ValueError, 'Frozen artifact changed'):
                read_frozen_bundle(root)
            manifest['artifacts'].insert(0, {'path': '../escape', 'sha256': '0' * 64})
            write_json(root / 'manifest.json', manifest)
            with self.assertRaisesRegex(ValueError, 'escapes'):
                read_frozen_bundle(root)


if __name__ == '__main__':
    unittest.main()
