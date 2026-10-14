"""Normalize a derived MusicXML copy for Verovio without changing music events.

Audiveris repeats a chord's beam indication on every chord tone. Verovio 6.3
imports that repetition as nested beam/chord containers and can then drop entire
runs of notes. Here only *identical* beam indications on additional chord tones
are removed; the chord's first note retains the original beam. Chord membership,
voice, staff, pitches, durations, ties, slurs, and original stable IDs are kept.
Positive octave-direction offsets are also anchored to exact staff events in
the rendering copy. Their musical times and actual pitches remain unchanged.

API:
    normalize(path_or_xml) -> normalized MusicXML string
    normalize_with_report(path_or_xml) -> (string, JSON-ready provenance report)
    write_normalized(source, destination) -> provenance report

This operates on regenerated notation, never on the saved MXL/OMR source. The
report documents the rendering adaptation; the interactive score is re-engraved
notation and must not be presented as a pixel-faithful copy of the scan.
"""
from copy import deepcopy
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
import xml.etree.ElementTree as ET


def _read_xml(source):
    if isinstance(source, str) and source.lstrip().startswith("<"):
        return source
    return Path(source).read_text(encoding="utf-8")


def _music_fingerprint(root):
    """Octave directions are compared separately by their exact musical times."""
    copy = deepcopy(root)
    for note in copy.findall(".//note"):
        for beam in note.findall("beam"):
            note.remove(beam)
    for measure in copy.findall("part/measure"):
        for direction in list(measure.findall("direction")):
            if direction.find("direction-type/octave-shift") is not None:
                measure.remove(direction)
    return ET.tostring(copy, encoding="unicode")


def _measure_positions(root):
    """Read MusicXML order, including backups, chords and effective divisions."""
    for part in root.findall('part'):
        divisions = Fraction(1)
        for measure in part.findall('measure'):
            cursor, previous = Fraction(0), Fraction(0)
            notes, directions = [], []
            for index, element in enumerate(measure):
                if element.tag == 'attributes':
                    divisions = Fraction(element.findtext('divisions', str(divisions)))
                elif element.tag in ('backup', 'forward'):
                    duration = Fraction(element.findtext('duration', '0')) / divisions
                    cursor += duration if element.tag == 'forward' else -duration
                elif element.tag == 'note':
                    cursor_before = cursor
                    onset = previous if element.find('chord') is not None else cursor
                    duration = Fraction(element.findtext('duration', '0')) / divisions
                    if element.find('chord') is None:
                        previous, cursor = onset, onset + duration
                    notes.append((element, index, element.findtext('staff', '1'), onset, onset + duration, cursor_before, cursor))
                elif element.tag == 'direction' and element.find('direction-type/octave-shift') is not None:
                    offset = Fraction(element.findtext('offset', '0')) / divisions
                    directions.append((element, index, element.findtext('staff', '1'), cursor + offset, offset))
            yield part.get('id'), measure, notes, directions


def _octave_semantics(root):
    result = []
    for part, measure, _, directions in _measure_positions(root):
        for direction, _, staff, time, _ in directions:
            body = deepcopy(direction)
            for offset in body.findall('offset'):
                body.remove(offset)
            result.append((part, measure.get('number'), staff, str(time), ET.tostring(body, encoding='unicode')))
    return sorted(result)


def _anchor_octave_offsets(root):
    """Verovio can attach leading positive offsets to the wrong voice/staff.

    Relocate only exactly representable endpoints in the engraving copy.
    No pitch shift, event-length repair, or approximate position is inferred.
    """
    changes, unresolved = [], []
    for part, measure, notes, directions in _measure_positions(root):
        for direction, index, staff, time, offset in directions:
            if not offset:
                continue
            shift = direction.find('direction-type/octave-shift')
            stop = shift.get('type') == 'stop'
            # Grace notes share their main note's metric time, but XML order
            # decides which side of an octave boundary they belong to. An
            # offset alone cannot establish that order, even at an exact beat.
            if any(item[2] == staff and item[3] == item[4] == time for item in notes):
                unresolved.append({'part':part, 'measure':measure.get('number'), 'staff':staff,
                                   'time':str(time), 'type':shift.get('type'), 'reason':'Zero-duration grace/event at octave endpoint; original direction retained'})
                continue
            candidates = [item for item in notes if item[2] == staff and item[4 if stop else 3] == time and item[6 if stop else 5] == time]
            if not candidates:
                unresolved.append({'part':part, 'measure':measure.get('number'), 'staff':staff,
                                   'time':str(time), 'type':shift.get('type'), 'reason':'No exact staff event endpoint; original direction retained'})
                continue
            anchor = max(candidates, key=lambda item:item[1]) if stop else min(candidates, key=lambda item:item[1])
            following = next((item for item in notes if item[1] > anchor[1]), None)
            if stop and following and following[0].find('chord') is not None:
                unresolved.append({'part':part, 'measure':measure.get('number'), 'staff':staff,
                                   'time':str(time), 'type':shift.get('type'), 'reason':'Endpoint would split an unequal-duration chord; original direction retained'})
                continue
            measure.remove(direction)
            for offset_node in direction.findall('offset'):
                direction.remove(offset_node)
            measure.insert(list(measure).index(anchor[0]) + int(stop), direction)
            changes.append({'part':part, 'measure':measure.get('number'), 'staff':staff,
                            'time':str(time), 'originalOffset':str(offset), 'type':shift.get('type'),
                            'anchorNoteId':anchor[0].get('id'), 'placement':'after' if stop else 'before',
                            'reason':'Keep exact octave scope while anchoring Verovio to the intended staff event'})
    return changes, unresolved


def normalize_with_report(source):
    original = _read_xml(source)
    root = ET.fromstring(original)
    before = _music_fingerprint(root)
    octave_before = _octave_semantics(root)
    octave_changes, unresolved_octaves = _anchor_octave_offsets(root)
    changes = []
    candidates = 0
    for part in root.findall("part"):
        for measure in part.findall("measure"):
            first_note = None
            for child in measure:
                if child.tag in ("backup", "forward"):
                    first_note = None
                if child.tag != "note":
                    continue
                if child.find("chord") is None:
                    first_note = child
                    continue
                if first_note is None:
                    continue
                primary = {(tuple(sorted(b.attrib.items())), b.text) for b in first_note.findall("beam")}
                for beam in list(child.findall("beam")):
                    candidates += 1
                    key = (tuple(sorted(beam.attrib.items())), beam.text)
                    if key not in primary:
                        continue
                    changes.append({"noteId": child.get("id"), "measure": measure.get("number"), "part": part.get("id"),
                                    "kind": "duplicate-chord-tone-beam", "beamNumber": beam.get("number", "1"),
                                    "beamType": beam.text, "retainedOn": first_note.get("id"),
                                    "reason": "Same chord beam already exists on its first note; omit repeated tone beam for Verovio compatibility."})
                    child.remove(beam)
    if _music_fingerprint(root) != before:
        raise AssertionError("Engraving normalization altered music or non-beam notation")
    if _octave_semantics(root) != octave_before:
        raise AssertionError("Engraving normalization altered exact octave direction times or content")
    normalized = ET.tostring(root, encoding="unicode", xml_declaration=True)
    report = {"policy": "Remove identical chord-tone beam duplicates; anchor exact octave offsets on their staff events",
              "displayStyle": "Re-engraved interactive notation; scan remains source of truth",
              "removedDuplicateBeams": len(changes), "unresolvedToneBeams": candidates - len(changes),
              "musicFingerprintUnchanged": True, "sourceSha256": sha256(original.encode()).hexdigest(),
              "octaveDirectionTimingUnchanged": True, "octaveDirectionRelocations": octave_changes,
              "unresolvedOctaveOffsets": unresolved_octaves,
              "normalizedSha256": sha256(normalized.encode()).hexdigest(), "changes": changes}
    return normalized, report


def normalize(source):
    return normalize_with_report(source)[0]


def write_normalized(source, destination):
    xml, report = normalize_with_report(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(xml, encoding="utf-8")
    return report
