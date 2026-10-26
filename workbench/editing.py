"""Version-bound user edits rebuilt from verified, frozen score inputs.

No recognition, source replacement, publication, or network access happens here.
Callers own authorization, compare-and-swap of the published head, and job storage.
This module and its ordinary import chain support Python 3.9.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import re

from workbench.music_semantics import fraction, rational

POLICY_VERSION = "user-edit-transaction-v1"
DURATIONS = {"32nd": Fraction(1, 8), "16th": Fraction(1, 4),
             "eighth": Fraction(1, 2), "quarter": Fraction(1),
             "half": Fraction(2), "whole": Fraction(4)}
BEAM_LEVELS = {"eighth": 1, "16th": 2, "32nd": 3}
NATURALS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def _hash(path):
    digest = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _order(note):
    return (note["measureIndex"], tuple(note.get("source", {}).get("order", [note.get("source", {}).get("noteOrdinal", 0), 0])))


def _spelling(note, pitch, direction):
    """Preserve source spelling where practical, then use directional enharmonics."""
    match = re.fullmatch(r"([A-G])([#b]*)(-?\d+)", note.get("pitchName") or "")
    if match is None:
        raise ValueError("Pitch spelling is unavailable; an explicit spelling review is required")
    letter, _, octave = match.groups()
    alter = pitch - ((int(octave) + 1) * 12 + NATURALS[letter])
    if abs(alter) > 2:
        names = (("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
                 if direction > 0 else
                 ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"))
        return names[pitch % 12] + str(pitch // 12 - 1)
    return letter + ("#" * alter if alter >= 0 else "b" * -alter) + octave


def _groups(model, family, focus=None):
    """Return source-scoped dependency sets, without guessing a missing endpoint."""
    notes = model.get("notes", []) + model.get("rests", [])
    if family == "tie":
        groups = []
        for edge in model.get("relations", {}).get("ties", []):
            members = set(edge.get("noteIds", []))
            if members:
                groups.append(members)
        # Connected components ensure selecting one edge is not enough for a chain.
        changed = True
        while changed:
            changed = False
            for i, group in enumerate(groups):
                for j in range(i + 1, len(groups)):
                    if group & groups[j]:
                        groups[i] = group | groups.pop(j)
                        changed = True
                        break
                if changed:
                    break
        return groups
    lanes = defaultdict(list)
    focus_symbols = [note for note in notes if focus is not None and note["id"] in focus]
    focus_lanes = {(note["sourcePart"], note["voice"]) for note in focus_symbols}
    focus_bars = {(note["sourcePart"], note["voice"], note["measureIndex"]) for note in focus_symbols}
    for note in notes:
        if focus is not None and ((note["sourcePart"], note["voice"]) not in focus_lanes or
                                  family == "beam" and (note["sourcePart"], note["voice"], note["measureIndex"]) not in focus_bars):
            continue
        lanes[(note["sourcePart"], note["voice"])].append(note)
    groups = []
    for lane in lanes.values():
        pending = {}
        for note in sorted(lane, key=_order):
            marks = note.get("notation", {}).get("beams" if family == "beam" else "tuplets", [])
            # Beam groups cannot cross a written bar; tuplets can.
            if family == "beam":
                for key in list(pending):
                    if key[0] != note["measureIndex"]:
                        groups.append(pending.pop(key))
            for key in pending:
                pending[key].add(note["id"])
            for mark in marks:
                value = mark.get("value") if family == "beam" else mark.get("type")
                key = (note["measureIndex"] if family == "beam" else None, str(mark.get("number", "1")))
                if value in ("begin", "start"):
                    if key in pending:
                        if focus is None or pending[key] & focus or note["id"] in focus:
                            raise ValueError("Ambiguous %s group requires relationship review" % family)
                        groups.append(pending.pop(key))
                    pending[key] = {note["id"]}
                elif value in ("end", "stop"):
                    groups.append(pending.pop(key, {note["id"]}))
        groups.extend(pending.values())
    return groups


def _require_groups(selected, groups, family):
    for group in groups:
        if selected & group and not group <= selected:
            raise ValueError("Partial %s dependency: explicitly select the whole group: %s" % (family, ", ".join(sorted(group))))


def _rebeam(group, symbols, changes):
    """Rebuild ordinary beam levels inside an explicitly selected complete group."""
    ordered = sorted((symbols[ident] for ident in group), key=_order)
    for note in ordered:
        for beam in note.get("notation", {}).get("beams", []):
            if beam.get("attributes", {}).get("fan", "none") != "none":
                raise ValueError("Feathered beams require explicit relationship review")
        changes.setdefault(note["id"], {})["beams"] = []
    for level in range(1, 4):
        runs, run = [], []
        for note in ordered:
            current_type = changes.get(note["id"], {}).get("notation", note.get("notation", {})).get("type")
            if BEAM_LEVELS.get(current_type, 0) >= level:
                run.append(note)
            else:
                if run:
                    runs.append(run)
                run = []
        if run:
            runs.append(run)
        for run in runs:
            if len(run) < 2:
                continue  # The note type supplies a flag; do not invent an orphan beam.
            for index, note in enumerate(run):
                changes[note["id"]]["beams"].append({"number": level,
                    "value": "begin" if index == 0 else "end" if index == len(run) - 1 else "continue"})


def validate_patch(model, operations, *, expected_revision=None, before_values=None):
    """Validate and normalize a batch without writing or changing ``model``.

    Operations are pitch.delta {noteIds, semitones: -1|1} and duration.set
    {noteIds, type, dots: 0|1}. A patch envelope may carry typedOperations,
    baseModelRevision, selectedNoteIds and beforeValues. Before-values are base
    snapshot values; successive operations accumulate against that snapshot.
    """
    envelope = operations if isinstance(operations, dict) else {}
    if envelope:
        expected_revision = expected_revision or envelope.get("baseModelRevision")
        before_values = before_values if before_values is not None else envelope.get("beforeValues")
        operations = envelope.get("typedOperations", envelope.get("operations"))
    revision = model.get("revision", model.get("version"))
    if not expected_revision or expected_revision != revision:
        raise ValueError("Score revision changed; reload the current version")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 256:
        raise ValueError("Provide 1 through 256 typed operations")
    symbols = {note["id"]: note for note in model.get("notes", []) + model.get("rests", [])}
    working = deepcopy(symbols)
    if before_values is not None:
        if not isinstance(before_values, dict):
            raise ValueError("beforeValues must map note IDs to original fields")
        for ident, values in before_values.items():
            if ident not in symbols or not isinstance(values, dict) or not values:
                raise ValueError("Invalid before-value target")
            for field, value in values.items():
                if field not in ("pitch", "pitchName", "duration", "notation", "tieTypes", "chordId", "voice", "staff"):
                    raise ValueError("Unsupported before-value field: %s" % field)
                if symbols[ident].get(field) != value:
                    raise ValueError("Before-value changed: %s/%s" % (ident, field))
    tie_groups = _groups(model, "tie")
    beam_groups = []
    chords = defaultdict(set)
    for note in symbols.values():
        chords[note.get("chordId", note["id"])].add(note["id"])
    changes, normalized, duration_ids, pitch_ids = {}, [], set(), set()
    respellings = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("Each operation must be an object")
        op = operation.get("op")
        allowed = {"op", "noteIds", "semitones", "delta"} if op == "pitch.delta" else {"op", "noteIds", "type", "dots"}
        if op not in ("pitch.delta", "duration.set") or set(operation) - allowed:
            raise ValueError("Unsupported typed operation or fields")
        ids = operation.get("noteIds", envelope.get("selectedNoteIds"))
        if (not isinstance(ids, list) or not ids or len(ids) > 4000
                or any(not isinstance(ident, str) or ident not in symbols for ident in ids)
                or len(ids) != len(set(ids))):
            raise ValueError("Select existing, unique note IDs")
        selected = set(ids)
        if envelope.get("selectedNoteIds") is not None and not selected <= set(envelope["selectedNoteIds"]):
            raise ValueError("Operation exceeds the explicitly selected note IDs")
        if before_values is not None and not selected <= set(before_values):
            raise ValueError("Every edited note requires a before-value snapshot")
        for ident in ids:
            note = symbols[ident]
            if note.get("grace") or note.get("placeholder") or note.get("source", {}).get("generated"):
                raise ValueError("Grace, placeholder and inserted symbols require a dedicated source edit")
        if op == "pitch.delta":
            delta = operation.get("semitones", operation.get("delta"))
            if type(delta) is not int or delta not in (-1, 1):
                raise ValueError("pitch.delta requires exactly one semitone up or down")
            _require_groups(selected, tie_groups, "tie")
            for ident in ids:
                note = working[ident]
                if type(note.get("pitch")) is not int:
                    raise ValueError("Pitch edits require ordinary pitched notes")
                target = note["pitch"] + delta
                if not 0 <= target <= 127:
                    raise ValueError("Pitch edit exceeds MIDI range 0 through 127")
                spelling = _spelling(note, target, delta)
                previous = note["pitchName"]
                # Ordinary accidental changes retain their letter and written octave.
                if (previous[0], re.search(r"-?\d+$", previous).group()) != (
                        spelling[0], re.search(r"-?\d+$", spelling).group()):
                    respellings.append({"noteId": ident, "before": previous, "after": spelling,
                                        "reason": "standard-enharmonic-beyond-double-accidental"})
                changes.setdefault(ident, {}).update(pitch=target, pitchName=spelling)
                note.update(pitch=target, pitchName=spelling)
            pitch_ids.update(ids)
            normalized.append({"op": op, "noteIds": ids, "semitones": delta})
        else:
            type_name, dots = operation.get("type"), operation.get("dots", 0)
            if type_name not in DURATIONS or type(dots) is not int or dots not in (0, 1):
                raise ValueError("Choose a supported note type and zero or one augmentation dot")
            operation_beams = _groups(model, "beam", selected)
            tuplet_groups = _groups(model, "tuplet", selected)
            beam_groups.extend(group for group in operation_beams if group not in beam_groups)
            _require_groups(selected, operation_beams, "beam")
            _require_groups(selected, tuplet_groups, "tuplet")
            _require_groups(selected, chords.values(), "chord")
            for ident in ids:
                note = symbols[ident]
                if note.get("measureRest"):
                    raise ValueError("A whole-measure rest is not an ordinary whole note")
                current = note.get("notation", {})
                if current.get("timeModification") and not any(ident in group and len(group) > 1 for group in tuplet_groups):
                    raise ValueError("Unbounded tuplet ratio requires explicit complete-group review")
                duration = DURATIONS[type_name] * (Fraction(3, 2) if dots else 1)
                change = changes.setdefault(ident, {})
                change.update(duration=float(duration), notation={"type": type_name, "dots": dots, "timeModification": None})
                if current.get("tuplets"):
                    change["tuplets"] = []
                working[ident].update(duration=float(duration), notation=deepcopy(change["notation"]))
            duration_ids.update(ids)
            normalized.append({"op": op, "noteIds": ids, "type": type_name, "dots": dots,
                               "durationRational": rational(duration)})
    for group in beam_groups:
        if group & duration_ids:
            _rebeam(group, symbols, changes)
    # Isolated pre-existing beam hooks must also be removed/rebuilt explicitly.
    for ident in duration_ids:
        if symbols[ident].get("notation", {}).get("beams") and not any(ident in group for group in beam_groups):
            _rebeam({ident}, symbols, changes)
    for members in chords.values():
        original = Counter(symbols[ident].get("pitch") for ident in members)
        edited = Counter(working[ident].get("pitch") for ident in members)
        if any(count > max(1, original.get(pitch, 0)) for pitch, count in edited.items()):
            raise ValueError("Pitch edit creates a duplicate chord tone")
    return {"policyVersion": POLICY_VERSION, "baseRevision": revision,
            "operations": normalized, "corrections": changes,
            "affectedNoteIds": sorted(changes), "pitchNoteIds": sorted(pitch_ids),
            "durationNoteIds": sorted(duration_ids), "respellings": respellings}


def read_frozen_bundle(base_bundle):
    """Verify the complete artifact index and resolve no external source paths."""
    bundle = Path(base_bundle).resolve()
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") not in ("complete", "complete-with-open-issues"):
        raise ValueError("Editing requires a complete immutable bundle")
    indexed = {}
    for item in manifest.get("artifacts", []):
        name = item.get("path")
        if not isinstance(name, str) or not name or name in indexed:
            raise ValueError("Invalid or duplicate artifact path")
        path = (bundle / name).resolve()
        if Path(name).is_absolute() or not path.is_relative_to(bundle):
            raise ValueError("Artifact escapes the frozen bundle")
        if not path.is_file() or _hash(path) != item.get("sha256"):
            raise ValueError("Frozen artifact changed or is missing: %s" % name)
        indexed[name] = path
    for path in (bundle / "assets").rglob("*"):
        if path.is_file() and path.relative_to(bundle).as_posix() not in indexed:
            raise ValueError("Unindexed score asset")
    def required(name):
        if not name or name not in indexed:
            raise ValueError("Frozen bundle is missing a required indexed input: %s" % name)
        return indexed[name]
    model = json.loads(required("assets/interactive.json").read_text(encoding="utf-8"))
    if model.get("revision") != manifest.get("revision"):
        raise ValueError("Frozen manifest/model revision mismatch")
    source = required(manifest.get("input", {}).get("musicxml"))
    ledger = json.loads(required("inputs/corrections.json").read_text(encoding="utf-8"))
    config = deepcopy(manifest["config"])
    config.update(input={"musicxml": str(source), "omr": str(required(manifest["input"]["omr"])) if manifest["input"].get("omr") else None,
                         "scoreIndex": None},
                  reviewFile=None, sourcePdf=str(required(manifest["sourcePdf"]["path"])) if manifest.get("sourcePdf") else None,
                  tempoEvidence=str(indexed["inputs/tempo-evidence.json"]) if "inputs/tempo-evidence.json" in indexed else None,
                  output=None)
    return {"bundle": bundle, "manifest": manifest, "model": model,
            "config": config, "source": source, "ledger": ledger}


def _issue_key(issue):
    evidence = issue.get("evidence", {})
    return (issue["kind"], issue.get("measure"), evidence.get("sourcePart"),
            evidence.get("voice"), evidence.get("staff"), tuple(evidence.get("staffs", [])),
            tuple(sorted(evidence.get("noteIds", []))), tuple(evidence.get("measureIndices", [])))


def _burden(issue, model):
    evidence = issue.get("evidence", {})
    if "observed" in evidence and "expected" in evidence:
        return max(0, evidence["observed"] - evidence["expected"])
    for key in ("gap", "overlap"):
        if isinstance(evidence.get(key), list) and len(evidence[key]) == 2:
            return evidence[key][1] - evidence[key][0]
    if "gaps" in evidence:
        return sum(end - start for start, end in evidence["gaps"])
    if issue["kind"] == "lane-overflow" and "expected" in evidence:
        notes = {note["id"]: note for note in model["notes"] + model["rests"]}
        return max([0] + [max(0, notes[ident]["offset"] + notes[ident]["duration"] - evidence["expected"])
                         for ident in evidence.get("noteIds", []) if ident in notes])
    return 1


def validate_edit_delta(before, after, patch):
    """Require stable source IDs, authorized fields, and no new/worse contradictions."""
    old = {note["id"]: note for note in before["notes"] + before["rests"]}
    new = {note["id"]: note for note in after["notes"] + after["rests"]}
    if set(old) != set(new):
        raise ValueError("Edit changed source symbol IDs")
    for ident in old:
        a = {key: value for key, value in old[ident]["source"].items() if key not in ("reviewed", "calibration")}
        b = {key: value for key, value in new[ident]["source"].items() if key not in ("reviewed", "calibration")}
        if a != b:
            raise ValueError("Edit changed original source provenance")
        allowed = patch["corrections"].get(ident, {})
        for field in ("pitch", "pitchName", "duration", "tieTypes", "slurs"):
            if field not in allowed and old[ident].get(field) != new[ident].get(field):
                raise ValueError("Unauthorized musical field changed: %s/%s" % (ident, field))
        for field in ("type", "dots", "timeModification", "beams", "tuplets", "articulations"):
            authorized = "notation" in allowed if field in ("type", "dots", "timeModification") else field in allowed
            if not authorized and old[ident].get("notation", {}).get(field) != new[ident].get("notation", {}).get(field):
                raise ValueError("Unauthorized notation field changed: %s/%s" % (ident, field))
    old_bad = {_issue_key(item): item for item in before["audit"]["issues"] if item.get("rhythmBlocking")}
    current = [item for item in after["audit"]["issues"] if item.get("rhythmBlocking")]
    introduced = [item for item in current if _issue_key(item) not in old_bad or
                  _burden(item, after) > _burden(old_bad[_issue_key(item)], before) + 1e-9]
    remaining = [item for item in current if item not in introduced]
    return {"canPublish": not introduced, "newIssues": introduced,
            "remainingBaselineIssues": remaining, "originalProvenancePreserved": True,
            "sourceIdsPreserved": len(old), "appliedSymbols": len(patch["corrections"])}


def _voice_changes(before, after):
    """Report interval unions rather than adding simultaneous chord tones."""
    def summarize(model):
        lanes = defaultdict(list)
        for note in model["notes"] + model["rests"] + model.get("timelineSpacers", []):
            if note.get("grace") or note.get("placeholder"):
                continue
            start = fraction(note.get("rational", {}).get("offset", note["offset"]))
            duration = fraction(note.get("rational", {}).get("duration", note["duration"]))
            lanes[(note["measureIndex"], note["sourcePart"], note["voice"])].append((start, start + duration))
        result = {}
        for key, spans in lanes.items():
            merged = []
            for start, end in sorted(spans):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            covered = sum((end - start for start, end in merged), Fraction(0))
            result[key] = {"coveredQuarterUnits": float(covered), "coveredRational": rational(covered),
                           "extentQuarterUnits": float(max(end for _, end in merged))}
        return result
    old, new = summarize(before), summarize(after)
    return [{"measureIndex": key[0], "sourcePart": key[1], "voice": key[2],
             "before": old.get(key), "after": new.get(key)} for key in sorted(set(old) | set(new))
            if old.get(key) != new.get(key)]


def _baseline_migration(saved, rebuilt):
    """Permit a policy-only upgrade only after exact musical-object comparison."""
    if saved["provenance"]["sourceSha256"] != rebuilt["provenance"]["sourceSha256"]:
        raise ValueError("Frozen source identity cannot reproduce the published model")
    metadata_changes = {"restPlaceholderIds": [], "measureCoverageRecomputed": False}
    for field in ("notes", "rests", "measures", "relations", "performanceEvents", "timelineSpacers",
                  "tempos", "tempoMetadata", "parts", "duration", "totalBeats"):
        old, new = saved.get(field), rebuilt.get(field)
        if field == "notes":
            old = [{key: value for key, value in note.items() if key != "svgPage"} for note in old]
        elif field == "rests":
            # v2.3's project-specific hidden-rest annotation was removed in
            # v2.4. It does not change a rest's identity, spelling, timing,
            # source, or any sounding event; it only changes coverage display.
            metadata_changes["restPlaceholderIds"] = [a["id"] for a, b in zip(old, new)
                                                       if a.get("placeholder") != b.get("placeholder")]
            old = [{key: value for key, value in note.items() if key != "placeholder"} for note in old]
            new = [{key: value for key, value in note.items() if key != "placeholder"} for note in new]
        elif field == "measures":
            metadata_changes["measureCoverageRecomputed"] = any(
                a.get(key) != b.get(key) for a, b in zip(old, new) for key in ("laneCoverage", "voiceCoverage"))
            old = [{key: value for key, value in measure.items() if key not in ("laneCoverage", "voiceCoverage")} for measure in old]
            new = [{key: value for key, value in measure.items() if key not in ("laneCoverage", "voiceCoverage")} for measure in new]
        if old != new:
            raise ValueError("Frozen model policy migration changes %s; explicit source migration is required" % field)
    return {"fromPolicy": saved.get("modelPolicy"), "toPolicy": rebuilt.get("modelPolicy"),
            "publishedRevision": saved["revision"], "rebuiltBaselineRevision": rebuilt["revision"],
            "musicalObjectsExactlyEquivalent": True, "sourceReviewed": False,
            "metadataChanges": metadata_changes}


def build_edit_bundle(base_bundle, output_dir, operations, *, expected_revision,
                      reason, stage_callback=None, allow_invalid_draft=False,
                      before_values=None):
    """Build a full isolated bundle, returning a receipt; never switch a live pointer.

    Existing baseline issues retain draft-audio labels. New/worse contradictions
    are rejected by default; an explicitly requested invalid draft is never
    publishable. Every edited revision loses inherited source-review approval.
    """
    from workbench.workflow import build, prepare_model, write_json
    def stage(name, status="running", **details):
        if stage_callback:
            stage_callback({"stage": name, "status": status, **details})
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError("Provide a nonempty edit reason of at most 2000 characters")
    output = Path(output_dir).resolve()
    if output.exists():
        raise ValueError("Edit output already exists")
    base = read_frozen_bundle(base_bundle)
    if output == base["bundle"] or output.is_relative_to(base["bundle"]):
        raise ValueError("Edit output must be outside the immutable base bundle")
    stage("validate-input")
    patch = validate_patch(base["model"], operations, expected_revision=expected_revision,
                           before_values=before_values)
    config, ledger = base["config"], deepcopy(base["ledger"])
    # An author's absolute SoundFont path is transport metadata, not permission
    # to read that path on a worker. Use the host's explicit runtime setting
    # (or its standard installed font), never the imported manifest path.
    from runtime import find_soundfont
    config["soundfont"] = str(find_soundfont())
    before = prepare_model(config, base["source"], config["input"].get("omr"), ledger)
    migration = None
    if before["revision"] != expected_revision:
        migration = _baseline_migration(base["model"], before)
    for ident, fields in patch["corrections"].items():
        change = ledger.setdefault(ident, {})
        change.pop("calibration", None)
        change.pop("status", None)
        change.update(deepcopy(fields), reason=reason.strip())
    after = prepare_model(config, base["source"], config["input"].get("omr"), ledger)
    delta = validate_edit_delta(before, after, patch)
    if delta["newIssues"] and not allow_invalid_draft:
        kinds = sorted({item["kind"] for item in delta["newIssues"]})
        raise ValueError("Edit introduces or worsens structural conflicts: " + ", ".join(kinds))
    stage("validate-input", "complete", canPublish=delta["canPublish"])
    output.parent.mkdir(parents=True, exist_ok=True)
    # Keep the transaction beside the build so workflow's existing-path guard
    # remains intact. All files are frozen into the final bundle below.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="score-edit-", dir=str(output.parent)) as temporary:
        ledger_path = Path(temporary) / "corrections.json"
        write_json(ledger_path, ledger)
        config.update(corrections=str(ledger_path), reviewFile=None,
                      rhythmPolicy="draft" if after["rhythmValidation"]["status"] != "validated" else "strict")
        # Revisions bind the playback/rhythm policy. Preserve an existing draft
        # policy even when a revision repairs its last structural problem.
        config["rhythmPolicy"] = base["config"].get("rhythmPolicy", "strict") if after["rhythmValidation"]["status"] == "validated" else "draft"
        stage("rebuild")
        manifest = build(config, output, stage_callback=stage_callback)
    stage("verify")
    rebuilt = json.loads((output / "assets/interactive.json").read_text(encoding="utf-8"))
    final_delta = validate_edit_delta(before, rebuilt, patch)
    if final_delta["newIssues"] and not allow_invalid_draft:
        raise ValueError("Full rebuild introduced structural conflicts")
    if rebuilt["provenance"]["sourceSha256"] != before["provenance"]["sourceSha256"]:
        raise ValueError("Rebuild changed frozen source identity")
    receipt = {"schemaVersion": 1, "policyVersion": POLICY_VERSION,
               "origin": "user-edit", "sourceReviewed": False,
               "baselineMigration": migration,
               "beforeRevision": expected_revision, "revision": rebuilt["revision"],
               "bundle": str(output), "status": "complete" if final_delta["canPublish"] else "invalid-draft",
               "reason": reason.strip(), "operations": patch["operations"],
               "affectedNoteIds": patch["affectedNoteIds"], **final_delta,
               "validation": {"delta": final_delta, "rhythm": rebuilt["rhythmValidation"],
                              "audio": rebuilt.get("audioValidation"), "sourceReviewed": False,
                              "migration": migration,
                              "voiceChanges": _voice_changes(before, rebuilt),
                              "respellings": patch["respellings"]}}
    # A complete receipt is part of the same immutable artifact index. Do not
    # carry reviewRecord/agentReview from the previous source-reviewed revision.
    manifest.pop("reviewRecord", None)
    manifest["userEdit"] = {key: receipt[key] for key in ("origin", "beforeRevision", "revision", "sourceReviewed", "affectedNoteIds", "canPublish")}
    write_json(output / "edit-receipt.json", receipt)
    # Resolve the saved config to its own frozen ledger/input, so a subsequent
    # edit never depends on the now-deleted temporary transaction directory.
    manifest["config"].update(
        input={"musicxml": str(output / manifest["input"]["musicxml"]),
               "omr": str(output / manifest["input"]["omr"]) if manifest["input"].get("omr") else None,
               "scoreIndex": None},
        corrections=str(output / "inputs/corrections.json"),
        tempoEvidence=str(output / "inputs/tempo-evidence.json") if (output / "inputs/tempo-evidence.json").is_file() else None,
        sourcePdf=str(output / manifest["sourcePdf"]["path"]) if manifest.get("sourcePdf") else None,
        output=None)
    write_json(output / "resolved-config.json", manifest["config"])
    manifest["artifacts"] = [{"path": path.relative_to(output).as_posix(), "bytes": path.stat().st_size,
                              "sha256": _hash(path)} for path in sorted(output.rglob("*"))
                             if path.is_file() and path != output / "manifest.json"]
    write_json(output / "manifest.json", manifest)
    stage("verify", "complete", revision=receipt["revision"], canPublish=receipt["canPublish"])
    return receipt
