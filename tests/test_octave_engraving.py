"""Actual engraving must preserve written registers around exact ottava bounds."""
import unittest
from xml.etree import ElementTree as ET
import verovio
from workbench.normalize_engraving import normalize_with_report


def source(stop=2):
    def note(identifier, step, octave, duration, staff=1, chord=False):
        return (f'<note id="{identifier}">' + ('<chord/>' if chord else '')
                + f'<pitch><step>{step}</step><octave>{octave}</octave></pitch>'
                + f'<duration>{duration}</duration><voice>{staff}</voice><type>{"half" if duration==2 else "whole" if duration==4 else "quarter"}</type><staff>{staff}</staff></note>')
    return ('<score-partwise version="4.0"><part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>'
            '<part id="P1"><measure number="1"><attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time><staves>2</staves>'
            '<clef number="1"><sign>G</sign><line>2</line></clef><clef number="2"><sign>F</sign><line>4</line></clef></attributes>'
            '<direction><direction-type><octave-shift type="down" size="8"/></direction-type><offset>1</offset><staff>1</staff></direction>'
            f'<direction><direction-type><octave-shift type="stop" size="8"/></direction-type><offset>{stop}</offset><staff>1</staff></direction>'
            + note('rh0','C',5,1) + note('rh1','D',6,1) + note('rh1b','F',6,1,chord=True)
            + note('rh2','E',5,2) + '<backup><duration>4</duration></backup>' + note('lh0','C',3,4,staff=2)
            + '</measure></part></score-partwise>')


class OctaveEngravingTests(unittest.TestCase):
    def test_leading_offsets_render_only_the_intended_right_hand_chord_an_octave_lower(self):
        xml, report = normalize_with_report(source())
        self.assertTrue(report['musicFingerprintUnchanged'])
        self.assertTrue(report['octaveDirectionTimingUnchanged'])
        self.assertEqual([(x['type'],x['anchorNoteId'],x['placement']) for x in report['octaveDirectionRelocations']], [('down','rh1','before'),('stop','rh1b','after')])
        toolkit=verovio.toolkit()
        self.assertTrue(toolkit.loadData(xml))
        mei=ET.fromstring(toolkit.getMEI())
        ns={'m':'http://www.music-encoding.org/ns/mei'}; xid='{http://www.w3.org/XML/1998/namespace}id'
        notes={n.get(xid):n for n in mei.findall('.//m:note',ns)}
        self.assertEqual({key:notes[key].get('oct') for key in ['rh0','rh1','rh1b','rh2','lh0']}, {'rh0':'5','rh1':'5','rh1b':'5','rh2':'5','lh0':'3'})
        self.assertEqual(notes['rh1'].get('oct.ges'),'6')
        self.assertEqual(notes['rh1b'].get('oct.ges'),'6')
        self.assertEqual(len(mei.findall('.//m:octave',ns)),1)
        original=ET.fromstring(source()); normalized=ET.fromstring(xml)
        self.assertEqual([ET.tostring(n) for n in original.findall('.//note')], [ET.tostring(n) for n in normalized.findall('.//note')])

    def test_unrepresentable_endpoint_stays_explicit_instead_of_guessing(self):
        xml,report=normalize_with_report(source(stop=3))
        self.assertEqual(len(report['unresolvedOctaveOffsets']),1)
        root=ET.fromstring(xml)
        stop=next(d for d in root.findall('.//direction') if d.find('direction-type/octave-shift').get('type')=='stop')
        self.assertEqual(stop.findtext('offset'),'3')

    def test_offset_adaptation_does_not_split_unequal_duration_chord_members(self):
        root=ET.fromstring(source())
        member=root.find(".//note[@id='rh1b']")
        member.find('duration').text='2';member.find('type').text='half'
        xml,report=normalize_with_report(ET.tostring(root,encoding='unicode'))
        self.assertEqual(len(report['unresolvedOctaveOffsets']),1)
        self.assertIn('unequal-duration chord',report['unresolvedOctaveOffsets'][0]['reason'])
        notes=ET.fromstring(xml).findall('.//note')
        self.assertEqual([n.get('id') for n in notes],['rh0','rh1','rh1b','rh2','lh0'])

    def test_grace_order_at_an_exact_boundary_is_not_inferred_from_metric_time(self):
        grace = ('<note id="gr"><grace/><pitch><step>G</step><octave>5</octave></pitch>'
                 '<voice>1</voice><type>eighth</type><staff>1</staff></note>')
        for boundary, following in [('down', 'rh1'), ('stop', 'rh2')]:
            with self.subTest(boundary=boundary):
                original = source().replace(f'<note id="{following}">', grace + f'<note id="{following}">')
                xml, report = normalize_with_report(original)
                unresolved = [item for item in report['unresolvedOctaveOffsets'] if item['type'] == boundary]
                self.assertEqual(len(unresolved), 1)
                self.assertIn('Zero-duration grace/event', unresolved[0]['reason'])
                root = ET.fromstring(xml)
                direction = next(d for d in root.findall('.//direction')
                                 if d.find('direction-type/octave-shift').get('type') == boundary)
                self.assertEqual(direction.findtext('offset'), '1' if boundary == 'down' else '2')
                self.assertNotIn(boundary, [item['type'] for item in report['octaveDirectionRelocations']])
                self.assertEqual([ET.tostring(n) for n in ET.fromstring(original).findall('.//note')],
                                 [ET.tostring(n) for n in root.findall('.//note')])


if __name__=='__main__':
    unittest.main()
