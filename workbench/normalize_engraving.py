"""Normalize a derived MusicXML copy for Verovio without changing music events.

Audiveris repeats a chord's beam indication on every chord tone. Verovio 6.3
imports that repetition as nested beam/chord containers and can then drop entire
runs of notes. Here only *identical* beam indications on additional chord tones
are removed; the chord's first note retains the original beam. Chord membership,
voice, staff, pitches, durations, ties, slurs, and original stable IDs are kept.

API:
    normalize(path_or_xml) -> normalized MusicXML string
    normalize_with_report(path_or_xml) -> (string, JSON-ready provenance report)
    write_normalized(source, destination) -> provenance report

This operates on regenerated notation, never on the saved MXL/OMR source. The
report documents the rendering adaptation; the interactive score is re-engraved
notation and must not be presented as a pixel-faithful copy of the scan.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import xml.etree.ElementTree as ET


def _read_xml(source):
    if isinstance(source, str) and source.lstrip().startswith("<"):
        return source
    return Path(source).read_text(encoding="utf-8")


def _music_fingerprint(root):
    """Everything except beam duplication must remain byte-equivalent as XML."""
    copy = deepcopy(root)
    for note in copy.findall(".//note"):
        for beam in note.findall("beam"):
            note.remove(beam)
    return ET.tostring(copy, encoding="unicode")


def normalize_with_report(source):
    original = _read_xml(source)
    root = ET.fromstring(original)
    before = _music_fingerprint(root)
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
    normalized = ET.tostring(root, encoding="unicode", xml_declaration=True)
    report = {"policy": "Remove only identical beam duplicates on extra chord tones",
              "displayStyle": "Re-engraved interactive notation; scan remains source of truth",
              "removedDuplicateBeams": len(changes), "unresolvedToneBeams": candidates - len(changes),
              "musicFingerprintUnchanged": True, "sourceSha256": sha256(original.encode()).hexdigest(),
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
