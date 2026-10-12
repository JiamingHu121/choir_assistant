"""Canonical symbol/timeline model with conservative, evidence-backed score checks.

API: build_model(corrections=None, source_path=None, omr_path=None, tempo_context=None) returns a
JSON-ready dict except for its private ``_xml`` string. ``write_canonical`` writes
that string to a new MusicXML file; the saved MXL/OMR is never modified.

Notes are notation segments, not already-merged performed notes. Stable IDs are
source part, source measure sequence, and source note ordinal. performanceEvents
explicitly map valid contiguous ties to their notation segments. Quarter-note
beats and seconds are separate units throughout.

Corrections are {note_id: {pitch: midi_integer, duration: quarter_beats, reason}}.
Reserved __measures__ maps source measure numbers to {timeSignature, duration,
reason}; timeSignature carries forward, while duration applies only to that bar.
Missing meters are only propagated from explicit evidence, never guessed from a
mode or a harmonic convention. Notated/performed duration mismatches are review
candidates: MusicXML explicitly permits them.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from workbench.tempo import POLICY_VERSION as TEMPO_POLICY_VERSION, from_direction, evidence_marks, resolve as resolve_tempos
from workbench.music_semantics import (POLICY_VERSION as MODEL_POLICY_VERSION, notation,
    rational, fraction as exact_fraction, validate_notation, build_relations,
    performance_events, validate_coverage, rhythm_validation, validate_chord_membership)

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DIVISIONS = 10080
TYPE_BEATS = {"maxima": Fraction(32), "long": Fraction(16), "breve": Fraction(8),
              "whole": Fraction(4), "half": Fraction(2), "quarter": Fraction(1),
              "eighth": Fraction(1, 2), "16th": Fraction(1, 4),
              "32nd": Fraction(1, 8), "64th": Fraction(1, 16),
              "128th": Fraction(1, 32), "256th": Fraction(1, 64)}
DIATONIC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
REFERENCE_URLS = {
    "duration": "https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/duration/",
    "chord": "https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/chord/",
    "tie": "https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/tie/",
    "tied": "https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/tied/",
    "slur": "https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/slur/",
    "beam": "https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/beam/",
    "backup": "https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/backup/",
    "forward": "https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/forward/",
    "staff": "https://www.w3.org/2021/06/musicxml40/musicxml-reference/examples/staff-element/",
    "transpose": "https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/transpose/",
}


def _fraction(value, default=0):
    return Fraction(str(value if value is not None else default)).limit_denominator(10080)


def _xml_number(value):
    value = _fraction(value)
    return str(value.numerator) if value.denominator == 1 else f"{float(value):.10f}".rstrip("0").rstrip(".")


def _read_source(path: Path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            if "META-INF/container.xml" in archive.namelist():
                container = ET.fromstring(archive.read("META-INF/container.xml"))
                candidates = [e.get("full-path") for e in container.iter() if e.tag.endswith("rootfile")]
            else:
                candidates = []
            name = next((n for n in candidates if n and n in archive.namelist()), None)
            name = name or next(n for n in archive.namelist() if n.endswith(".xml") and not n.startswith("META-INF/"))
            raw = archive.read(name)
    else:
        raw = path.read_bytes()
    root = ET.fromstring(raw)
    # MusicXML uses no default namespace; accept namespace-bearing fixture files.
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    if root.tag != "score-partwise":
        raise ValueError("Only score-partwise MusicXML is supported; convert score-timewise before importing")
    return root, raw


def _measure_number(text, index):
    try:
        return int(text)
    except (ValueError, TypeError):
        return text or index + 1


def _meter(time):
    if time is None or time.find("senza-misura") is not None:
        return None
    beats = time.findall("beats")
    types = time.findall("beat-type")
    if not beats or len(beats) != len(types):
        return None
    values = [(sum(int(n) for n in b.text.split("+")), int(t.text)) for b, t in zip(beats, types)]
    return {"text": "+".join(f"{b.text}/{t.text}" for b, t in zip(beats, types)),
            "beats": float(sum(Fraction(b * 4, t) for b, t in values)), "source": "MusicXML"}


def _meter_text(text, reason):
    pairs = [part.split("/") for part in text.split("+")]
    length = sum(Fraction(int(a) * 4, int(b)) for a, b in pairs)
    return {"text": text, "beats": float(length), "source": "reviewed-correction", "reason": reason}


def _write_meter(measure, meter):
    attrs = measure.find("attributes")
    if attrs is None:
        attrs = ET.Element("attributes")
        measure.insert(0, attrs)
    for time in attrs.findall("time"):
        attrs.remove(time)
    time = ET.Element("time")
    for pair in meter["text"].split("+"):
        a, b = pair.split("/")
        ET.SubElement(time, "beats").text = a
        ET.SubElement(time, "beat-type").text = b
    # In attributes, time follows key and precedes staves/clef.
    before = next((i for i, e in enumerate(attrs) if e.tag in ("staves", "part-symbol", "instruments", "clef", "staff-details")), len(attrs))
    attrs.insert(before, time)


def _write_staff_meters(measure, values):
    """Fill a part's own staff meter history, never another part's change."""
    attrs = measure.find("attributes")
    if attrs is None:
        attrs = ET.Element("attributes")
        measure.insert(0, attrs)
    for time in attrs.findall("time"):
        attrs.remove(time)
    common = len({value["text"] for value in values.values() if value}) == 1
    selected = {None: next(value for value in values.values() if value)} if common else values
    before = next((index for index, child in enumerate(attrs) if child.tag in ("staves", "part-symbol", "instruments", "clef", "staff-details")), len(attrs))
    for offset, (staff, value) in enumerate(selected.items()):
        if value is None:
            continue
        time = ET.Element("time", {"number": str(staff)} if staff is not None else {})
        for pair in value["text"].split("+"):
            beats, denominator = pair.split("/")
            ET.SubElement(time, "beats").text = beats
            ET.SubElement(time, "beat-type").text = denominator
        attrs.insert(before + offset, time)


def _normalize_tie_order(note):
    before, after = [], []
    for parent, tag in [(note, "tie")] + [(container, "tied") for container in note.findall("notations")]:
        positions = [index for index, child in enumerate(parent) if child.tag == tag]
        nodes = [parent[index] for index in positions]
        ordered = sorted(nodes, key=lambda node: {"stop": 0, "continue": 1, "start": 2}.get(node.get("type"), 3))
        before.extend(node.get("type") for node in nodes)
        after.extend(node.get("type") for node in ordered)
        for index, node in zip(positions, ordered):
            parent[index] = node
    return before, after


def _notated_duration(note):
    length = TYPE_BEATS.get(note.findtext("type"))
    if length is None:
        return None
    dots = len(note.findall("dot"))
    length *= 2 - Fraction(1, 2 ** dots)
    mod = note.find("time-modification")
    if mod is not None:
        actual, normal = mod.findtext("actual-notes"), mod.findtext("normal-notes")
        if actual and normal:
            try:
                actual, normal = int(actual), int(normal)
            except ValueError:
                return None
            if actual <= 0 or normal <= 0:
                return None
            length *= Fraction(normal, actual)
    return length


def _staff_meter_history(parts, meters, overrides, numbers):
    """Retain staff-local inherited notation meters before XML normalization.

    Reviewed global meter corrections apply to every staff. Independent source
    meter changes thereafter remain independent; this function does not invent
    a performed-time conversion between their quarter-note units.
    """
    history = defaultdict(dict)
    for part in parts:
        part_id, running = part.get("id"), {}
        staff_count = max([int(part.findtext("measure/attributes/staves", "1"))]
                          + [int(note.findtext("staff", "1")) for note in part.findall("measure/note")])
        for index, measure in enumerate(part.findall("measure")):
            change = overrides.get(str(numbers[index]), overrides.get(numbers[index], {}))
            if change.get("timeSignature"):
                value = _meter_text(change["timeSignature"], change.get("reason", "用户曲谱校对"))
                running = {staff: deepcopy(value) for staff in range(1, staff_count + 1)}
            else:
                for time in measure.findall("attributes/time"):
                    value = _meter(time)
                    if value:
                        value["sourcePart"], value["sourceMeasure"] = part_id, numbers[index]
                        targets = [int(time.get("number"))] if time.get("number") else range(1, staff_count + 1)
                        for staff in targets:
                            running[staff] = deepcopy(value)
            for staff in range(1, staff_count + 1):
                value = deepcopy(running.get(staff, meters[index]))
                if value:
                    value["beatsRational"] = rational(_fraction(value["beats"]))
                    value.setdefault("inheritance", "staff-explicit" if staff in running else "shared-conductor-meter")
                history[index][f"{part_id}/staff{staff}"] = value
    return history


def _set_duration(note, duration, replace_notation=False):
    node = note.find("duration")
    if node is None:
        node = ET.Element("duration")
        after = max((i + 1 for i, e in enumerate(note) if e.tag in ("pitch", "rest", "unpitched")), default=0)
        note.insert(after, node)
    node.text = _xml_number(duration * CANONICAL_DIVISIONS)
    if not replace_notation or note.find("rest") is not None and note.find("rest").get("measure") == "yes":
        return
    for e in list(note):
        if e.tag in ("type", "dot", "time-modification"):
            note.remove(e)
    choice = next(((t, dots) for t, beats in TYPE_BEATS.items() for dots in range(3)
                   if beats * (2 - Fraction(1, 2 ** dots)) == duration), None)
    if choice:
        type_name, dots = choice
        mod = None
    else:
        type_name, dots = "quarter", 0
        ratio = Fraction(1) / duration
        mod = ET.Element("time-modification")
        ET.SubElement(mod, "actual-notes").text = str(ratio.numerator)
        ET.SubElement(mod, "normal-notes").text = str(ratio.denominator)
    before = next((i for i, e in enumerate(note) if e.tag in ("accidental", "time-modification", "stem", "notehead", "staff", "beam", "notations", "lyric")), len(note))
    note.insert(before, ET.Element("type"))
    note[before].text = type_name
    for index in range(dots):
        note.insert(before + 1 + index, ET.Element("dot"))
    if mod is not None:
        note.insert(before + 1 + dots, mod)


def _set_articulations(note, values):
    allowed = {"accent", "strong-accent", "staccato", "tenuto", "detached-legato", "staccatissimo",
               "spiccato", "scoop", "plop", "doit", "falloff", "breath-mark", "caesura", "stress", "unstress", "soft-accent"}
    if not isinstance(values, list) or any(not isinstance(value, str) or value not in allowed for value in values):
        raise ValueError("articulations correction must be a list of supported MusicXML articulation names")
    containers = note.findall("notations")
    for container in containers:
        for articulations in container.findall("articulations"):
            container.remove(articulations)
    if values:
        container = containers[0] if containers else ET.SubElement(note, "notations")
        articulations = ET.SubElement(container, "articulations")
        for value in values:
            ET.SubElement(articulations, value)
    for container in containers:
        if not len(container):
            note.remove(container)


def _clear_tuplets(note, values):
    if not isinstance(values, list):
        raise ValueError("tuplets correction must be a reviewed endpoint list or []")
    attributes = {"showNumber": ("show-number", {"actual", "both", "none"}),
                  "showType": ("show-type", {"actual", "both", "none"}),
                  "bracket": ("bracket", {"yes", "no"}),
                  "placement": ("placement", {"above", "below"})}
    allowed = {"type", "number", "actualNumber", "normalNumber", *attributes}
    endpoints = set()
    for value in values:
        if (not isinstance(value, dict) or not set(value).issubset(allowed)
                or value.get("type") not in {"start", "stop"}
                or not isinstance(value.get("number", "1"), str)
                or not value.get("number", "1").isdigit()
                or not 1 <= int(value.get("number", "1")) <= 16):
            raise ValueError("Reviewed tuplet endpoint requires start/stop and string number 1 through 16")
        key = (value["type"], value.get("number", "1"))
        if key in endpoints:
            raise ValueError("Duplicate reviewed tuplet endpoint")
        endpoints.add(key)
        for name, (_, choices) in attributes.items():
            if name in value and value[name] not in choices:
                raise ValueError(f"Invalid reviewed tuplet {name}")
        for name in ("actualNumber", "normalNumber"):
            if name in value and (isinstance(value[name], bool) or not isinstance(value[name], int) or not 1 <= value[name] <= 64):
                raise ValueError(f"Reviewed tuplet {name} must be an integer 1 through 64")
    containers = note.findall("notations")
    for container in containers:
        for mark in container.findall("tuplet"):
            container.remove(mark)
    if values:
        container = containers[0] if containers else ET.SubElement(note, "notations")
        for value in values:
            attrs = {"type": value["type"], "number": value.get("number", "1")}
            attrs.update({xml_name: value[name] for name, (xml_name, _) in attributes.items() if name in value})
            mark = ET.SubElement(container, "tuplet", attrs)
            for name, xml_name in (("actualNumber", "tuplet-actual"), ("normalNumber", "tuplet-normal")):
                if name in value:
                    ET.SubElement(ET.SubElement(mark, xml_name), "tuplet-number").text = str(value[name])
    for container in containers:
        if not len(container):
            note.remove(container)


def _set_reviewed_notation(note, value, duration):
    """Retain explicitly verified type/dots/ratio instead of respelling beats.

    The same .75 quarter units can be a dotted eighth or an undotted duplet
    eighth. A numeric-duration edit alone cannot preserve this distinction.
    """
    if not isinstance(value, dict) or set(value) != {"type", "dots", "timeModification"}:
        raise ValueError("Reviewed notation requires exactly type, dots, timeModification")
    type_name, dots, mod = value["type"], value["dots"], value["timeModification"]
    if type_name not in TYPE_BEATS or isinstance(dots, bool) or not isinstance(dots, int) or not 0 <= dots <= 3:
        raise ValueError("Reviewed notation needs a supported type and 0 through 3 dots")
    expected = TYPE_BEATS[type_name] * (2 - Fraction(1, 2 ** dots))
    if mod is not None:
        if not isinstance(mod, dict) or not {"actualNotes", "normalNotes"}.issubset(mod) or not set(mod).issubset({"actualNotes", "normalNotes", "normalType", "normalDots"}):
            raise ValueError("Reviewed timeModification requires actualNotes/normalNotes and optional normalType/normalDots")
        for name in ("actualNotes", "normalNotes"):
            if isinstance(mod[name], bool) or not isinstance(mod[name], int) or not 1 <= mod[name] <= 64:
                raise ValueError("Reviewed tuplet ratio counts must be integers 1 through 64")
        if "normalType" in mod and mod["normalType"] not in TYPE_BEATS:
            raise ValueError("Reviewed tuplet normalType is unsupported")
        normal_dots = mod.get("normalDots", 0)
        if isinstance(normal_dots, bool) or not isinstance(normal_dots, int) or not 0 <= normal_dots <= 3:
            raise ValueError("Reviewed tuplet normalDots must be 0 through 3")
        if normal_dots and not mod.get("normalType"):
            raise ValueError("Reviewed tuplet normalDots requires normalType")
        expected *= Fraction(mod["normalNotes"], mod["actualNotes"])
    if expected != duration:
        raise ValueError(f"Reviewed notation duration {expected} disagrees with corrected duration {duration}")
    for node in list(note):
        if node.tag in {"type", "dot", "time-modification"}:
            note.remove(node)
    before = next((index for index, node in enumerate(note) if node.tag in {"accidental", "stem", "notehead", "staff", "beam", "notations", "lyric"}), len(note))
    type_node = ET.Element("type")
    type_node.text = type_name
    note.insert(before, type_node)
    for index in range(dots):
        note.insert(before + 1 + index, ET.Element("dot"))
    if mod is not None:
        node = ET.Element("time-modification")
        ET.SubElement(node, "actual-notes").text = str(mod["actualNotes"])
        ET.SubElement(node, "normal-notes").text = str(mod["normalNotes"])
        if mod.get("normalType"):
            ET.SubElement(node, "normal-type").text = mod["normalType"]
        for _ in range(mod.get("normalDots", 0)):
            ET.SubElement(node, "normal-dot")
        before_mod = next((index for index, child in enumerate(note) if child.tag in {"stem", "notehead", "staff", "beam", "notations", "lyric"}), len(note))
        note.insert(before_mod, node)


def _normalize_reviewed_tuplet_ratio(note, change):
    """Make reviewed graphical ratios explicit to MusicXML importers.

    Verovio keeps duration ticks but computes spacing/timemaps from MEI's
    graphical tuplet ratio. An explicit tuplet-actual without tuplet-normal
    otherwise imports with an unintended default base of one. This repair is
    limited to an explicitly reviewed notation AND tuplet endpoint; untouched
    source tuplets and unimplemented nested tuplets are never guessed.
    """
    mod = change.get("notation", {}).get("timeModification")
    endpoints = change.get("tuplets")
    if not mod or endpoints is None:
        return []
    starts = [value for value in endpoints if value["type"] == "start"]
    if len(starts) > 1:
        raise ValueError("Reviewed nested tuplet ratios require explicit support")
    changes = []
    for value in starts:
        if value.get("actualNumber") is None:
            continue
        if value["actualNumber"] != mod["actualNotes"]:
            raise ValueError("Reviewed printed tuplet actualNumber disagrees with timeModification")
        if value.get("normalNumber") is not None and value["normalNumber"] != mod["normalNotes"]:
            raise ValueError("Reviewed printed tuplet normalNumber disagrees with timeModification")
        if value.get("normalNumber") is not None:
            continue
        mark = next((mark for mark in note.findall("notations/tuplet") if
                     mark.get("type") == "start" and mark.get("number", "1") == value.get("number", "1")), None)
        if mark is None:
            raise ValueError("Reviewed tuplet start was not exported")
        normal = mark.find("tuplet-normal")
        if normal is None:
            normal = ET.SubElement(mark, "tuplet-normal")
        number = normal.find("tuplet-number")
        if number is None:
            number = ET.SubElement(normal, "tuplet-number")
        number.text = str(mod["normalNotes"])
        changes.append({"number": value.get("number", "1"), "actualNumber": value["actualNumber"],
                        "normalNumber": mod["normalNotes"]})
    return changes


def _set_ties(note, values):
    if (not isinstance(values, list) or any(not isinstance(value, str) or value not in ("start", "stop") for value in values)
            or len(values) != len(set(values))):
        raise ValueError("tieTypes correction must contain unique start/stop types or be []")
    for child in note.findall("tie"):
        note.remove(child)
    containers = note.findall("notations")
    for container in containers:
        for child in container.findall("tied"):
            container.remove(child)
    before = next((index for index, child in enumerate(note) if child.tag in ("instrument", "footnote", "level", "voice", "type", "dot", "accidental", "time-modification", "stem", "notehead", "staff", "beam", "notations", "lyric")), len(note))
    ordered = sorted(values, key=lambda value: 0 if value == "stop" else 1)
    for offset, value in enumerate(ordered):
        note.insert(before + offset, ET.Element("tie", {"type": value}))
    if values:
        container = containers[0] if containers else ET.SubElement(note, "notations")
        for value in ordered:
            ET.SubElement(container, "tied", {"type": value})
    for container in containers:
        if not len(container):
            note.remove(container)


def _set_slurs(note, values):
    if not isinstance(values, list):
        raise ValueError("slurs correction must be a list of type/number objects or []")
    known = set()
    for value in values:
        if (not isinstance(value, dict) or set(value) != {"type", "number"}
                or value["type"] not in ("start", "stop", "continue")
                or not isinstance(value["number"], str) or not value["number"].isdigit()
                or not 1 <= int(value["number"]) <= 16):
            raise ValueError("slurs correction requires start/stop/continue and string number 1 through 16")
        key = (value["type"], value["number"])
        if key in known:
            raise ValueError("slurs correction cannot contain duplicate endpoint marks")
        known.add(key)
    containers = note.findall("notations")
    for container in containers:
        for child in container.findall("slur"):
            container.remove(child)
    if values:
        container = containers[0] if containers else ET.SubElement(note, "notations")
        for value in values:
            ET.SubElement(container, "slur", value)
    for container in containers:
        if not len(container):
            note.remove(container)


def _insert_reviewed_symbols(parts, numbers, entries, corrections):
    """Insert explicit PDF-reviewed rests/chord tones without source renumbering."""
    if not isinstance(entries, dict):
        raise ValueError("__insertions__ must map stable reviewed IDs to rest definitions")
    source_notes, ordinals = {}, {}
    part_map = {part.get("id"): part for part in parts}
    for part in parts:
        for index, measure in enumerate(part.findall("measure")):
            for ordinal, note in enumerate(measure.findall("note"), 1):
                stable_id = f"n-{part.get('id')}-m{index + 1}-o{ordinal}"
                source_notes[stable_id] = (part.get("id"), index + 1, measure, note)
                ordinals[note] = ordinal
    generated = {}
    allowed = {"sourcePart", "measureIndex", "afterNoteId", "kind", "duration", "staff", "voice", "reason", "evidence", "pitch", "pitchName", "status"}
    anchor_insert_counts = Counter()
    for stable_id, entry in entries.items():
        if not isinstance(entry, dict) or set(entry) - allowed or entry.get("kind") not in ("rest", "chord-tone"):
            raise ValueError("Reviewed insertions support only explicit rest or chord-tone definitions")
        chord_tone = entry["kind"] == "chord-tone"
        prefix = "n-review-" if chord_tone else "r-review-"
        if not isinstance(stable_id, str) or not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]+", stable_id):
            raise ValueError(f"Reviewed {entry['kind']} IDs must start with {prefix} and contain only letters, digits, - or _")
        if stable_id in source_notes or stable_id in generated:
            raise ValueError("Reviewed rest ID collides with an existing symbol")
        if not entry.get("reason") or not entry.get("evidence"):
            raise ValueError("Reviewed rest insertion requires reason and original-score evidence")
        index = entry.get("measureIndex")
        if type(index) is not int or not 1 <= index <= len(numbers) or entry.get("sourcePart") not in part_map:
            raise ValueError("Reviewed rest insertion has an unknown sourcePart or measureIndex")
        anchor = source_notes.get(entry.get("afterNoteId"))
        if anchor is None or anchor[:2] != (entry["sourcePart"], index):
            raise ValueError("Reviewed rest anchor must be an original symbol in the same sourcePart and measure")
        _, _, measure, after = anchor
        if any(value["afterNoteId"] == entry["afterNoteId"] and value["kind"] != entry["kind"] for value in generated.values()):
            raise ValueError("Reviewed rest and chord-tone insertions cannot share an anchor; anchor the rest after the complete original chord")
        next_note = next((node for node in list(measure)[list(measure).index(after) + 1:] if node.tag == "note"), None)
        if not chord_tone and next_note is not None and next_note.find("chord") is not None:
            raise ValueError("A reviewed rest cannot split a simultaneous chord")
        duration = _fraction(entry.get("duration", 0))
        if duration <= 0 or duration > 64:
            raise ValueError("Reviewed rest duration must be positive and at most 64 quarter-note units")
        staff = entry.get("staff", int(after.findtext("staff", "1")))
        voice = entry.get("voice", after.findtext("voice", "1"))
        if type(staff) is not int or staff <= 0 or not isinstance(voice, str) or not voice:
            raise ValueError("Reviewed rest staff/voice must be a positive staff number and nonempty voice string")
        if staff != int(after.findtext("staff", "1")) or voice != after.findtext("voice", "1"):
            raise ValueError("Reviewed rest must use its anchor's staff and voice; other-lane insertion requires an explicit cursor mapping")
        if chord_tone:
            if after.find("pitch") is None or after.find("grace") is not None:
                raise ValueError("A reviewed chord tone requires a pitched, nongrace anchor")
            # Source duration may use any inherited divisions. Compare against
            # its reviewed duration when present, otherwise against that exact
            # source cursor unit; a notated type is not independent authority.
            divisions = Fraction(1)
            for original_measure in part_map[entry["sourcePart"]].findall("measure")[:index]:
                value = original_measure.findtext("attributes/divisions")
                if value is not None:
                    divisions = _fraction(value)
            anchor_duration = corrections.get(entry["afterNoteId"], {}).get("duration")
            if anchor_duration is None:
                anchor_duration = (_fraction(after.findtext("duration")) / divisions
                                   if after.find("duration") is not None else _notated_duration(after))
            if anchor_duration is None or duration != _fraction(anchor_duration):
                raise ValueError("Reviewed chord-tone duration must equal its corrected anchor duration")
            pitch = entry.get("pitch")
            if type(pitch) is not int or not 0 <= pitch <= 127:
                raise ValueError("Reviewed chord-tone pitch must be an integer MIDI pitch from 0 to 127")
            notes_in_measure = measure.findall("note")
            base_index = notes_in_measure.index(after)
            while base_index > 0 and notes_in_measure[base_index].find("chord") is not None:
                base_index -= 1
            chord = [notes_in_measure[base_index]]
            for existing in notes_in_measure[base_index + 1:]:
                if existing.find("chord") is None:
                    break
                chord.append(existing)
            pitches = [corrections.get(f"n-{entry['sourcePart']}-m{index}-o{ordinals[existing]}", {}).get("pitch", _pitch(existing)[0])
                       if existing in ordinals else _pitch(existing)[0] for existing in chord]
            if pitch in pitches:
                raise ValueError("Reviewed chord tone duplicates an existing pitch in its chord")
        elif "pitch" in entry or "pitchName" in entry:
            raise ValueError("A reviewed rest cannot carry pitch fields")
        inserted = ET.Element("note", {"id": stable_id})
        if chord_tone:
            ET.SubElement(inserted, "chord")
            pitch_node = ET.SubElement(inserted, "pitch")
            ET.SubElement(pitch_node, "step").text = "C"
            ET.SubElement(pitch_node, "octave").text = "4"
            _set_pitch(inserted, entry["pitch"], entry.get("pitchName"))
        else:
            ET.SubElement(inserted, "rest")
        ET.SubElement(inserted, "voice").text = voice
        ET.SubElement(inserted, "staff").text = str(staff)
        _set_duration(inserted, duration, replace_notation=True)
        # Multiple reviewed rests at one anchor retain ledger order.
        tail = next((node for node in reversed(list(measure)) if node.get("id") in generated
                     and generated[node.get("id")]["afterNoteId"] == entry["afterNoteId"]), after)
        measure.insert(list(measure).index(tail) + 1, inserted)
        generated[stable_id] = deepcopy(entry)
        anchor_insert_counts[entry["afterNoteId"]] += 1
        generated[stable_id]["_symbolOrder"] = [ordinals[after], anchor_insert_counts[entry["afterNoteId"]]]
    return generated, ordinals


def _pitch(note):
    p = note.find("pitch")
    if p is None:
        return None, None
    step, octave = p.findtext("step"), int(p.findtext("octave"))
    alter = _fraction(p.findtext("alter"))
    midi = Fraction((octave + 1) * 12 + DIATONIC[step]) + alter
    accidental = "#" * int(alter) if alter >= 0 else "b" * int(-alter)
    return float(midi) if midi.denominator != 1 else int(midi), f"{step}{accidental}{octave}"


def _set_pitch(note, midi, pitch_name=None, prefer_flats=False):
    if isinstance(midi, bool) or int(midi) != midi or not 0 <= int(midi) <= 127:
        raise ValueError("Corrected pitch must be an integer MIDI number from 0 to 127")
    p = note.find("pitch")
    if p is None:
        raise ValueError("A rest/unpitched symbol cannot be changed into a pitched note with a pitch-only correction")
    old, _ = _pitch(note)
    spelling = re.fullmatch(r"([A-G])([#b]*)(-?\d+)", pitch_name) if pitch_name else None
    if pitch_name and spelling is None:
        raise ValueError("pitchName must use a spelling such as Eb4 or F#3")
    if spelling:
        step, accidental_text, octave_text = spelling.groups()
        alter = accidental_text.count("#") - accidental_text.count("b")
        if (int(octave_text) + 1) * 12 + DIATONIC[step] + alter != midi:
            raise ValueError("pitchName and pitch disagree")
        for child in list(p):
            p.remove(child)
        ET.SubElement(p, "step").text = step
        if alter:
            ET.SubElement(p, "alter").text = str(alter)
        ET.SubElement(p, "octave").text = octave_text
    elif (midi - old) % 12 == 0:
        p.find("octave").text = str(int(p.findtext("octave")) + int((midi - old) / 12))
    else:
        name = (FLAT_NAMES if prefer_flats else SHARP_NAMES)[int(midi) % 12]
        for child in list(p):
            p.remove(child)
        ET.SubElement(p, "step").text = name[0]
        if "#" in name or "b" in name:
            ET.SubElement(p, "alter").text = "1" if "#" in name else "-1"
        ET.SubElement(p, "octave").text = str(int(midi) // 12 - 1)
    # An exported accidental is visual only; keep it consistent after correction.
    accidental = note.find("accidental")
    if accidental is not None:
        alter = int(_fraction(p.findtext("alter")))
        accidental.text = {0: "natural", 1: "sharp", -1: "flat", 2: "double-sharp", -2: "flat-flat"}.get(alter, "natural")


def _logical_part(part_id, names):
    # Source IDs carry identity, never implied SATB roles.
    return names.get(part_id, part_id)


def _omr_index(path):
    """Map source measure/part/voice chord order to preserved OMR objects.

    This is an ordinal join checked against symbol counts and rest/head kinds.
    It deliberately does not use corrected onset or guessed pitch to force a join.
    Ambiguous joins return no exact symbol position.
    """
    result, measures, pages = {}, [], []
    if path is None or not path.exists():
        return result, measures, pages
    global_index = 0
    with zipfile.ZipFile(path) as archive:
        names = sorted((n for n in archive.namelist() if re.fullmatch(r"sheet#\d+/sheet#\d+\.xml", n)), key=lambda n: int(n.split("#")[1].split("/")[0]))
        for name in names:
            sheet = ET.fromstring(archive.read(name))
            page_num = int(name.split("#")[1].split("/")[0])
            picture = sheet.find("picture")
            width, height = int(picture.get("width")), int(picture.get("height"))
            pages.append({"page": page_num, "width": width, "height": height, "image": f"/assets/page-{page_num}.png"})
            for system in sheet.findall(".//system"):
                inters = {e.get("id"): e for e in system.findall("sig/inters/*")}
                contained = defaultdict(list)
                for rel in system.findall("sig/relations/relation"):
                    if rel.find("containment") is not None:
                        contained[rel.get("source")].append(rel.get("target"))
                points = system.findall("part/staff/lines/line/point")
                top = min(float(p.get("y")) for p in points) - 30
                bottom = max(float(p.get("y")) for p in points) + 45
                for stack_index, stack in enumerate(system.findall("stack")):
                    global_index += 1
                    left, right = float(stack.get("left")), float(stack.get("right"))
                    measures.append({"page": page_num, "system": int(system.get("id")), "x": left / width,
                                     "y": top / height, "w": (right - left) / width, "h": (bottom - top) / height,
                                     "omrDuration": float(_fraction(stack.get("duration")) * 4) if stack.get("duration") else None})
                    for part in system.findall("part"):
                        source_part = f"P{part.get('id')}"
                        part_measures = part.findall("measure")
                        if stack_index >= len(part_measures):
                            continue
                        measure = part_measures[stack_index]
                        staff_order = [staff.get("id") for staff in part.findall("staff")]
                        for voice in measure.findall("voice"):
                            chord_ids = []
                            if voice.get("measure-rest-chord"):
                                chord_ids.append(voice.get("measure-rest-chord"))
                            else:
                                chord_ids = [e.find("value").get("chord") for e in voice.findall("slots/entry")
                                             if e.find("value").get("status") == "BEGIN"]
                            records = []
                            for chord_id in chord_ids:
                                chord = inters.get(chord_id)
                                if chord is None:
                                    continue
                                ids = contained.get(chord_id, [])
                                heads = [inters[i] for i in ids if i in inters and inters[i].tag in ("head", "rest")]
                                heads.sort(key=lambda e: (float(e.get("pitch", "0")), float(e.find("bounds").get("y"))))
                                symbols = []
                                for head in heads:
                                    b = head.find("bounds")
                                    symbols.append({"headId": head.get("id"), "chordId": chord_id,
                                                    "recognitionGrade": float(head.get("ctx-grade", head.get("grade", "0"))),
                                                    "kind": "rest" if head.tag == "rest" else "note",
                                                    "shape": head.get("shape"), "staffPosition": head.get("pitch"),
                                                    "bbox": {"x": float(b.get("x")) / width, "y": float(b.get("y")) / height,
                                                             "w": float(b.get("w")) / width, "h": float(b.get("h")) / height}})
                                records.append({"symbols": symbols, "staff": staff_order.index(chord.get("staff")) + 1 if chord.get("staff") in staff_order else None,
                                                "page": page_num, "system": int(system.get("id")), "method": "checked-part-voice-chord-order"})
                            result[(global_index, source_part, voice.get("id"))] = records
    return result, measures, pages


def _link_omr(events, index):
    by_lane = defaultdict(list)
    for event in events:
        if event["source"].get("generated"):
            continue
        by_lane[(event["measureIndex"], event["sourcePart"], event["voice"])].append(event)
    for key, lane in by_lane.items():
        groups = []
        for event in lane:
            if groups and event["chordId"] == groups[-1][0]["chordId"]:
                groups[-1].append(event)
            else:
                groups.append([event])
        candidates = index.get(key, [])
        if len(candidates) != len(groups):
            continue
        for group, candidate in zip(groups, candidates):
            symbols = candidate["symbols"]
            if len(group) != len(symbols):
                continue
            ordered = sorted(group, key=lambda e: e["pitch"] if e["pitch"] is not None else -1, reverse=True)
            if any(e["kind"] != s["kind"] for e, s in zip(ordered, symbols)):
                continue
            for event, symbol in zip(ordered, symbols):
                event["source"]["omr"] = {k: v for k, v in candidate.items() if k != "symbols"} | symbol


def build_model(corrections=None, source_path=None, omr_path=None, tempo_context=None):
    corrections = deepcopy(corrections or {})
    if source_path is None:
        raise ValueError("An explicit MusicXML source_path is required")
    source_path = Path(source_path)
    if omr_path is None:
        omr_path = source_path.with_suffix(".omr")
    omr_path = Path(omr_path) if omr_path else None
    root, raw = _read_source(source_path)
    source_hash = sha256(raw).hexdigest()
    names = {p.get("id"): p.findtext("part-name", p.get("id")) for p in root.findall("part-list/score-part")}
    parts = root.findall("part")
    if not parts:
        raise ValueError("MusicXML contains no parts")
    first_measures = parts[0].findall("measure")
    numbers = [_measure_number(m.get("number"), i) for i, m in enumerate(first_measures)]
    missing_part_measures = []
    first_keys = [m.get("number") for m in first_measures]
    if len(set(first_keys)) != len(first_keys):
        raise ValueError("Repeated measure labels require explicit disambiguation before import")
    for part in parts:
        existing = {m.get("number"): m for m in part.findall("measure")}
        if set(existing) - set(first_keys):
            raise ValueError("A part contains measures absent from the conductor timeline")
        aligned = []
        for index, key in enumerate(first_keys):
            measure = existing.get(key)
            if measure is None:
                measure = ET.Element("measure", {"number": key})
                missing_part_measures.append((part.get("id"), numbers[index]))
            aligned.append(measure)
        for measure in list(part.findall("measure")):
            part.remove(measure)
        part.extend(aligned)
    issues, applied = [], []

    def issue(measure, severity, kind, message, evidence, proposal=None, auto=False):
        item = {"id": f"audit-{len(issues) + 1:04d}", "measure": measure, "severity": severity, "kind": kind,
                "message": message, "evidence": evidence, "proposal": proposal, "autoApplied": auto}
        issues.append(item)
        return item

    def correction(object_id, measure, field, before, after, reason, auto=False):
        category = "tuplet-ratio-normalization" if field == "tupletRatioNormalization" else "relation-order-normalization" if field == "tieEndpointOrder" else ("meter-propagation" if auto else "reviewed-meter") if field == "timeSignature" else ("measure-rest-normalization" if auto and "全小节" in reason else "symbol-duration-recovery" if auto else "reviewed-duration") if field == "duration" else f"reviewed-{field}"
        applied.append({"category": category, "id": f"correction-{len(applied) + 1:04d}", "objectId": object_id, "measure": measure,
                        "field": field, "before": before, "after": after, "reason": reason, "autoApplied": auto})

    for part_id, number in missing_part_measures:
        issue(number, "error", "missing-source-measure", "此声部的小节在MusicXML中缺失，按小节编号保留空位，避免后续整段错位。",
              {"sourcePart": part_id, "sourceMeasure": number}, "回原谱补录该小节；不从邻小节猜测音符。")
    explicit = defaultdict(list)
    for p in parts:
        for i, m in enumerate(p.findall("measure")):
            value = _meter(m.find("attributes/time"))
            if value:
                explicit[i].append(value)
    measure_overrides = corrections.get("__measures__", {})
    meters, current = [], None
    for i, number in enumerate(numbers):
        override = measure_overrides.get(str(number), measure_overrides.get(number, {}))
        if override.get("timeSignature"):
            current = _meter_text(override["timeSignature"], override.get("reason", "用户曲谱校对"))
            correction(f"measure-{i + 1}", number, "timeSignature", explicit[i][0]["text"] if explicit[i] else None,
                       current["text"], current["reason"])
        elif explicit[i]:
            texts = {v["text"] for v in explicit[i]}
            if len(texts) == 1:
                current = explicit[i][0]
            else:
                issue(number, "error", "conflicting-meters", "同一小节的声部拍号冲突。", {"meters": sorted(texts)}, "核对原谱拍号；保持第一声部拍号作为临时时间基准。")
                current = explicit[i][0]
        meters.append(deepcopy(current))
    missing = [n for n, meter in zip(numbers, meters) if meter is None]
    if missing:
        issue(missing[0], "error", "missing-meter", "导出的MusicXML在此区间没有有效拍号，不能据此认证小节拍数。",
              {"measures": missing, "source": str(source_path)}, "对照原谱确认拍号；相邻声部与重复节奏只能给候选，不能自动决定。")

    staff_meters = _staff_meter_history(parts, meters, measure_overrides, numbers)
    insertions, source_ordinals = _insert_reviewed_symbols(parts, numbers, corrections.get("__insertions__", {}), corrections)

    events, tempo_marks, lane_extents, per_part_extents = [], [], defaultdict(dict), defaultdict(dict)
    spacers = []
    tempo_nodes = []
    used_corrections = set()
    for p in parts:
        part_id = p.get("id")
        divisions = Fraction(1)
        key_fifths = 0
        staff_visibility = defaultdict(lambda: True)
        transpose_state = {}
        for i, m in enumerate(p.findall("measure")):
            number = numbers[i]
            for details in m.findall("attributes/staff-details"):
                staff_visibility[int(details.get("number", "1"))] = details.get("print-object", "yes") != "no"
            key = m.findtext("attributes/key/fifths")
            if key is not None:
                key_fifths = int(key)
            div = m.find("attributes/divisions")
            if div is not None:
                divisions = _fraction(div.text)
                if divisions <= 0:
                    raise ValueError(f"Non-positive divisions in {part_id}, measure {number}")
                div.text = str(CANONICAL_DIVISIONS)
            elif i == 0:
                attrs = m.find("attributes")
                if attrs is None:
                    attrs = ET.Element("attributes")
                    m.insert(0, attrs)
                attrs.insert(0, ET.Element("divisions"))
                attrs[0].text = str(CANONICAL_DIVISIONS)
            bar_change = measure_overrides.get(str(number), measure_overrides.get(number, {}))
            force_meter = bool(bar_change.get("timeSignature"))
            local_staff_meters = {int(key.rsplit("/staff", 1)[1]): value for key, value in staff_meters[i].items()
                                  if key.rsplit("/staff", 1)[0] == part_id}
            if any(local_staff_meters.values()) and (force_meter or m.find("attributes/time") is None and (i == 0 or explicit.get(i))):
                _write_staff_meters(m, local_staff_meters)
                if not force_meter:
                    correction(f"{part_id}-measure-{i + 1}", number, "timeSignature", None,
                               {str(staff): value["text"] for staff, value in local_staff_meters.items() if value},
                               "传播本源声部已知的谱表拍号；不以其他声部后续变化覆盖独立继承。", True)
            for transposition in m.findall("attributes/transpose"):
                value = {child.tag: child.text for child in transposition}
                value["attributes"] = dict(transposition.attrib)
                targets = [int(transposition.get("number"))] if transposition.get("number") else list(local_staff_meters)
                for staff in targets:
                    transpose_state[staff] = deepcopy(value)
            nonzero_transposes = {}
            for staff, value in transpose_state.items():
                try:
                    nonzero = any(_fraction(value.get(field, 0)) != 0 for field in ("diatonic", "chromatic", "octave-change")) or "double" in value
                except (ValueError, ZeroDivisionError):
                    nonzero = True
                if nonzero:
                    nonzero_transposes[str(staff)] = value
            transposition_issue = None
            if nonzero_transposes:
                transposition_issue = issue(number, "error", "unsupported-pitch-transposition",
                    "此源部件有尚未支持的乐器移调；MIDI音高仍来自MusicXML pitch，不能认证为正确发声音高。",
                    {"sourcePart": part_id, "staffTranspositions": nonzero_transposes, "measureIndices": [i + 1], "noteIds": []},
                    "先实现并校对书写音高到发声音高的变换；不在此猜移调或默默播放为已验证。")
            clef_change = corrections.get("__clefs__", {}).get(part_id, {}).get(str(number), {})
            if clef_change:
                attrs = m.find("attributes")
                if attrs is None:
                    attrs = ET.Element("attributes")
                    m.insert(0, attrs)
                old_clefs = [ET.tostring(c, encoding="unicode") for c in attrs.findall("clef")]
                for clef in attrs.findall("clef"):
                    attrs.remove(clef)
                clef = ET.Element("clef")
                ET.SubElement(clef, "sign").text = clef_change["sign"]
                ET.SubElement(clef, "line").text = str(clef_change["line"])
                if clef_change.get("octaveChange"):
                    ET.SubElement(clef, "clef-octave-change").text = str(clef_change["octaveChange"])
                attrs.append(clef)
                correction(f"{part_id}-measure-{i + 1}", number, "clef", old_clefs, ET.tostring(clef, encoding="unicode"), clef_change.get("reason", "原谱谱号校对"))
            clefs = m.findall("attributes/clef")
            duplicate = Counter(c.get("number", "1") for c in clefs)
            if any(count > 1 for count in duplicate.values()):
                issue(number, "warning", "conflicting-clefs", "同一谱表在同一attributes中导出多个谱号，音高可能受误识别谱号影响。",
                      {"sourcePart": part_id, "clefs": [ET.tostring(c, encoding="unicode") for c in clefs]}, "先看原谱真实谱号及八度标记，再核对音高；不凭和声习惯改音。")
            raw_cursor, cursor, previous_onset, previous_raw_onset = Fraction(0), Fraction(0), Fraction(0), Fraction(0)
            anchors = {Fraction(0): Fraction(0)}
            ordinal, chord_id, spacer_ordinal = 0, None, 0
            previous_staff, previous_voice = 1, "1"
            preceding_note = None
            for child in list(m):
                if child.tag == "direction":
                    offset_node = child.find("offset")
                    offset = _fraction(offset_node.text) / divisions if offset_node is not None else Fraction(0)
                    if offset_node is not None:
                        offset_node.text = _xml_number(offset * CANONICAL_DIVISIONS)
                    tempo = from_direction(child, meters[i])
                    if tempo:
                        sound_offset = child.find("sound/offset")
                        if sound_offset is not None:
                            offset = _fraction(sound_offset.text) / divisions
                            sound_offset.text = _xml_number(offset * CANONICAL_DIVISIONS)
                        mark = {"measureIndex": i + 1, "offset": float(cursor + offset), "sourcePart": part_id, **tempo}
                        tempo_marks.append(mark)
                        tempo_nodes.append((child, mark))
                elif child.tag == "sound":
                    # MusicXML also allows standalone measure-level sound tags.
                    wrapper = ET.Element("direction")
                    wrapper.append(deepcopy(child))
                    tempo = from_direction(wrapper, meters[i])
                    if tempo:
                        offset_node = child.find("offset")
                        offset = _fraction(offset_node.text) / divisions if offset_node is not None else Fraction(0)
                        if offset_node is not None:
                            offset_node.text = _xml_number(offset * CANONICAL_DIVISIONS)
                        mark = {"measureIndex": i + 1, "offset": float(cursor + offset), "sourcePart": part_id, **tempo}
                        tempo_marks.append(mark)
                        tempo_nodes.append((child, mark))
                elif child.tag in ("backup", "forward"):
                    d = child.find("duration")
                    duration = _fraction(d.text) / divisions
                    if child.tag == "backup":
                        target = raw_cursor - duration
                        if target < 0:
                            issue(number, "error", "negative-backup", "声部回退越过小节起点。", {"sourcePart": part_id, "target": float(target)}, "核对声部时值与backup。")
                        new_target = anchors.get(target, target)
                        new_duration = cursor - new_target
                        if new_duration <= 0:
                            new_duration = duration
                            issue(number, "warning", "ambiguous-backup", "时值修改后的局部声部回退无法唯一重定位。", {"sourcePart": part_id}, "需要人工核对声部起点。")
                        cursor, raw_cursor = new_target, target
                    else:
                        new_duration = duration
                        spacer_ordinal += 1
                        spacers.append({"id": f"forward-{part_id}-m{i + 1}-o{spacer_ordinal}", "kind": "forward",
                                        "measure": number, "measureIndex": i + 1, "sourcePart": part_id,
                                        "staff": int(child.findtext("staff", str(previous_staff))),
                                        "voice": child.findtext("voice", previous_voice),
                                        "offset": float(cursor), "duration": float(duration),
                                        "rational": {"offset": rational(cursor), "duration": rational(duration)},
                                        "scopeSource": "explicit" if child.find("voice") is not None else "current-cursor-lane"})
                        spacer = spacers[-1]
                        if duration <= 0:
                            issue(number, "error", "invalid-forward-duration", "显式forward必须有正时值。",
                                  {"sourcePart": part_id, "duration": float(duration), "spacerIds": [spacer["id"]], "measureIndices": [i + 1]},
                                  "核对原MusicXML位移；不认证或猜补无效静默时段。")
                        cursor += duration
                        raw_cursor += duration
                        lane = (part_id, spacer["staff"], spacer["voice"])
                        lane_extents[i][lane] = max(lane_extents[i].get(lane, Fraction(0)), cursor)
                        per_part_extents[i][part_id] = max(per_part_extents[i].get(part_id, Fraction(0)), cursor)
                    d.text = _xml_number(new_duration * CANONICAL_DIVISIONS)
                elif child.tag == "note":
                    generated_entry = insertions.get(child.get("id"))
                    ordinal = source_ordinals.get(child)
                    stable_id = child.get("id") if generated_entry else f"n-{part_id}-m{i + 1}-o{ordinal}"
                    child.set("id", stable_id)
                    is_chord, is_grace = child.find("chord") is not None, child.find("grace") is not None
                    original_duration_node = None if generated_entry else child.find("duration")
                    original_duration = (_fraction(original_duration_node.text) / divisions) if original_duration_node is not None else Fraction(0)
                    notated = _notated_duration(child)
                    original_pitch, original_name = _pitch(child)
                    original_type, original_dots = child.findtext("type"), len(child.findall("dot"))
                    original_notation = notation(child)
                    original_ties = [tie.get("type") for tie in child.findall("tie")]
                    duration = original_duration
                    change = corrections.get(stable_id, {})
                    if generated_entry:
                        if change:
                            raise ValueError("A generated rest must be edited in its __insertions__ entry")
                        change = {key: generated_entry[key] for key in ("duration", "reason", "evidence")}
                    if change:
                        if not isinstance(change, dict):
                            raise ValueError(f"Correction {stable_id} must be a dictionary")
                        if not set(change).issubset({"pitch", "pitchName", "duration", "articulations", "tuplets", "tieTypes", "slurs", "notation", "calibration", "reason", "evidence", "status"}):
                            raise ValueError(f"Unknown correction fields for {stable_id}")
                        used_corrections.add(stable_id)
                        reason = change.get("reason", "用户音符校对")
                        if "pitch" in change:
                            _set_pitch(child, change["pitch"], change.get("pitchName"), prefer_flats=key_fifths < 0)
                            correction(stable_id, number, "pitch", original_pitch, change["pitch"], reason)
                        if "duration" in change:
                            duration = _fraction(change["duration"])
                            if duration <= 0 or duration > 64:
                                raise ValueError(f"Invalid corrected duration for {stable_id}")
                            correction(stable_id, number, "duration", None if generated_entry else float(original_duration), float(duration), reason)
                        if "articulations" in change:
                            _set_articulations(child, change["articulations"])
                            correction(stable_id, number, "articulations", original_notation["articulations"], change["articulations"], reason)
                        if "tuplets" in change:
                            _clear_tuplets(child, change["tuplets"])
                            correction(stable_id, number, "tuplets", original_notation["tuplets"], change["tuplets"], reason)
                        if "tieTypes" in change:
                            _set_ties(child, change["tieTypes"])
                            correction(stable_id, number, "tieTypes", original_ties, change["tieTypes"], reason)
                        if "slurs" in change:
                            _set_slurs(child, change["slurs"])
                            correction(stable_id, number, "slurs", original_notation["slurs"], change["slurs"], reason)
                    elif original_duration_node is None and not is_grace and notated:
                        duration = notated
                        correction(stable_id, number, "duration", None, float(duration), "源duration缺失，按已明确的type、附点与连音比例重建。", True)
                        issue(number, "info", "missing-duration-recovered", "缺失的duration已按完整记谱时值重建。", {"noteIds": [stable_id], "notatedBeats": float(notated)}, None, True)
                    is_rest = child.find("rest") is not None
                    measure_rest = is_rest and child.find("rest").get("measure") == "yes"
                    # Only a source-confirmed meter plus non-implicit full bar supports this repair.
                    if measure_rest and meters[i] and m.get("implicit") != "yes" and "duration" not in change:
                        staff = int(child.findtext("staff", "1"))
                        local_meter = staff_meters[i].get(f"{part_id}/staff{staff}")
                        expected = _fraction((local_meter or meters[i])["beats"])
                        if duration != expected:
                            correction(stable_id, number, "duration", float(duration), float(expected), "全小节休止符按明确拍号规范化；并保留原OMR时值。", True)
                            duration = expected
                    if not is_grace:
                        _set_duration(child, duration, replace_notation="duration" in change)
                        if "notation" in change:
                            _set_reviewed_notation(child, change["notation"], duration)
                            correction(stable_id, number, "notation", original_notation,
                                       change["notation"], reason)
                            normalized_tuplets = _normalize_reviewed_tuplet_ratio(child, change)
                            if normalized_tuplets:
                                correction(stable_id, number, "tupletRatioNormalization", None, normalized_tuplets,
                                           "以明确校对的timeModification补出图形连音正常音符数，保证刻谱与演奏时值一致。", True)
                    tie_order_before, tie_order_after = _normalize_tie_order(child)
                    if tie_order_before != tie_order_after:
                        correction(stable_id, number, "tieEndpointOrder", tie_order_before, tie_order_after,
                                   "规范导出在同音符先关闭旧延音再开始新延音，避免导入器产生自指；不改端点、音高或时值。", True)
                    onset = previous_onset if is_chord else cursor
                    raw_onset = previous_raw_onset if is_chord else raw_cursor
                    if not is_chord:
                        chord_id = stable_id
                        previous_onset, previous_raw_onset = onset, raw_onset
                    pitch, pitch_name = _pitch(child)
                    ties = [tie.get("type") for tie in child.findall("tie")]
                    slurs = [{"type": slur.get("type"), "number": slur.get("number", "1")} for slur in child.findall("notations/slur")]
                    event = {"id": stable_id, "kind": "rest" if is_rest else "note", "measure": number,
                             "measureIndex": i + 1, "part": _logical_part(part_id, names), "sourcePart": part_id,
                             "staff": int(child.findtext("staff", "1")), "voice": child.findtext("voice", "1"),
                             "pitch": pitch, "pitchName": pitch_name, "offset": float(onset), "duration": float(duration),
                             "tie": "continue" if set(ties) == {"start", "stop"} else ties[0] if ties else None,
                             "tieTypes": ties, "slurs": slurs, "chordId": chord_id, "chordTone": is_chord,
                             "grace": is_grace, "measureRest": measure_rest, "notatedDuration": float(_notated_duration(child)) if _notated_duration(child) is not None else None,
                             "notation": notation(child), "rational": {"offset": rational(onset), "duration": rational(duration)},
                             "placeholder": False,
                             "pitchSupport": {"status": "unsupported-transposition" if str(int(child.findtext("staff", "1"))) in nonzero_transposes else "musicxml-pitch",
                                              "transposition": deepcopy(transpose_state.get(int(child.findtext("staff", "1"))))},
                             "source": {"part": part_id, "measureNumber": m.get("number"), "measureIndex": i + 1,
                                        "noteOrdinal": ordinal, "order": generated_entry["_symbolOrder"] if generated_entry else [ordinal, 0],
                                        "duration": float(original_duration), "offset": float(raw_onset),
                                        "pitch": None if generated_entry else original_pitch,
                                        "pitchName": None if generated_entry else original_name,
                                        "type": None if generated_entry else original_type, "dots": None if generated_entry else original_dots,
                                        "notation": None if generated_entry else original_notation,
                                        "tieTypes": original_ties, "slurs": original_notation["slurs"],
                                        "generated": bool(generated_entry), "reviewed": bool(generated_entry),
                                        "insertion": ({key: value for key, value in generated_entry.items() if not key.startswith("_")} if generated_entry else None),
                                        "calibration": deepcopy(change.get("calibration")),
                                        "staffVisible": staff_visibility[int(child.findtext("staff", "1"))], "omr": None}, "auditIds": []}
                    events.append(event)
                    if generated_entry:
                        correction(stable_id, number, f"{generated_entry['kind']}-insertion", None,
                                   {"duration": float(duration), "pitch": pitch, "afterNoteId": generated_entry["afterNoteId"]}, generated_entry["reason"])
                    previous_staff, previous_voice = event["staff"], event["voice"]
                    validate_notation(event, issue)
                    validate_chord_membership(event, preceding_note, issue)
                    preceding_note = event
                    if transposition_issue is not None and pitch is not None and event["pitchSupport"]["status"] == "unsupported-transposition":
                        transposition_issue["evidence"]["noteIds"].append(stable_id)
                    if not is_grace:
                        extent = onset + duration
                        lane = (part_id, event["staff"], event["voice"])
                        lane_extents[i][lane] = max(lane_extents[i].get(lane, Fraction(0)), extent)
                        per_part_extents[i][part_id] = max(per_part_extents[i].get(part_id, Fraction(0)), extent)
                    if not is_chord and not is_grace:
                        anchors[raw_cursor] = cursor
                        cursor += duration
                        raw_cursor += original_duration
                        anchors[raw_cursor] = cursor
                    if notated is not None and original_duration_node is not None and original_duration != notated and not measure_rest and not is_grace and "duration" not in change:
                        issue(number, "warning", "notation-duration-mismatch", "播放时值与type、附点、连音比例不一致；这可能是识谱误差，也可能是有意记谱。",
                              {"noteIds": [stable_id], "duration": float(original_duration), "notatedDuration": float(notated), "sourcePart": part_id}, "核对原谱和邻近节奏；不自动用type覆盖duration。")
                    if pitch is not None and not 0 <= pitch <= 127:
                        issue(number, "error", "invalid-midi-pitch", "音高超出MIDI支持范围。", {"noteIds": [stable_id], "pitch": pitch}, "核对谱号与八度。")
            for repeat in m.findall("barline/repeat"):
                issue(number, "warning", "repeat-not-unfolded", "存在反复记号；当前时间轴按谱面顺序，尚未展开演奏路线。", {"sourcePart": part_id, "direction": repeat.get("direction")}, "按反复/房子/跳转路线建立演奏序列后再认证整曲时长。")
    unknown = set(corrections) - used_corrections - {"__measures__", "__clefs__", "__meta__", "__insertions__"}
    if unknown:
        raise ValueError(f"Correction refers to unknown note IDs: {', '.join(sorted(unknown))}")
    omr, geometry, pages = _omr_index(omr_path)
    _link_omr(events, omr)
    measures, global_onset = [], Fraction(0)
    for i, number in enumerate(numbers):
        observed = max(per_part_extents[i].values(), default=Fraction(0))
        expected = _fraction(meters[i]["beats"]) if meters[i] else None
        override = measure_overrides.get(str(number), measure_overrides.get(number, {}))
        duration = observed
        if expected is not None and first_measures[i].get("implicit") != "yes":
            duration = max(duration, expected)
        if "duration" in override:
            confirmed = _fraction(override["duration"])
            if confirmed < observed:
                issue(number, "error", "requested-bar-shorter-than-notes", "确认的小节长度短于当前音符跨度，未截断音符。", {"requested": float(confirmed), "observed": float(observed)}, "逐声部修正多余拍数后才能缩短小节。")
            duration = max(confirmed, observed)
        if expected is not None and observed > expected and first_measures[i].get("implicit") != "yes":
            over = {f"{p}/staff{s}/voice{v}": float(d) for (p, s, v), d in lane_extents[i].items() if d > expected}
            note_ids = [event["id"] for event in events if event["measureIndex"] == i + 1 and not event["grace"]
                        and exact_fraction(event["rational"]["offset"]) + exact_fraction(event["rational"]["duration"]) > expected]
            issue(number, "error", "bar-overflow", "当前声部时值超出明确拍号；仅在草稿时间轴保留完整跨度。", {"expected": float(expected), "observed": float(observed), "lanes": over, "noteIds": note_ids, "measureIndices": [i + 1]}, "逐声部检查漏梁、附点、休止、声部起点；不将音符强行缩放。")
        value = {"number": number, "id": f"measure-{i + 1}", "index": i + 1, "onset": float(global_onset), "duration": float(duration),
                 "observedDuration": float(observed), "timeSignature": meters[i], "implicit": first_measures[i].get("implicit") == "yes",
                 "staffMeters": staff_meters[i],
                 "rational": {"onset": rational(global_onset), "duration": rational(duration), "end": rational(global_onset + duration)},
                 "sourceLaneDurations": {f"{p}/staff{s}/voice{v}": float(d) for (p, s, v), d in lane_extents[i].items()}}
        if i < len(geometry):
            value.update(geometry[i])
        measures.append(value)
        local_lengths = {exact_fraction(meter["beatsRational"]) for meter in staff_meters[i].values() if meter}
        value["meterMapping"] = {"status": "needs-review" if len(local_lengths) > 1 else "shared-notation-quarter",
                                 "sourceUnits": "quarter-note notation beats", "performedScaleByStaff": None}
        if len(local_lengths) > 1:
            issue(number, "error", "unresolved-meter-mapping", "谱表使用不同小节时值，尚未确认公共演奏时间映射。",
                  {"staffMeters": staff_meters[i], "measureIndices": [i + 1]},
                  "对照原谱等拍关系确认映射；保留各谱表记谱单位，不统一缩放来凑拍。")
        global_onset += duration
    validate_coverage(events, measures, spacers, issue)
    reviewed_tempos = evidence_marks(tempo_context, source_hash, measures)
    tempos, tempo_metadata = resolve_tempos(tempo_marks + reviewed_tempos, measures, issue)
    tempo_by_onset = {_fraction(mark["onset"]): mark["bpm"] for mark in tempos}

    # Resolve playback values in the generated MusicXML with the same priority
    # policy as the model, including conflicting directions at one position.
    # Original candidates remain in metadata and the saved source is untouched.
    for node, mark in tempo_nodes:
        if mark.get("bpm") is None:
            continue
        onset = max(0, measures[mark["measureIndex"] - 1]["onset"] + mark["offset"])
        selected = next(item for item in reversed(tempos) if item["onset"] <= onset)
        sound = node if node.tag == "sound" else node.find("sound")
        if sound is None:
            sound = ET.SubElement(node, "sound")
        sound.set("tempo", _xml_number(selected["bpm"]))

    # Recover reviewed PDF text in the generated score, while retaining the
    # independent evidence record and clearly inferred numeric playback field.
    for mark in reviewed_tempos:
        m = parts[0].findall("measure")[mark["measureIndex"] - 1]
        direction = ET.Element("direction", {"placement": "above"})
        kind = ET.SubElement(direction, "direction-type")
        ET.SubElement(kind, "words").text = mark["sourceText"]
        if mark["offset"]:
            ET.SubElement(direction, "offset").text = _xml_number(_fraction(mark["offset"]) * CANONICAL_DIVISIONS)
        onset = measures[mark["measureIndex"] - 1]["onset"] + mark["offset"]
        selected = next(item for item in reversed(tempos) if item["onset"] <= onset)
        ET.SubElement(direction, "sound", {"tempo": _xml_number(selected["bpm"])})
        before = next((i for i, child in enumerate(m) if child.tag not in ("print", "attributes")), len(m))
        m.insert(before, direction)

    def seconds(target):
        target = _fraction(target)
        last, bpm, total = Fraction(0), tempo_metadata["initialQuarterBpm"], 0.0
        for onset, value in sorted(tempo_by_onset.items()):
            if onset > target:
                break
            total += float(onset - last) * 60 / bpm
            last, bpm = onset, value
        return total + float(target - last) * 60 / bpm

    for measure in measures:
        measure["start"], measure["end"] = seconds(measure["onset"]), seconds(measure["onset"] + measure["duration"])
    for event in events:
        onset = exact_fraction(measures[event["measureIndex"] - 1]["rational"]["onset"]) + exact_fraction(event["rational"]["offset"])
        end = onset + exact_fraction(event["rational"]["duration"])
        event["onset"] = float(onset)
        event["rational"].update(onset=rational(onset), end=rational(end))
        event["start"], event["end"] = seconds(onset), seconds(end)
    notes = [e for e in events if e["kind"] == "note"]
    rests = [e for e in events if e["kind"] == "rest"]
    relations = build_relations(notes, issue)
    performance = performance_events(notes, relations, seconds)
    seen_metric_marks = set()
    for mark in tempo_metadata["changes"]:
        if mark.get("unresolved") and "=" in mark.get("sourceText", ""):
            index = mark["measureIndex"]
            key = (index, mark["sourceText"])
            if key in seen_metric_marks:
                continue
            seen_metric_marks.add(key)
            issue(measures[index - 1]["number"], "error", "unresolved-metric-modulation",
                  "此等拍标记尚未解析，不能认证后续谱表之间的节拍映射。",
                  {"sourceText": mark["sourceText"], "sourcePart": mark.get("sourcePart"), "measureIndices": list(range(index, len(measures) + 1))},
                  "原图确认旧/新拍子或跨谱表对应及作用范围，再建立显式映射。")
    validation = rhythm_validation(measures, issues)
    event_by_id = {e["id"]: e for e in events}
    for item in issues:
        for note_id in item["evidence"].get("noteIds", []):
            if note_id in event_by_id:
                event_by_id[note_id]["auditIds"].append(item["id"])
    version = sha256((source_hash + json.dumps({"corrections": corrections, "tempoContext": tempo_context,
                     "tempoPolicy": TEMPO_POLICY_VERSION, "modelPolicy": MODEL_POLICY_VERSION}, ensure_ascii=False, sort_keys=True)).encode()).hexdigest()[:16]
    return {"schemaVersion": 2, "modelPolicy": MODEL_POLICY_VERSION, "version": version, "title": root.findtext("work/work-title") or root.findtext("movement-title") or source_path.stem,
            "notes": notes, "rests": rests, "measures": measures, "pages": pages, "duration": seconds(global_onset),
            "relations": relations, "rhythmValidation": validation, "timelineSpacers": spacers,
            "totalBeats": float(global_onset), "tempos": tempos, "tempoMetadata": tempo_metadata, "performanceEvents": performance,
            "parts": [{"sourcePart": p.get("id"), "name": names.get(p.get("id"), p.get("id")), "part": _logical_part(p.get("id"), names)} for p in parts],
            "pilot": {"measures": [1, 2, 3, 4], "status": "review-needed", "noteIds": [n["id"] for n in notes if n["measureIndex"] <= 4]},
            "provenance": {"source": str(source_path), "sourceSha256": source_hash, "omr": str(omr_path) if omr_path and omr_path.exists() else None,
                           "units": {"onset": "quarter-note beats", "duration": "quarter-note beats", "start": "seconds", "end": "seconds"},
                           "rationalUnits": {"offset": "local notation-quarter position", "duration": "notation-quarter length", "onset": "draft/common notation-quarter position", "end": "draft/common notation-quarter position"},
                           "tempoFallback": tempo_metadata["initialQuarterBpm"] if tempo_metadata["sourceKind"] == "fallback" else None,
                           "tempoEvidence": tempo_context, "playbackRoute": "written-order", "references": {**REFERENCE_URLS, **tempo_metadata["references"]}},
            "audit": {"issues": issues, "corrections": applied, "summary": {"notes": len(notes), "rests": len(rests), "measures": len(measures),
                        "issues": len(issues), "bySeverity": dict(Counter(i["severity"] for i in issues)), "byKind": dict(Counter(i["kind"] for i in issues)),
                        "correctionCategories": dict(Counter(c["category"] for c in applied)), "autoCorrections": sum(bool(c["autoApplied"]) for c in applied), "reviewedCorrections": sum(not c["autoApplied"] for c in applied),
                        "exactSourcePositions": sum(e["source"]["omr"] is not None for e in events), "unverifiedPitchPolicy": "No harmony-based pitch replacement"}},
            "_xml": ET.tostring(root, encoding="unicode", xml_declaration=True)}


def public_model(model):
    return {key: value for key, value in model.items() if not key.startswith("_")}


def write_report(model, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(public_model(model), ensure_ascii=False, indent=2), encoding="utf-8")
    return destination


def _tempo_directions_at_offsets(measure):
    """Copy global tempo directives with offsets relative to the bar start.

    Canonical durations and offsets use CANONICAL_DIVISIONS. A direction can
    follow notes/backup/forward in a discarded placeholder part, so copying
    its original relative offset to the start of another part would move it.
    Sound offsets retain their precedence over display direction offsets.
    """
    cursor = Fraction(0)
    entries = []
    for child in measure:
        if child.tag == "note":
            if child.find("chord") is None and child.find("grace") is None:
                cursor += _fraction(child.findtext("duration")) / CANONICAL_DIVISIONS
        elif child.tag in ("backup", "forward"):
            duration = _fraction(child.findtext("duration")) / CANONICAL_DIVISIONS
            cursor += duration * (-1 if child.tag == "backup" else 1)
        elif child.tag in ("direction", "sound"):
            wrapper = child
            if child.tag == "sound":
                wrapper = ET.Element("direction")
                wrapper.append(deepcopy(child))
            tempo = from_direction(wrapper)
            if not tempo:
                continue
            copied = deepcopy(child)
            offset = copied.find("offset")
            display_position = cursor + (_fraction(offset.text) / CANONICAL_DIVISIONS if offset is not None else 0)
            if offset is None:
                offset = ET.Element("offset")
                before = next((i for i, item in enumerate(copied) if item.tag != "direction-type"), len(copied)) if copied.tag == "direction" else len(copied)
                copied.insert(before, offset)
            offset.text = _xml_number(display_position * CANONICAL_DIVISIONS)
            sound_offset = copied.find("sound/offset")
            sound_position = display_position
            if sound_offset is not None:
                sound_position = cursor + _fraction(sound_offset.text) / CANONICAL_DIVISIONS
                sound_offset.text = _xml_number(sound_position * CANONICAL_DIVISIONS)
            # Ignore source layout attributes when identifying equivalent marks,
            # while retaining text, written beat units, and the resolved sound.
            words = " ".join((word.text or "").strip() for word in copied.findall("direction-type/words")).casefold()
            metronome = copied.find("direction-type/metronome")
            metronome_content = tuple((node.tag, node.text) for node in metronome) if metronome is not None else ()
            sound = copied if copied.tag == "sound" else copied.find("sound")
            key = (sound_position, words, metronome_content, sound.get("tempo") if sound is not None else None)
            entries.append((key, copied))
    return entries


def write_canonical(model, destination, merge_parts=False):
    """Write every source part with stable note IDs; no score-specific merging."""
    if merge_parts:
        raise ValueError("Logical-part merging is unsupported in the distributable plugin; preserve source parts")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(model["_xml"], encoding="utf-8")
    return destination
