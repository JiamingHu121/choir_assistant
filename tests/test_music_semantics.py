"""Musical contradictions and legal polyphonic notation, not parser mirrors."""
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from workbench.score_model import build_model
from workbench.music_semantics import POLICY_VERSION, BLOCKING_ISSUE_KINDS
from workbench.validation import require_rhythm


def note(step="C", duration=4, voice="1", type_name="whole", extra="", staff=None):
    return (f'<note><pitch><step>{step}</step><octave>4</octave></pitch><duration>{duration}</duration>'
            f'<voice>{voice}</voice><type>{type_name}</type>'
            + (f'<staff>{staff}</staff>' if staff is not None else '') + extra + '</note>')


class MusicalSemanticsTests(unittest.TestCase):
    def test_divisions_changes_follow_xml_order_and_inherit_across_measures(self):
        body = (note(duration=2, type_name='quarter')
                + '<attributes><divisions>6</divisions></attributes>'
                + note('D', 3, type_name='eighth')
                + note('F', 3, type_name='eighth').replace('<note>', '<note><chord/>')
                + '<backup><duration>9</duration></backup><forward><duration>3</duration><voice>2</voice></forward>'
                + note('E', 6, voice='2', type_name='quarter')
                + '<backup><duration>9</duration></backup>'
                + note('G', 9, voice='3', type_name='quarter', extra='<dot/>'))
        model = self.model(self.bar(body, meter='3/8', divisions=2)
                           + '<measure number="2">' + note('A', 9, type_name='quarter', extra='<dot/>') + '</measure>')
        self.assertEqual([n['duration'] for n in model['notes']], [1, .5, .5, 1, 1.5, 1.5])
        self.assertEqual([n['offset'] for n in model['notes']], [0, 1, 1, .5, 0, 0])
        self.assertEqual(model['rhythmValidation']['blockingMeasureIndices'], [])
        xml = ET.fromstring(model['_xml'])
        self.assertEqual([d.text for d in xml.findall('part/measure/attributes/divisions')], ['10080', '10080'])
        self.assertEqual(xml.findtext('part/measure/backup/duration'), '15120')

    def test_reviewed_chord_anchor_uses_its_current_midbar_divisions(self):
        body = (note(duration=2, type_name='quarter') + '<attributes><divisions>6</divisions></attributes>'
                + note('D', 3, type_name='eighth'))
        entry = {'sourcePart': 'P1', 'measureIndex': 1, 'afterNoteId': 'n-P1-m1-o2',
                 'kind': 'chord-tone', 'duration': .5, 'pitch': 65, 'pitchName': 'F4',
                 'reason': 'Synthetic missing chord member', 'evidence': 'source image review'}
        model = self.model(self.bar(body, meter='3/8', divisions=2),
                           {'__insertions__': {'n-review-midbar': entry}})
        member = next(n for n in model['notes'] if n['id'] == 'n-review-midbar')
        self.assertEqual((member['offset'], member['duration']), (1, .5))
        self.assertTrue(member['chordTone'])

    def test_sequential_mid_measure_clefs_are_valid_but_simultaneous_duplicates_warn(self):
        g = '<clef number="1"><sign>G</sign><line>2</line></clef>'
        f = '<clef number="1"><sign>F</sign><line>4</line></clef>'
        changing = ('<attributes>' + g + '</attributes>'
                    + note(duration=2, type_name='half')
                    + '<attributes>' + f + '</attributes>'
                    + note('D', duration=2, type_name='half'))
        model = self.model(self.bar(changing))
        self.assertNotIn('conflicting-clefs', model['audit']['summary']['byKind'])
        xml = ET.fromstring(model['_xml'])
        self.assertEqual([c.findtext('sign') for c in xml.findall('part/measure/attributes/clef')], ['G', 'F'])
        simultaneous = self.model(self.bar('<attributes>' + g + f + '</attributes>' + note()))
        self.assertEqual(simultaneous['audit']['summary']['byKind']['conflicting-clefs'], 1)

    def model(self, measures, corrections=None, parts=None):
        parts = parts or '<part id="P1">' + measures + '</part>'
        xml = ('<score-partwise><part-list><score-part id="P1"><part-name>Test</part-name></score-part>'
               '<score-part id="P2"><part-name>Other</part-name></score-part></part-list>' + parts + '</score-partwise>')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.xml'
            source.write_text(xml)
            return build_model(corrections, source_path=source)

    def bar(self, body, number=1, meter='4/4', divisions=1, implicit=False):
        count, denominator = meter.split('/')
        return (f'<measure number="{number}"' + (' implicit="yes"' if implicit else '') + '>'
                f'<attributes><divisions>{divisions}</divisions><time><beats>{count}</beats>'
                f'<beat-type>{denominator}</beat-type></time></attributes>' + body + '</measure>')

    def test_triplet_positions_remain_exact_and_policy_changes_revision(self):
        triplet = '<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification>'
        model = self.model(self.bar(''.join(note(step, 1, type_name='eighth', extra=triplet) for step in ('C', 'D', 'E')),
                                   meter='1/4', divisions=3))
        self.assertEqual([n['rational']['offset'] for n in model['notes']],
                         [{'numerator': 0, 'denominator': 1}, {'numerator': 1, 'denominator': 3}, {'numerator': 2, 'denominator': 3}])
        self.assertEqual(model['notes'][-1]['rational']['end'], {'numerator': 1, 'denominator': 1})
        self.assertEqual(model['notes'][0]['notation']['timeModification']['multiplier'], {'numerator': 2, 'denominator': 3})
        self.assertEqual(model['modelPolicy'], POLICY_VERSION)
        self.assertEqual(model['schemaVersion'], 2)
        self.assertEqual(model['rhythmValidation']['status'], 'validated')

    def test_visual_review_removes_false_dot_and_staccato_with_provenance(self):
        body = note(duration=3, type_name='eighth', extra='<dot/><notations><articulations><staccato/></articulations></notations>')
        model = self.model(self.bar(body, meter='1/8', divisions=4),
                           {'n-P1-m1-o1': {'duration': .5, 'articulations': [], 'reason': 'original PDF review'}})
        symbol = model['notes'][0]
        self.assertEqual(symbol['notation']['dots'], 0)
        self.assertEqual(symbol['notation']['articulations'], [])
        self.assertEqual(symbol['source']['notation']['dots'], 1)
        self.assertEqual(symbol['source']['notation']['articulations'][0]['type'], 'staccato')
        xml = ET.fromstring(model['_xml'])
        self.assertIsNone(xml.find('part/measure/note/dot'))
        self.assertIsNone(xml.find('part/measure/note/notations/articulations'))
        self.assertEqual(model['audit']['summary']['correctionCategories']['reviewed-articulations'], 1)

    def test_quarter_with_standard_eighth_beam_is_blocking_even_when_duration_agrees(self):
        model = self.model(self.bar(note(duration=1, type_name='quarter', extra='<beam number="1">begin</beam>'), meter='1/4'))
        issue = next(item for item in model['audit']['issues'] if item['kind'] == 'beam-type-contradiction')
        self.assertEqual(issue['evidence']['noteIds'], ['n-P1-m1-o1'])
        self.assertEqual(model['rhythmValidation']['blockingMeasureIndices'], [1])
        self.assertEqual(model['notes'][0]['duration'], 1)
        self.assertIn('beam-type-contradiction', BLOCKING_ISSUE_KINDS)

    def test_full_chord_and_sparse_secondary_voice_are_valid(self):
        body = note() + note('E', extra='<chord/>') + '<backup><duration>2</duration></backup>' + note('G', 2, voice='2', type_name='half')
        model = self.model(self.bar(body))
        self.assertEqual(model['rhythmValidation']['status'], 'validated')
        coverage = model['measures'][0]['laneCoverage']
        self.assertEqual([(lane['voice'], lane['status']) for lane in coverage], [('1', 'complete'), ('2', 'sparse')])
        self.assertEqual(coverage[0]['intervals'][0]['noteIds'], ['n-P1-m1-o1', 'n-P1-m1-o2'])
        self.assertNotIn('lane-overlap', model['audit']['summary']['byKind'])

    def test_explicit_forward_is_silent_coverage_and_is_not_guessed_rest(self):
        body = note(duration=1, type_name='quarter') + '<forward><duration>1</duration><voice>1</voice></forward>' + note('D', 2, type_name='half')
        model = self.model(self.bar(body))
        self.assertEqual(model['rhythmValidation']['status'], 'validated')
        self.assertEqual(model['timelineSpacers'][0]['rational']['offset'], {'numerator': 1, 'denominator': 1})
        self.assertEqual(len(model['rests']), 0)

    def test_empty_explicit_bar_does_not_become_validated_silence(self):
        model = self.model(self.bar(''))
        item = next(item for item in model['audit']['issues'] if item['kind'] == 'staff-underfill')
        self.assertEqual(item['evidence']['gaps'], [[0, 4]])
        self.assertEqual(model['rhythmValidation']['blockingMeasures'], [1])

    def test_explicit_implicit_measure_keeps_free_length_without_regular_bar_overflow(self):
        model = self.model(self.bar(note(duration=6, extra='<dot/>'), implicit=True))
        self.assertEqual(model['totalBeats'], 6)
        self.assertNotIn('bar-overflow', model['audit']['summary']['byKind'])
        self.assertNotIn('lane-overflow', model['audit']['summary']['byKind'])
        self.assertEqual(model['rhythmValidation']['status'], 'validated')

    def test_implicit_forward_tail_is_preserved_before_next_bar_and_rejects_zero(self):
        first = self.bar(note(duration=1, type_name='quarter')
                         + '<forward><duration>3</duration><voice>1</voice><staff>1</staff></forward>', implicit=True)
        model = self.model(first + self.bar(note(), number=2))
        self.assertEqual(model['measures'][0]['observedDuration'], 4)
        self.assertEqual(model['measures'][0]['duration'], 4)
        self.assertEqual(model['measures'][1]['onset'], 4)
        self.assertEqual(model['totalBeats'], 8)
        self.assertEqual(model['measures'][0]['voiceCoverage'][0]['explicitForwardIds'], ['forward-P1-m1-o1'])
        self.assertEqual(require_rhythm(model)['status'], 'rhythm-validated')
        bad = self.model(self.bar(note() + '<forward><duration>0</duration></forward>'))
        self.assertIn('invalid-forward-duration', bad['audit']['summary']['byKind'])
        with self.assertRaises(ValueError):
            require_rhythm(bad)

    def test_cross_staff_voice_and_tie_remain_one_complete_musical_stream(self):
        first = note(duration=2, type_name='half', staff=1, extra='<tie type="start"/>')
        last = note(duration=2, type_name='half', staff=2, extra='<tie type="stop"/>')
        model = self.model(self.bar(first + last))
        self.assertEqual(model['rhythmValidation']['status'], 'validated')
        self.assertNotIn('staff-underfill', model['audit']['summary']['byKind'])
        self.assertEqual([lane['status'] for lane in model['measures'][0]['laneCoverage']], ['sparse', 'sparse'])
        voice = model['measures'][0]['voiceCoverage'][0]
        self.assertTrue(voice['crossStaff'])
        self.assertEqual(voice['status'], 'complete')
        self.assertEqual(voice['uncoveredIntervals'], [])
        self.assertEqual(model['relations']['ties'][0]['staffs'], [1, 2])
        self.assertEqual(len(model['performanceEvents']), 1)
        self.assertEqual(model['performanceEvents'][0]['duration'], 4)
        self.assertEqual(require_rhythm(model)['status'], 'rhythm-validated')

    def test_sparse_voice_is_a_review_candidate_even_when_another_voice_covers_part(self):
        body = note() + '<backup><duration>4</duration></backup>' + note('G', 2, voice='2', type_name='half', staff=2)
        model = self.model(self.bar(body))
        self.assertEqual(model['rhythmValidation']['status'], 'validated')
        warning = next(item for item in model['audit']['issues'] if item['kind'] == 'sparse-voice-unverified')
        self.assertEqual(warning['severity'], 'warning')
        self.assertEqual(warning['evidence']['uncoveredIntervals'], [[2, 4]])
        self.assertEqual(warning['evidence']['voice'], '2')
        self.assertEqual(model['measures'][0]['voiceCoverage'][1]['status'], 'sparse')

    def test_ambiguous_unison_tie_starts_do_not_choose_a_staff_or_arbitrary_endpoint(self):
        body = note(duration=2, type_name='half', staff=1, extra='<tie type="start"/>')
        body += note(duration=2, type_name='half', staff=2, extra='<chord/><tie type="start"/>')
        body += note(duration=2, type_name='half', staff=1, extra='<tie type="stop"/>')
        body += note(duration=2, type_name='half', staff=2, extra='<chord/><tie type="stop"/>')
        model = self.model(self.bar(body))
        self.assertIn('ambiguous-tie', model['audit']['summary']['byKind'])
        self.assertEqual(len(model['performanceEvents']), 4)
        self.assertTrue(all(edge['status'] == 'needs-review' for edge in model['relations']['ties']))
        with self.assertRaises(ValueError):
            require_rhythm(model)

    def test_source_or_corrected_chord_cannot_outlast_anchor_but_shorter_is_legal(self):
        bad = self.model(self.bar(note(duration=2, type_name='half') + note('E', extra='<chord/>')))
        self.assertIn('chord-tone-too-long', bad['audit']['summary']['byKind'])
        with self.assertRaises(ValueError):
            require_rhythm(bad)
        good_body = note() + note('E', 2, type_name='half', staff=2, extra='<chord/>')
        good = self.model(self.bar(good_body))
        self.assertEqual(good['rhythmValidation']['status'], 'validated')
        self.assertEqual(good['notes'][1]['offset'], 0)
        changed = self.model(self.bar(good_body), {'n-P1-m1-o1': {'duration': 1}})
        self.assertIn('chord-tone-too-long', changed['audit']['summary']['byKind'])
        orphan = self.model(self.bar(note(extra='<chord/>')))
        self.assertIn('orphan-chord-tone', orphan['audit']['summary']['byKind'])

    def test_nonzero_instrument_transposition_is_explicitly_gated_until_reset(self):
        first = self.bar(note()).replace('</attributes>', '<transpose><diatonic>-1</diatonic><chromatic>-2</chromatic></transpose></attributes>')
        second = self.bar(note(), number=2)
        third = self.bar(note(), number=3).replace('</attributes>', '<transpose><chromatic>0</chromatic></transpose></attributes>')
        model = self.model(first + second + third)
        self.assertEqual(model['notes'][0]['pitch'], 60)  # importer does not guess sounding pitch
        self.assertEqual(model['rhythmValidation']['blockingMeasureIndices'], [1, 2])
        self.assertEqual(model['notes'][0]['pitchSupport']['status'], 'unsupported-transposition')
        self.assertEqual(model['notes'][2]['pitchSupport']['status'], 'musicxml-pitch')
        self.assertEqual(model['notes'][0]['notation']['musicxmlPitch']['step'], 'C')
        self.assertNotIn('writtenPitch', model['notes'][0]['notation'])
        with self.assertRaises(ValueError):
            require_rhythm(model)

    def test_canonical_export_preserves_other_parts_independently_inherited_meter(self):
        six = note(duration=6, extra='<dot/>')
        first_part = '<part id="P1">' + self.bar(six, meter='12/8') + self.bar(note(), number=2) + '</part>'
        second_part = '<part id="P2">' + self.bar(six, meter='12/8') + '<measure number="2"><note><rest measure="yes"/><duration>6</duration></note></measure></part>'
        model = self.model('', parts=first_part + second_part)
        meter = model['measures'][1]['staffMeters']['P2/staff1']
        self.assertEqual(meter['text'], '12/8')
        self.assertEqual(next(symbol for symbol in model['rests'] if symbol['sourcePart'] == 'P2')['duration'], 6)
        root = ET.fromstring(model['_xml'])
        exported = root.find("part[@id='P2']/measure[@number='2']")
        self.assertEqual(exported.findtext('attributes/time/beats'), '12')
        self.assertEqual(exported.findtext('attributes/time/beat-type'), '8')
        self.assertIn('unresolved-meter-mapping', model['audit']['summary']['byKind'])

    def test_stop_before_start_continuation_exports_two_mei_ties_without_self_loop(self):
        import importlib.util
        if importlib.util.find_spec('verovio') is None:
            self.skipTest('local Verovio runtime is optional')
        import verovio
        start = '<tie type="start"/><notations><tied type="start"/></notations>'
        continuation = '<tie type="start"/><tie type="stop"/><notations><tied type="start"/><tied type="stop"/></notations>'
        stop = '<tie type="stop"/><notations><tied type="stop"/></notations>'
        model = self.model(self.bar(note(duration=2, type_name='half', extra=start)
                                   + note(duration=2, type_name='half', extra=continuation)
                                   + note(duration=2, type_name='half', extra=stop), meter='6/4'))
        current = model['notes'][1]
        self.assertEqual(current['source']['tieTypes'], ['start', 'stop'])
        self.assertEqual(current['tieTypes'], ['stop', 'start'])
        self.assertEqual([mark['type'] for mark in current['source']['notation']['tied']], ['start', 'stop'])
        toolkit = verovio.toolkit()
        self.assertTrue(toolkit.loadData(model['_xml']))
        mei = ET.fromstring(toolkit.getMEI())
        edges = {(node.get('startid'), node.get('endid')) for node in mei.iter() if node.tag.rsplit('}', 1)[-1] == 'tie'}
        self.assertEqual(edges, {('#n-P1-m1-o1', '#n-P1-m1-o2'), ('#n-P1-m1-o2', '#n-P1-m1-o3')})
        self.assertEqual(len(model['performanceEvents']), 1)

    def test_gap_overlap_and_underfill_have_local_symbol_evidence(self):
        # This forward belongs to a different voice; it does not explain voice 1.
        gap = note(duration=1, type_name='quarter') + '<forward><duration>1</duration><voice>2</voice></forward>' + note('D', 2, type_name='half')
        model = self.model(self.bar(gap))
        item = next(item for item in model['audit']['issues'] if item['kind'] == 'lane-gap')
        self.assertEqual(item['evidence']['gap'], [1, 2])
        self.assertEqual(item['evidence']['noteIds'], ['n-P1-m1-o1', 'n-P1-m1-o2'])
        overlap = note(duration=2, type_name='half') + '<backup><duration>1</duration></backup>' + note('D', 2, type_name='half')
        model = self.model(self.bar(overlap))
        self.assertIn('lane-overlap', model['audit']['summary']['byKind'])
        self.assertIn('staff-underfill', model['audit']['summary']['byKind'])
        self.assertEqual(model['measures'][0]['rhythmStatus'], 'needs-review')

    def test_tie_graph_merges_exact_segments_and_slur_never_extends_duration(self):
        body = note(duration=2, type_name='half', extra='<tie type="start"/><notations><tied type="start"/><slur type="start" number="1"/></notations>')
        body += note(duration=2, type_name='half', extra='<tie type="stop"/><notations><tied type="stop"/><slur type="stop" number="1" bezier-x="4000"/></notations>')
        model = self.model(self.bar(body))
        self.assertEqual(model['rhythmValidation']['status'], 'validated')
        self.assertEqual(model['relations']['ties'][0]['from'], 'n-P1-m1-o1')
        self.assertEqual(model['relations']['ties'][0]['to'], 'n-P1-m1-o2')
        self.assertEqual(model['relations']['slurs'][0]['memberNoteIds'], ['n-P1-m1-o1', 'n-P1-m1-o2'])
        self.assertEqual(len(model['performanceEvents']), 1)
        self.assertEqual(model['performanceEvents'][0]['rational']['duration'], {'numerator': 4, 'denominator': 1})
        self.assertFalse(model['notes'][1]['attack'])

    def test_wrong_pitch_open_and_noncontiguous_ties_are_not_merged(self):
        first = self.bar(note(extra='<tie type="start"/>'))
        wrong_pitch = self.bar(note('D', extra='<tie type="stop"/>'), number=2)
        model = self.model(first + wrong_pitch)
        self.assertEqual(len(model['performanceEvents']), 2)
        self.assertEqual(len(model['relations']['ties']), 2)
        self.assertEqual(model['rhythmValidation']['blockingMeasureIndices'], [1, 2])
        delayed = self.bar(note(duration=1, type_name='quarter', extra='<tie type="start"/>')
                           + '<forward><duration>1</duration></forward>'
                           + note(duration=2, type_name='half', extra='<tie type="stop"/>'))
        model = self.model(delayed)
        self.assertEqual(model['relations']['ties'][0]['status'], 'needs-review')
        self.assertIn('noncontiguous-tie', model['audit']['summary']['byKind'])
        self.assertEqual(len(model['performanceEvents']), 2)

    def test_reviewed_tie_and_slur_endpoints_sync_xml_and_preserve_source_marks(self):
        first = '<notations><slur type="start" number="1"/><slur type="start" number="2"/></notations>'
        second = '<notations><slur type="stop" number="1"/><slur type="stop" number="2"/></notations>'
        body = note(duration=2, type_name='half', extra=first) + note(duration=2, type_name='half', extra=second)
        changes = {'n-P1-m1-o1': {'tieTypes': ['start'], 'slurs': [{'type': 'start', 'number': '1'}]},
                   'n-P1-m1-o2': {'tieTypes': ['stop'], 'slurs': [{'type': 'stop', 'number': '1'}]}}
        model = self.model(self.bar(body), changes)
        self.assertEqual(model['rhythmValidation']['status'], 'validated')
        self.assertEqual(len(model['relations']['ties']), 1)
        self.assertEqual(len(model['relations']['slurs']), 1)
        self.assertEqual(len(model['performanceEvents']), 1)
        self.assertEqual(model['notes'][0]['source']['tieTypes'], [])
        self.assertEqual(len(model['notes'][0]['source']['notation']['slurs']), 2)
        xml = ET.fromstring(model['_xml'])
        for symbol, expected in zip(xml.findall('part/measure/note'), ('start', 'stop')):
            self.assertEqual(symbol.find('tie').get('type'), expected)
            self.assertEqual(symbol.find('notations/tied').get('type'), expected)
            self.assertEqual(len(symbol.findall('notations/slur')), 1)
        clear = self.model(self.bar(note(extra='<tie type="start"/><notations><tied type="start"/><slur type="start"/></notations>')),
                           {'n-P1-m1-o1': {'tieTypes': [], 'slurs': []}})
        self.assertEqual(clear['relations'], {'ties': [], 'slurs': []})
        self.assertEqual(clear['notes'][0]['source']['tieTypes'], ['start'])
        self.assertEqual(clear['rhythmValidation']['status'], 'validated')
    def test_independent_staff_meters_remain_independent_until_reviewed(self):
        parts = '<part id="P1">' + self.bar(note()) + '</part><part id="P2">' + self.bar(note(duration=6, extra='<dot/>'), meter='12/8') + '</part>'
        model = self.model('', parts=parts)
        meters = model['measures'][0]['staffMeters']
        self.assertEqual(meters['P1/staff1']['text'], '4/4')
        self.assertEqual(meters['P2/staff1']['text'], '12/8')
        self.assertEqual(model['measures'][0]['meterMapping']['status'], 'needs-review')
        self.assertIn('unresolved-meter-mapping', model['audit']['summary']['byKind'])
        reviewed = self.model('', {'__measures__': {'1': {'timeSignature': '12/8'}}}, parts=parts)
        self.assertEqual({meter['text'] for meter in reviewed['measures'][0]['staffMeters'].values()}, {'12/8'})
        self.assertEqual(reviewed['notes'][0]['duration'], 4)  # meter review does not stretch notes

    def test_invalid_tuplet_does_not_crash_or_guess_a_ratio(self):
        body = note(duration=1, type_name='eighth', extra='<time-modification><actual-notes>0</actual-notes><normal-notes>2</normal-notes></time-modification>')
        model = self.model(self.bar(body, meter='1/4'))
        self.assertIn('invalid-time-modification', model['audit']['summary']['byKind'])
        self.assertEqual(model['notes'][0]['duration'], 1)

    def test_reviewed_rest_insertion_preserves_source_ids_offsets_and_tuples(self):
        fake_tuplet = '<time-modification><actual-notes>3</actual-notes><normal-notes>2</normal-notes></time-modification><notations><tuplet type="start"/></notations>'
        body = note(duration=1, type_name='eighth', extra=fake_tuplet) + note('D', 1, type_name='quarter')
        changes = {'n-P1-m1-o1': {'duration': .5, 'tuplets': []},
                   '__insertions__': {'r-review-P1-m1-1': {'sourcePart': 'P1', 'measureIndex': 1,
                       'afterNoteId': 'n-P1-m1-o1', 'kind': 'rest', 'duration': .5,
                       'reason': 'rest visibly present in source PDF', 'evidence': 'PDF page 1 crop'}}}
        model = self.model(self.bar(body, meter='2/4'), changes)
        self.assertEqual([symbol['id'] for symbol in model['notes']], ['n-P1-m1-o1', 'n-P1-m1-o2'])
        self.assertEqual(model['notes'][1]['source']['offset'], 1)
        self.assertEqual(model['notes'][1]['offset'], 1)
        self.assertEqual(model['rests'][0]['id'], 'r-review-P1-m1-1')
        self.assertTrue(model['rests'][0]['source']['generated'])
        self.assertIsNone(model['rests'][0]['source']['noteOrdinal'])
        self.assertIsNone(model['rests'][0]['source']['notation'])
        self.assertEqual(model['notes'][0]['notation']['tuplets'], [])
        self.assertEqual(model['notes'][0]['source']['notation']['tuplets'][0]['type'], 'start')
        self.assertEqual(model['rhythmValidation']['status'], 'validated')
        exported = ET.fromstring(model['_xml'])
        self.assertEqual([symbol.get('id') for symbol in exported.findall('part/measure/note')],
                         ['n-P1-m1-o1', 'r-review-P1-m1-1', 'n-P1-m1-o2'])
        self.assertIsNone(exported.find('part/measure/note/notations/tuplet'))

    def test_reviewed_insertion_cannot_split_chord_or_cross_lanes_or_lack_evidence(self):
        definitions = {'sourcePart': 'P1', 'measureIndex': 1, 'afterNoteId': 'n-P1-m1-o1',
                       'kind': 'rest', 'duration': .5, 'reason': 'PDF review', 'evidence': 'crop'}
        chord = self.bar(note() + note('E', extra='<chord/>'))
        with self.assertRaisesRegex(ValueError, 'split a simultaneous chord'):
            self.model(chord, {'__insertions__': {'r-review-a': definitions}})
        with self.assertRaisesRegex(ValueError, 'unknown sourcePart'):
            self.model(self.bar(note()), {'__insertions__': {'r-review-a': definitions | {'sourcePart': 'P2'}}})
        with self.assertRaisesRegex(ValueError, 'requires reason'):
            self.model(self.bar(note()), {'__insertions__': {'r-review-a': definitions | {'evidence': None}}})
        with self.assertRaisesRegex(ValueError, "anchor's staff and voice"):
            self.model(self.bar(note()), {'__insertions__': {'r-review-a': definitions | {'voice': '2'}}})

    def test_reviewed_chord_tone_preserves_simultaneity_source_ids_and_coverage(self):
        body = note(duration=2, type_name='half') + note('G', 2, type_name='half')
        inserted = {'sourcePart': 'P1', 'measureIndex': 1, 'afterNoteId': 'n-P1-m1-o1',
                    'kind': 'chord-tone', 'duration': 2, 'pitch': 64, 'pitchName': 'E4',
                    'reason': 'visible missing chord head', 'evidence': 'PDF crop', 'status': 'source-verified'}
        model = self.model(self.bar(body), {'__insertions__': {'n-review-P1-m1-1': inserted}})
        self.assertEqual([symbol['id'] for symbol in model['notes']], ['n-P1-m1-o1', 'n-review-P1-m1-1', 'n-P1-m1-o2'])
        self.assertEqual([symbol['offset'] for symbol in model['notes']], [0, 0, 2])
        added = model['notes'][1]
        self.assertEqual(added['chordId'], model['notes'][0]['id'])
        self.assertTrue(added['chordTone'])
        self.assertTrue(added['source']['generated'])
        self.assertIsNone(added['source']['pitch'])
        self.assertEqual(added['pitchName'], 'E4')
        self.assertEqual(added['source']['insertion']['status'], 'source-verified')
        self.assertEqual(model['rhythmValidation']['status'], 'validated')
        self.assertEqual(model['totalBeats'], 4)
        self.assertEqual(len(model['performanceEvents']), 3)
        with self.assertRaisesRegex(ValueError, 'duplicates an existing pitch'):
            self.model(self.bar(body), {'__insertions__': {'n-review-a': inserted | {'pitch': 60, 'pitchName': 'C4'}}})
        with self.assertRaisesRegex(ValueError, 'must equal its corrected anchor'):
            self.model(self.bar(body), {'__insertions__': {'n-review-a': inserted | {'duration': 1}}})
        with self.assertRaisesRegex(ValueError, 'pitchName and pitch disagree'):
            self.model(self.bar(body), {'__insertions__': {'n-review-a': inserted | {'pitchName': 'F4'}}})

    def test_an_explicit_source_is_required(self):
        with self.assertRaisesRegex(ValueError, 'explicit MusicXML'):
            build_model()


if __name__ == '__main__':
    unittest.main()
