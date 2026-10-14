"""Read-only, independent checks of a built score and source-frozen facts.

``verify_bundle(path, facts=None)`` uses only the standard library. It does not
call the importer, infer notes from harmony, or certify a human reader's facts.
Source XML is addressed by part / measure sequence / original note ordinal;
canonical XML is addressed by its current IDs. Original XML IDs are provenance,
not stable identities. Durations are notation-quarter units, not seconds.

Facts schema (version 1)::

    {"schemaVersion": 1, "revision": "...",
     "hashes": {"sourceMusicxml": "...", "canonicalMusicxml": "...",
                "model": "...", "sourcePdf": "... optional ..."},
     "scope": "Original-image reading of selected staff bars",
     "staffBars": [{"sourcePart": "P1", "measureIndex": 1, "staff": 1,
                    "events": [{"kind": "note", "pitchName": "C4",
                                "offset": "0", "duration": "1",
                                "voice": "1", "tieTypes": []}]}]}

Voice and tieTypes are optional; everything else shown for an event is required.
Each selected staff bar is a complete multiset, so extra events also fail. Facts
may be frozen before building, then bound to the reviewed artifact's hashes
without changing their musical contents. A match covers only the listed bars.
"""
from __future__ import annotations

from datetime import datetime, timezone
from fractions import Fraction
from hashlib import sha256
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile


_STEPS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_HASH_KEYS = ("sourceMusicxml", "canonicalMusicxml", "model")


def _digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _manifest_artifacts(bundle, manifest, required):
    """Check every declared file, including SVG/audio, without interpreting it."""
    errors, indexed, checked = [], set(), 0
    if manifest.get("status") not in ("complete", "complete-with-open-issues"):
        errors.append({"kind": "manifest-incomplete", "status": manifest.get("status")})
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("manifest.artifacts must be a list")
    for item in artifacts:
        if not isinstance(item, dict):
            raise ValueError("Each manifest artifact must be an object")
        name = item.get("path")
        if not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts:
            errors.append({"kind": "unsafe-manifest-artifact-path", "artifact": name})
            continue
        try:
            path = _inside(bundle, name)
        except ValueError:
            errors.append({"kind": "unsafe-manifest-artifact-path", "artifact": name})
            continue
        if path in indexed:
            errors.append({"kind": "duplicate-manifest-artifact", "artifact": name})
            continue
        indexed.add(path)
        if not path.is_file():
            errors.append({"kind": "missing-manifest-artifact", "artifact": name})
            continue
        checked += 1
        if not item.get("sha256"):
            errors.append({"kind": "manifest-artifact-hash-missing", "artifact": name})
        elif _digest(path) != item["sha256"]:
            errors.append({"kind": "manifest-artifact-hash-mismatch", "artifact": name})
        if "bytes" in item and (type(item["bytes"]) is not int or item["bytes"] != path.stat().st_size):
            errors.append({"kind": "manifest-artifact-size-mismatch", "artifact": name})
    for path in sorted(set(required) - indexed):
        errors.append({"kind": "core-artifact-not-indexed", "artifact": str(path.relative_to(bundle))})
    return errors, checked


def _fraction(value):
    if isinstance(value, bool):
        raise ValueError("Boolean values are not rational positions")
    if isinstance(value, dict):
        if type(value.get("numerator")) is not int or type(value.get("denominator")) is not int or value["denominator"] <= 0:
            raise ValueError("Rational objects need integer numerator and positive integer denominator")
        return Fraction(value["numerator"], value["denominator"])
    return Fraction(str(value))


def _inside(bundle, relative):
    """Never follow an artifact reference outside a copied bundle."""
    path = (bundle / relative).resolve()
    if not path.is_relative_to(bundle):
        raise ValueError(f"Artifact must be inside the bundle: {relative}")
    return path


def _xml_root(path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            if "META-INF/container.xml" not in archive.namelist():
                raise ValueError("Compressed MusicXML must declare META-INF/container.xml")
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            candidates = [e.get("full-path") for e in container.iter()
                          if e.tag.rsplit("}", 1)[-1] == "rootfile"]
            names = [name for name in candidates if name in archive.namelist()]
            if len(names) != 1:
                raise ValueError("Compressed MusicXML must declare one score rootfile")
            raw = archive.read(names[0])
    else:
        raw = Path(path).read_bytes()
    root = ET.fromstring(raw)
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    if root.tag != "score-partwise":
        raise ValueError("Independent verification supports score-partwise MusicXML only")
    return root


def _read_events(path, *, canonical=False):
    """Walk sequential XML units, including changes within a measure.

    A chord member uses the preceding non-chord onset and never advances the
    cursor. A backup/forward uses divisions effective at that exact XML node.
    Octave-shift affects printing, so it deliberately does not change pitch.
    """
    events, errors = [], []
    root = _xml_root(path)
    for part in root.findall("part"):
        part_id, divisions = part.get("id"), Fraction(1)
        for measure_index, measure in enumerate(part.findall("measure"), 1):
            cursor, previous_onset, ordinal = Fraction(0), None, 0
            for element in measure:
                if element.tag == "attributes":
                    value = element.findtext("divisions")
                    if value is not None:
                        divisions = _fraction(value)
                        if divisions <= 0:
                            raise ValueError("MusicXML divisions must be positive")
                elif element.tag in ("backup", "forward"):
                    duration = _fraction(element.findtext("duration", "0")) / divisions
                    if duration <= 0:
                        errors.append({"kind": "invalid-movement", "part": part_id,
                                       "measureIndex": measure_index, "element": element.tag})
                    cursor += duration if element.tag == "forward" else -duration
                    if cursor < 0:
                        errors.append({"kind": "negative-cursor", "part": part_id,
                                       "measureIndex": measure_index, "offset": str(cursor)})
                    previous_onset = None
                elif element.tag == "note":
                    ordinal += 1
                    chord = element.find("chord") is not None
                    grace = element.find("grace") is not None
                    duration = _fraction(element.findtext("duration", "0")) / divisions
                    if duration < 0 or (duration == 0 and not grace):
                        errors.append({"kind": "invalid-note-duration", "part": part_id,
                                       "measureIndex": measure_index, "ordinal": ordinal})
                    if chord and previous_onset is None:
                        errors.append({"kind": "unanchored-chord", "part": part_id,
                                       "measureIndex": measure_index, "ordinal": ordinal})
                    onset = previous_onset if chord and previous_onset is not None else cursor
                    if not chord:
                        previous_onset = onset
                        cursor += duration
                    pitch_node = element.find("pitch")
                    pitch, pitch_name = None, None
                    if pitch_node is not None:
                        step = pitch_node.findtext("step")
                        octave = int(pitch_node.findtext("octave"))
                        alter = _fraction(pitch_node.findtext("alter", "0"))
                        pitch = Fraction((octave + 1) * 12 + _STEPS[step]) + alter
                        # Microtonal MIDI is retained; the existing model's name
                        # convention uses integral accidental spelling as well.
                        accidental = "#" * int(alter) if alter >= 0 else "b" * int(-alter)
                        pitch_name = f"{step}{accidental}{octave}"
                    identifier = element.get("id")
                    if canonical and not identifier:
                        errors.append({"kind": "missing-canonical-id", "part": part_id,
                                       "measureIndex": measure_index, "ordinal": ordinal})
                    events.append({"id": identifier, "sourcePart": part_id,
                                   "measureIndex": measure_index, "noteOrdinal": ordinal,
                                   "staff": int(element.findtext("staff", "1")),
                                   "voice": element.findtext("voice", "1"),
                                   "kind": "rest" if element.find("rest") is not None else "note",
                                   "pitch": pitch, "pitchName": pitch_name,
                                   "offset": onset, "duration": duration, "grace": grace,
                                   "tieTypes": sorted(t.get("type") for t in element.findall("tie"))})
    return events, errors


def _value(value):
    return str(value) if isinstance(value, Fraction) else value


def _canonical_check(events, symbols):
    errors, by_id, model_by_id = [], {}, {}
    for name, values, target in (("canonical", events, by_id), ("model", symbols, model_by_id)):
        for event in values:
            identifier = event.get("id")
            if not identifier or identifier in target:
                errors.append({"kind": f"{name}-duplicate-or-missing-id", "id": identifier})
            target[identifier] = event
    for identifier in sorted(set(by_id) - set(model_by_id), key=str):
        errors.append({"kind": "canonical-extra-symbol", "id": identifier})
    for identifier in sorted(set(model_by_id) - set(by_id), key=str):
        errors.append({"kind": "model-extra-symbol", "id": identifier})
    for identifier in sorted(set(by_id) & set(model_by_id), key=str):
        expected, actual = by_id[identifier], model_by_id[identifier]
        values = {field: actual.get(field) for field in
                  ("sourcePart", "measureIndex", "staff", "voice", "kind", "pitchName", "grace")}
        values["pitch"] = None if actual.get("pitch") is None else _fraction(actual["pitch"])
        values["tieTypes"] = sorted(actual.get("tieTypes", []))
        for field in ("offset", "duration"):
            values[field] = _fraction(actual["rational"][field])
        for field, value in values.items():
            if expected[field] != value:
                errors.append({"kind": "canonical-model-mismatch", "id": identifier,
                               "field": field, "canonical": _value(expected[field]), "model": _value(value)})
    return errors


def _source_check(events, symbols):
    """Check raw snapshots, allowing current, explicitly retained corrections.

    The original source offset/duration fields are floats in schema v2, unlike
    the current rational fields. Compare those floats only as snapshots; exact
    current timing is always checked separately against canonical MusicXML.
    """
    errors, models, changes = [], {}, []
    for event in symbols:
        snapshot = event.get("source", {})
        if snapshot.get("generated"):
            changes.append({"id": event.get("id"), "kind": "reviewed-insertion"})
            continue
        locator = (snapshot.get("part"), snapshot.get("measureIndex"), snapshot.get("noteOrdinal"))
        if None in locator or locator in models:
            errors.append({"kind": "invalid-or-duplicate-source-locator", "id": event.get("id")})
        models[locator] = event
    seen = set()
    for source in events:
        locator = (source["sourcePart"], source["measureIndex"], source["noteOrdinal"])
        seen.add(locator)
        event = models.get(locator)
        if event is None:
            errors.append({"kind": "source-symbol-missing-from-model", "sourceLocator": list(locator)})
            continue
        snapshot = event["source"]
        for field in ("kind", "staff", "voice"):
            # The current correction format cannot change these source fields.
            if source[field] != event.get(field):
                errors.append({"kind": "source-identity-mismatch", "sourceLocator": list(locator),
                               "id": event["id"], "field": field,
                               "source": source[field], "model": event.get(field)})
        for field in ("pitchName", "pitch", "offset", "duration", "tieTypes"):
            expected, actual = source[field], snapshot.get(field)
            if field in ("offset", "duration"):
                matches = isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isclose(
                    float(expected), actual, rel_tol=1e-12, abs_tol=1e-9)
            elif field == "tieTypes":
                matches = sorted(actual or []) == expected
            elif field == "pitch":
                matches = (expected is None and actual is None) or (
                    expected is not None and actual is not None and expected == _fraction(actual))
            else:
                matches = expected == actual
            if not matches:
                errors.append({"kind": "source-snapshot-mismatch", "sourceLocator": list(locator),
                               "id": event["id"], "field": field,
                               "source": _value(expected), "snapshot": actual})
        different = []
        for field in ("pitchName", "pitch", "offset", "duration", "tieTypes"):
            current = event.get(field)
            if field in ("offset", "duration"):
                current = _fraction(event["rational"][field])
            elif field == "pitch" and current is not None:
                current = _fraction(current)
            elif field == "tieTypes":
                current = sorted(current or [])
            if current != source[field]:
                different.append(field)
        if different:
            changes.append({"id": event["id"], "kind": "current-differs-from-original",
                            "fields": different})
    for locator in sorted(set(models) - seen, key=str):
        errors.append({"kind": "model-source-locator-absent-from-input", "sourceLocator": list(locator),
                       "id": models[locator].get("id")})
    return errors, changes


def _event_matches(expected, actual):
    if any(expected[field] != actual.get(field) for field in ("kind", "pitchName")):
        return False
    if any(_fraction(expected[field]) != _fraction(actual["rational"][field]) for field in ("offset", "duration")):
        return False
    if "voice" in expected and str(expected["voice"]) != str(actual.get("voice")):
        return False
    return "tieTypes" not in expected or sorted(expected["tieTypes"]) == sorted(actual.get("tieTypes", []))


def _facts_check(facts, model, hashes, symbols):
    errors = []
    if not isinstance(facts, dict):
        raise ValueError("Facts must be a JSON object")
    if facts.get("schemaVersion") != 1:
        errors.append({"kind": "unsupported-facts-schema"})
    if facts.get("revision") != model.get("revision"):
        errors.append({"kind": "facts-revision-mismatch", "expected": model.get("revision"),
                       "actual": facts.get("revision")})
    declared = facts.get("hashes", {})
    if not isinstance(declared, dict):
        raise ValueError("facts.hashes must be an object")
    for key in _HASH_KEYS + (("sourcePdf",) if hashes.get("sourcePdf") or "sourcePdf" in declared else ()):
        if not hashes.get(key) or declared.get(key) != hashes[key]:
            errors.append({"kind": "facts-hash-mismatch", "artifact": key,
                           "expected": hashes.get(key), "actual": declared.get(key)})
    if not isinstance(facts.get("scope"), str) or not facts["scope"].strip():
        errors.append({"kind": "missing-facts-scope"})
    bars = facts.get("staffBars", [])
    if not isinstance(bars, list):
        raise ValueError("facts.staffBars must be a list")
    known_parts = {item.get("sourcePart") for item in model.get("parts", [])}
    known_parts.update(event.get("sourcePart") for event in symbols)
    known_measures = {item.get("index") for item in model.get("measures", [])}
    known_measures.update(event.get("measureIndex") for event in symbols)
    known_staffs = {(event.get("sourcePart"), event.get("staff")) for event in symbols}
    known_staffs.update((part, 1) for part in known_parts)
    checked, selected, seen = 0, [], set()
    for bar in bars:
        if not isinstance(bar, dict):
            raise ValueError("Each facts staff bar must be an object")
        part, index, staff = bar["sourcePart"], bar["measureIndex"], bar["staff"]
        if not isinstance(part, str) or type(index) is not int or index < 1 or type(staff) is not int or staff < 1:
            raise ValueError("A facts staff bar needs sourcePart, positive integer measureIndex and staff")
        lane = (part, index, staff)
        if part not in known_parts or index not in known_measures or (part, staff) not in known_staffs:
            errors.append({"kind": "unknown-facts-staff-bar", "lane": list(lane)})
        if lane in seen:
            errors.append({"kind": "duplicate-facts-staff-bar", "lane": list(lane)})
            continue
        seen.add(lane)
        expected = bar["events"]
        if not isinstance(expected, list):
            raise ValueError("Each facts staff bar needs an events list")
        actual = [event for event in symbols if (event.get("sourcePart"), event.get("measureIndex"), event.get("staff")) == lane]
        selected.append({"sourcePart": part, "measureIndex": index, "staff": staff,
                         "expectedObjects": len(expected), "modelObjects": len(actual)})
        # Optional voice/ties produce a bipartite match, not a greedy match:
        # a broad expectation must not steal the only event matching a narrow
        # one. A perfect matching preserves multiplicity, including unisons.
        candidates = []
        for event in expected:
            checked += 1
            if not isinstance(event, dict):
                raise ValueError("Each frozen event must be an object")
            if event.get("kind") not in ("note", "rest") or "pitchName" not in event:
                raise ValueError("A facts event requires kind=note/rest and pitchName (null for rests/unpitched)")
            if _fraction(event["duration"]) < 0 or _fraction(event["offset"]) < 0:
                raise ValueError("Facts durations/offsets cannot be negative")
            if "tieTypes" in event and (not isinstance(event["tieTypes"], list) or
                    len(set(event["tieTypes"])) != len(event["tieTypes"]) or
                    not set(event["tieTypes"]).issubset({"start", "stop"})):
                raise ValueError("Facts tieTypes must contain unique start/stop values")
            candidates.append([i for i, target in enumerate(actual) if _event_matches(event, target)])
        owner = {}

        def augment(expected_index, visited):
            for actual_index in candidates[expected_index]:
                if actual_index in visited:
                    continue
                visited.add(actual_index)
                if actual_index not in owner or augment(owner[actual_index], visited):
                    owner[actual_index] = expected_index
                    return True
            return False

        for index_in_bar in sorted(range(len(expected)), key=lambda i: len(candidates[i])):
            if not augment(index_in_bar, set()):
                errors.append({"kind": "frozen-event-missing-or-different", "lane": list(lane),
                               "expectedIndex": index_in_bar, "expected": expected[index_in_bar]})
        for actual_index, event in enumerate(actual):
            if actual_index not in owner:
                errors.append({"kind": "unexpected-model-event", "lane": list(lane), "id": event.get("id")})
    if not checked:
        errors.append({"kind": "empty-source-facts", "message": "No positive source objects were supplied"})
    return {"status": "selected-staff-bars-matched" if not errors else "failed",
            "passed": not errors, "scope": facts.get("scope"), "selectedStaffBars": selected,
            "sourceObjectsChecked": checked, "errors": errors,
            "sourcePdfBinding": ("hash-matched" if hashes.get("sourcePdf") and declared.get("sourcePdf") == hashes["sourcePdf"]
                                 else "hash-mismatch" if hashes.get("sourcePdf") or declared.get("sourcePdf") else "not-frozen"),
            "interpretation": "Only the supplied complete staff bars were compared. A PDF hash binds identity, not the correctness of a reader's transcription. Unselected bars are not certified."}


def verify_bundle(bundle, facts=None):
    """Return a JSON-ready report; malformed/missing artifacts fail explicitly.

    ``facts`` accepts a version-1 dictionary or a JSON file path. No supplied
    facts means artifact consistency only; ``sourceFidelity.passed`` is null.
    All paths in the manifest are resolved inside the bundle. External paths
    retained in a build configuration are never required or opened.
    """
    bundle = Path(bundle).resolve()
    report = {"schemaVersion": 1, "checkedAt": datetime.now(timezone.utc).isoformat(),
              "revision": None, "bundle": str(bundle), "hashes": {},
              "artifactConsistency": {"passed": False, "errors": [],
                                      "scope": "All declared bundle files are hash-checked; source snapshots and canonical MusicXML/model symbols are compared. MIDI, audio and engraving semantics require separate checks."},
              "sourceFidelity": {"status": "not-supplied", "passed": None,
                                 "sourceObjectsChecked": 0,
                                 "interpretation": "Artifact agreement does not establish original-score accuracy."},
              "passed": False}
    errors = report["artifactConsistency"]["errors"]
    facts_started = False
    try:
        manifest_path = _inside(bundle, "manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_path = _inside(bundle, manifest["input"]["musicxml"])
        canonical_path = _inside(bundle, "assets/canonical.musicxml")
        model_path = _inside(bundle, "assets/interactive.json")
        model = json.loads(model_path.read_text(encoding="utf-8"))
        report["revision"] = model.get("revision")
        hashes = report["hashes"]
        hashes.update(sourceMusicxml=_digest(source_path), canonicalMusicxml=_digest(canonical_path),
                      model=_digest(model_path), manifest=_digest(manifest_path), sourcePdf=None)
        if not model.get("revision") or manifest.get("revision") != model.get("revision"):
            errors.append({"kind": "manifest-model-revision-mismatch", "manifest": manifest.get("revision"), "model": model.get("revision")})
        expected_hash = manifest["input"].get("musicxmlFileSha256")
        if expected_hash and expected_hash != hashes["sourceMusicxml"]:
            errors.append({"kind": "manifest-source-hash-mismatch"})
        # Only an explicit frozen-PDF declaration binds the source image. Old
        # configs may contain an unavailable absolute sourcePdf path; never
        # follow it or mistake that declaration for a verified snapshot.
        frozen_pdf = manifest.get("sourcePdf")
        if frozen_pdf:
            pdf_path = _inside(bundle, frozen_pdf["path"])
            hashes["sourcePdf"] = _digest(pdf_path)
            if not frozen_pdf.get("sha256") or frozen_pdf["sha256"] != hashes["sourcePdf"]:
                errors.append({"kind": "manifest-source-pdf-hash-mismatch"})
        report["sourcePdfStatus"] = "bundled-and-hashed" if hashes["sourcePdf"] else "not-frozen"
        artifact_errors, checked = _manifest_artifacts(bundle, manifest, (source_path, canonical_path, model_path))
        errors.extend(artifact_errors)
        report["artifactConsistency"]["manifestArtifactsChecked"] = checked
        source, source_parse_errors = _read_events(source_path)
        canonical, canonical_errors = _read_events(canonical_path, canonical=True)
        symbols = model["notes"] + model["rests"]
        # A saved OMR input can contain the very bad duration/cursor a reviewed
        # correction repaired. Preserve those diagnostics, but do not reject a
        # valid current artifact merely because its immutable input was wrong.
        errors.extend(dict(layer="canonical", **error) for error in canonical_errors)
        errors.extend(_canonical_check(canonical, symbols))
        source_snapshot_errors, modifications = _source_check(source, symbols)
        errors.extend(source_snapshot_errors)
        report["artifactConsistency"].update(
            sourceSymbols=len(source), canonicalSymbols=len(canonical), modelSymbols=len(symbols),
            sourceComparison="original-snapshots-checked", sourceToCurrentChanges=modifications,
            sourceDiagnostics=source_parse_errors,
            timingContract="Exact canonical/model rational quarter units; original schema-v2 float snapshots checked with 1e-9 tolerance",
            passed=not errors)
        if facts is not None:
            facts_started = True
            if isinstance(facts, dict):
                frozen = facts
                hashes["facts"] = sha256(json.dumps(facts, ensure_ascii=False, sort_keys=True,
                                                    separators=(",", ":")).encode("utf-8")).hexdigest()
                report["factsHashKind"] = "canonical-json"
            else:
                facts_path = Path(facts)
                frozen = json.loads(facts_path.read_text(encoding="utf-8"))
                hashes["facts"] = _digest(facts_path)
                report["factsHashKind"] = "file-bytes"
            report["sourceFidelity"] = _facts_check(frozen, model, hashes, symbols)
        report["passed"] = not errors and report["sourceFidelity"]["passed"] is not False
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError, ET.ParseError, zipfile.BadZipFile) as error:
        if facts_started:
            report["sourceFidelity"] = {"status": "invalid-facts", "passed": False,
                                         "sourceObjectsChecked": 0,
                                         "errors": [{"kind": "invalid-facts", "message": str(error)}]}
        else:
            errors.append({"kind": "invalid-verification-input", "message": str(error)})
            report["artifactConsistency"]["passed"] = False
    return report
