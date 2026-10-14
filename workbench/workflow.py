"""Reusable, staged score build; leaves the current workbench assets untouched.

Run with the project runtime: env/venv/bin/python -m workbench.workflow --help.
The manifest distinguishes successful artifact generation from score approval.
"""
from __future__ import annotations

import argparse
from array import array
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
import math
from pathlib import Path
import shutil
import sys
import time
import wave
import xml.etree.ElementTree as ET

from workbench.score_model import _measure_number, _read_source, build_model, public_model, write_canonical
from workbench.tempo import load_evidence
from workbench.validation import POLICIES, require_rhythm

ROOT = Path(__file__).resolve().parents[1]
GROUPS = {"S", "A", "T", "B", "Piano"}
STAGES = ("input", "canonical-audit", "engraving", "audio", "package")
CONFIG_KEYS = {"schemaVersion", "title", "input", "corrections", "reviewFile", "tempoEvidence", "soundfont", "partMap", "mergeLogicalParts", "playback", "output", "rhythmPolicy", "sourcePdf"}


def file_hash(path):
    digest = sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def resolve_path(value, base, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty path string")
    path = Path(value).expanduser()
    return (base / path if not path.is_absolute() else path).resolve()


def load_config(path=None, overrides=None):
    """Resolve configuration paths relative to its JSON file, never the shell cwd."""
    config_path = Path(path).resolve() if path else None
    base = config_path.parent if config_path else Path.cwd()
    raw = json.loads(config_path.read_text(encoding="utf-8")) if config_path else {}
    if not isinstance(raw, dict) or set(raw) - CONFIG_KEYS:
        raise ValueError("Unknown workflow configuration fields")
    raw.update(overrides or {})
    if raw.get("schemaVersion", 1) != 1:
        raise ValueError("Unsupported workflow schemaVersion")
    rhythm_policy = raw.get("rhythmPolicy", "strict")
    if rhythm_policy not in POLICIES:
        raise ValueError("rhythmPolicy must be strict or draft")
    source = raw.get("input", {})
    if not isinstance(source, dict) or set(source) - {"musicxml", "pdf", "omr", "scoreIndex"}:
        raise ValueError("input supports musicxml, pdf, omr and scoreIndex")
    kinds = [key for key in ("musicxml", "pdf") if source.get(key)]
    if len(kinds) != 1:
        raise ValueError("Choose exactly one input.musicxml or input.pdf")
    kind = kinds[0]
    input_path = resolve_path(source[kind], base, f"input.{kind}")
    if not input_path.is_file():
        raise ValueError(f"Input does not exist: {input_path}")
    extensions = {"musicxml": {".musicxml", ".mxl", ".xml"}, "pdf": {".pdf"}}
    if input_path.suffix.lower() not in extensions[kind]:
        raise ValueError(f"Unsupported {kind} file extension")
    score_index = source.get("scoreIndex")
    if score_index is not None and (type(score_index) is not int or score_index < 0 or kind != "pdf"):
        raise ValueError("input.scoreIndex is a zero-based PDF export index")
    omr = resolve_path(source["omr"], base, "input.omr") if source.get("omr") else None
    if kind == "pdf" and omr is not None:
        raise ValueError("PDF imports use the OMR produced in their own build")
    if omr is not None and not omr.is_file():
        raise ValueError(f"OMR file does not exist: {omr}")
    if kind == "musicxml" and omr is None and input_path.with_suffix(".omr").is_file():
        omr = input_path.with_suffix(".omr")
    corrections = resolve_path(raw["corrections"], base, "corrections") if raw.get("corrections") else None
    if corrections is not None and not corrections.is_file():
        raise ValueError(f"Correction ledger does not exist: {corrections}")
    review = resolve_path(raw["reviewFile"], base, "reviewFile") if raw.get("reviewFile") else None
    if review is not None and not review.is_file():
        raise ValueError(f"Review record does not exist: {review}")
    tempo_evidence = resolve_path(raw["tempoEvidence"], base, "tempoEvidence") if raw.get("tempoEvidence") else None
    source_pdf = resolve_path(raw["sourcePdf"], base, "sourcePdf") if raw.get("sourcePdf") else None
    if source_pdf is not None and (not source_pdf.is_file() or source_pdf.suffix.lower() != ".pdf"):
        raise ValueError("sourcePdf must point to the original PDF score")
    if tempo_evidence is not None and not tempo_evidence.is_file():
        raise ValueError(f"Tempo evidence does not exist: {tempo_evidence}")
    if tempo_evidence is not None:
        load_evidence(tempo_evidence)
    from runtime import find_soundfont
    soundfont = resolve_path(raw.get("soundfont") or str(find_soundfont()), base, "soundfont")
    if not soundfont.is_file():
        raise ValueError(f"Soundfont does not exist: {soundfont}")
    part_map = raw.get("partMap", {})
    if not isinstance(part_map, dict) or any(not isinstance(k, str) or not isinstance(v, str) or v not in GROUPS for k, v in part_map.items()):
        raise ValueError("partMap maps source part IDs to S, A, T, B or Piano")
    merge_parts = raw.get("mergeLogicalParts", False)
    if type(merge_parts) is not bool:
        raise ValueError("mergeLogicalParts must be a boolean")
    if merge_parts:
        raise ValueError("mergeLogicalParts is unsupported; the plugin preserves every source part")
    playback = raw.get("playback", {})
    if not isinstance(playback, dict) or set(playback) - {"transpose", "program", "gains", "a4"}:
        raise ValueError("Unknown playback settings")
    from workbench.playback import validate_settings
    validate_settings(playback)
    title = raw.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise ValueError("title must be a nonempty string")
    output = resolve_path(raw["output"], base, "output") if raw.get("output") else None
    return {"schemaVersion": 1, "title": title, "rhythmPolicy": rhythm_policy, "input": {kind: str(input_path), "omr": str(omr) if omr else None,
            "scoreIndex": score_index}, "corrections": str(corrections) if corrections else None,
            "reviewFile": str(review) if review else None,
            "sourcePdf": str(source_pdf) if source_pdf else None,
            "tempoEvidence": str(tempo_evidence) if tempo_evidence else None,
            "soundfont": str(soundfont), "partMap": part_map,
            "mergeLogicalParts": merge_parts,
            "playback": {"transpose": 0, "program": 52, "gains": {}, "a4": 440, **playback},
            "output": str(output) if output else None}


def read_corrections(path):
    ledger = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    if not isinstance(ledger, dict):
        raise ValueError("Correction ledger must be an object")
    entries = []
    for object_id, change in ledger.items():
        if object_id == "__meta__":
            continue
        if not isinstance(change, dict):
            raise ValueError(f"Invalid correction: {object_id}")
        if object_id == "__measures__":
            entries.extend((f"measure-{key}", item) for key, item in change.items())
        elif object_id == "__insertions__":
            entries.extend(change.items())
        elif object_id == "__clefs__":
            for part, measures in change.items():
                if not isinstance(measures, dict):
                    raise ValueError(f"Invalid clef correction: {part}")
                entries.extend((f"clef-{part}-{measure}", item) for measure, item in measures.items())
        else:
            entries.append((object_id, change))
    for object_id, change in entries:
        if not isinstance(change, dict) or not isinstance(change.get("reason"), str) or not change["reason"].strip():
            raise ValueError(f"A saved correction requires an evidence reason: {object_id}")
        if "pitch" in change and (type(change["pitch"]) is not int or not 0 <= change["pitch"] <= 127):
            raise ValueError(f"Invalid MIDI pitch: {object_id}")
        if "duration" in change:
            value = change["duration"]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid quarter-beat duration: {object_id}")
    return ledger


def validate_correction_targets(ledger, source_root):
    """Reject reserved ledger entries that the shared parser would never apply."""
    parts = source_root.findall("part")
    if not parts:
        raise ValueError("MusicXML contains no parts")

    def measure_labels(part):
        return {str(_measure_number(measure.get("number"), index))
                for index, measure in enumerate(part.findall("measure"))}

    timeline = measure_labels(parts[0])
    unknown = set(ledger.get("__measures__", {})) - timeline
    if unknown:
        raise ValueError(f"Measure correction refers to unknown source measures: {sorted(unknown)}")
    part_measures = {part.get("id"): measure_labels(part) for part in parts}
    clefs = ledger.get("__clefs__", {})
    unknown = set(clefs) - set(part_measures)
    if unknown:
        raise ValueError(f"Clef correction refers to unknown source part IDs: {sorted(unknown)}")
    for part, changes in clefs.items():
        unknown = set(changes) - (timeline & part_measures[part])
        if unknown:
            raise ValueError(f"Clef correction for {part} refers to unknown source measures: {sorted(unknown)}")
    reference_numbers = [str(_measure_number(m.get("number"), i)) for i, m in enumerate(parts[0].findall("measure"))]
    measure_indices = {number: i + 1 for i, number in enumerate(reference_numbers)}
    source_note_ids = {}
    for part in parts:
        for index, measure in enumerate(part.findall("measure")):
            number = str(_measure_number(measure.get("number"), index))
            canonical_index = measure_indices.get(number)
            for ordinal, note in enumerate(measure.findall("note"), 1):
                source_note_ids[f"n-{part.get('id')}-m{canonical_index}-o{ordinal}"] = (part.get("id"), canonical_index)
    for object_id, entry in ledger.get("__insertions__", {}).items():
        target = (entry.get("sourcePart"), entry.get("measureIndex"))
        if source_note_ids.get(entry.get("afterNoteId")) != target:
            raise ValueError(f"Rest insertion refers to unknown or mismatched source anchor: {object_id}")


def recognized_part_role(name):
    """Use explicit source names only, never ordinal IDs or inferred pitch ranges."""
    import re
    text = re.sub(r"[.\s_-]+", " ", str(name).strip().casefold()).strip()
    aliases = {"s": "S", "soprano": "S", "女高音": "S", "a": "A", "alto": "A", "女低音": "A",
               "t": "T", "tenor": "T", "男高音": "T", "b": "B", "bass": "B", "男低音": "B",
               "piano": "Piano", "pianoforte": "Piano", "pno": "Piano", "钢琴": "Piano"}
    return aliases.get(text)


def map_parts(model, mapping):
    known = {p["sourcePart"] for p in model["parts"]}
    if set(mapping) - known:
        raise ValueError(f"partMap contains unknown source part IDs: {sorted(set(mapping) - known)}")
    assignments = {}
    for part in model["parts"]:
        group = mapping.get(part["sourcePart"], part["part"])
        if group not in GROUPS:
            raise ValueError(f"Add partMap for source part {part['sourcePart']} ({part['name']})")
        part["part"] = group
        assignments[part["sourcePart"]] = group
    for event in model["notes"] + model["rests"]:
        event["part"] = assignments[event["sourcePart"]]
    by_id = {note["id"]: note for note in model["notes"]}
    for event in model["performanceEvents"]:
        first = by_id[event["noteIds"][0]]
        event["part"] = assignments[first["sourcePart"]]
    return assignments


def prepare_model(config, source, omr=None, corrections=None, tempo_context=None):
    """Prepare one configured model for both calibration and artifact generation.

    Source names determine unmapped parts; only explicit assignments can group
    arbitrary imports. The revision binds the same musical and playback options
    regardless of whether the source was copied into a build directory.
    """
    corrections = read_corrections(config.get("corrections")) if corrections is None else corrections
    source_root, _ = _read_source(Path(source))
    validate_correction_targets(corrections, source_root)
    if tempo_context is None and config.get("tempoEvidence"):
        tempo_context = load_evidence(config["tempoEvidence"])
    model = build_model(corrections, source_path=source, omr_path=omr or "", tempo_context=tempo_context)
    source_names = {part.get("id"): part.findtext("part-name", part.get("id"))
                    for part in source_root.findall("part-list/score-part")}
    for part in model["parts"]:
        part["part"] = recognized_part_role(source_names.get(part["sourcePart"], part["sourcePart"])) or source_names.get(part["sourcePart"], part["sourcePart"])
    model["title"] = config.get("title") or source_root.findtext("work/work-title") or Path(source).stem
    assignments = map_parts(model, config.get("partMap", {}))
    seed = json.dumps({"sourceVersion": model["version"], "partMap": assignments,
                       "mergeLogicalParts": config.get("mergeLogicalParts", False),
                       "playback": config["playback"], "rhythmPolicy": config.get("rhythmPolicy", "strict"),
                       "title": model["title"]}, sort_keys=True, ensure_ascii=False)
    model["revision"] = sha256(seed.encode()).hexdigest()[:16]
    model["playbackSettings"] = deepcopy(config["playback"])
    return model


def attach_review(model, path, ledger):
    """Bind a bounded review record to the source and applied ledger, without importing proposals."""
    review = json.loads(Path(path).read_text(encoding="utf-8"))
    expected_hash = review.get("sourceSha256")
    if not expected_hash:
        recorded_source = Path(review.get("source", ""))
        if not recorded_source.is_absolute():
            recorded_source = ROOT / recorded_source
        if not recorded_source.is_file():
            raise ValueError("reviewFile requires sourceSha256 or an accessible root-relative source path")
        _, raw = _read_source(recorded_source)
        expected_hash = sha256(raw).hexdigest()
    if expected_hash != model["provenance"]["sourceSha256"]:
        raise ValueError("Review record refers to a different source score")
    proposals = review.get("corrections", {})
    matched = all(isinstance(proposal, dict) and all(ledger.get(object_id, {}).get(key) == value
                  for key, value in proposal.items() if key != "reason") for object_id, proposal in proposals.items())
    inserted = review.get("insertions", {})
    matched = matched and all(ledger.get("__insertions__", {}).get(object_id) == entry
                              for object_id, entry in inserted.items())
    model["agentReview"] = {key: review[key] for key in ("reviewDate", "scope", "evidence", "verifiedUnchanged", "notAutoCorrected", "calibrationPolicy") if key in review}
    model["agentReview"].update(status="bounded-review-record", sourceIdentityVerified=True,
        sourceSha256=expected_hash, reviewFileSha256=file_hash(path), currentBuildRevision=model["revision"],
        reviewedProposalValuesMatchLedger=matched)


def review_packet(model, source, input_reference):
    """Export source measure snippets and candidates; never accept agent proposals."""
    root, _ = _read_source(Path(source))
    snippets = {}
    for part in root.findall("part"):
        for measure in part.findall("measure"):
            text = ET.tostring(measure, encoding="unicode")
            snippets.setdefault(measure.get("number"), []).append({"sourcePart": part.get("id"),
                "sourceXml": text[:12000], "truncated": len(text) > 12000})
    packets = []
    for issue in model["audit"]["issues"]:
        selected = [n for n in model["notes"] + model["rests"] if n["measure"] == issue["measure"]]
        packets.append({"issue": issue, "sourceMeasures": snippets.get(str(issue["measure"]), []),
            "symbols": [{key: n[key] for key in ("id", "sourcePart", "staff", "voice", "kind", "pitch", "offset", "duration", "source") if key in n}
                        for n in selected]})
    return {"schemaVersion": 1, "revision": model["revision"], "source": input_reference,
            "status": "proposals-unaccepted", "policy": [
                "Use the original score image and explicit meter/clef/rhythm evidence.",
                "Harmony, neighboring voices and repeated phrases may suggest candidates; they do not establish the original notes.",
                "Each proposal must specify objectId, before, after, field, reason and a source page/region or source XML evidence.",
                "Apply accepted proposals only through a separate correction ledger and rebuild all downstream artifacts."],
            "proposalExample": {"objectId": "n-PART-m1-o1", "field": "pitch", "before": 60, "after": 62,
                "reason": "Describe the visible evidence", "evidence": {"sourceFile": "original.pdf", "page": 1, "region": []},
                "status": "unaccepted"}, "issues": packets}


def plan(config):
    kind = "pdf" if config["input"].get("pdf") else "musicxml"
    return {"schemaVersion": 1, "status": "plan", "config": config,
            "stages": [{"name": name, "operation": operation} for name, operation in zip(STAGES, (
                "Run Audiveris and explicitly choose one exported score" if kind == "pdf" else "Snapshot MusicXML, optional OMR and correction ledger",
                "Build canonical symbols and conservative audit; keep unresolved issues",
                "Normalize rendering only and require complete note-ID coverage",
                "Render canonical MIDI/WAV/MP3 with duration and clipping checks",
                "Export model, review packet and artifact hashes; leave the existing workbench untouched"))],
            "limits": {"tempoFallbackQuarterBpm": 120, "playbackRoute": "written-order",
            "scoreApproval": "Artifact checks cannot certify recognition or agent proposals",
                "browserDeployment": "This command exports an asset bundle; it does not switch the running workbench"}}


def build(config, output=None):
    """Build a new bundle. Refuse existing output paths, including app asset paths."""
    from runtime import data_dir
    output = Path(output or config.get("output") or data_dir() / "projects" /
                  datetime.now(timezone.utc).strftime("score-%Y%m%dT%H%M%S%fZ")).resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("Build outputs must stay outside the installed plugin")
    if output.exists():
        raise ValueError(f"Output already exists; choose a new directory: {output}")
    input_kind = "pdf" if config["input"].get("pdf") else "musicxml"
    original = Path(config["input"][input_kind])
    original_hash = file_hash(original)
    output.mkdir(parents=True)
    assets = output / "assets"
    assets.mkdir()
    inputs = output / "inputs"
    inputs.mkdir()
    manifest = {"schemaVersion": 1, "status": "running", "createdAt": datetime.now(timezone.utc).isoformat(),
                "output": str(output), "config": config, "stages": [], "artifacts": [], "input": {
                    "kind": input_kind, "originalPath": str(original), "sha256": original_hash}}
    manifest_path = output / "manifest.json"
    write_json(manifest_path, manifest)
    stage = None

    def begin(name):
        nonlocal stage
        stage = {"name": name, "status": "running", "startedAt": datetime.now(timezone.utc).isoformat()}
        manifest["stages"].append(stage)
        write_json(manifest_path, manifest)
        return time.perf_counter()

    def finish(started, **details):
        stage.update(status="complete", elapsedSeconds=round(time.perf_counter() - started, 6), **details)
        write_json(manifest_path, manifest)

    try:
        started = begin("input")
        snapshot = inputs / original.name
        shutil.copy2(original, snapshot)
        source_pdf = config.get("sourcePdf") or (str(original) if input_kind == "pdf" else None)
        if source_pdf:
            source_pdf = Path(source_pdf)
            pdf_hash = file_hash(source_pdf)
            if input_kind == "pdf" and source_pdf.resolve() == original.resolve():
                pdf_snapshot = snapshot
            else:
                pdf_snapshot = inputs / "source.pdf"
                if pdf_snapshot == snapshot:
                    pdf_snapshot = inputs / "source-view.pdf"
                shutil.copy2(source_pdf, pdf_snapshot)
            if file_hash(pdf_snapshot) != pdf_hash:
                raise ValueError("Original PDF changed while its snapshot was copied")
            manifest["sourcePdf"] = {"path": pdf_snapshot.relative_to(output).as_posix(), "sha256": pdf_hash}
        corrections = read_corrections(config.get("corrections"))
        tempo_context = load_evidence(config["tempoEvidence"]) if config.get("tempoEvidence") else None
        write_json(inputs / "corrections.json", corrections)
        if tempo_context is not None:
            write_json(inputs / "tempo-evidence.json", tempo_context)
        write_json(output / "resolved-config.json", config)
        source, omr = snapshot, None
        if input_kind == "pdf":
            from omr.audiveris_runner import run_omr
            log = io.StringIO()
            with redirect_stdout(log):
                exports = [Path(p) for p in run_omr(str(snapshot), str(output))]
            (output / "omr-stage.log").write_text(log.getvalue(), encoding="utf-8")
            index = config["input"].get("scoreIndex")
            if index is None and len(exports) != 1:
                raise ValueError(f"Audiveris exported {len(exports)} scores. Set input.scoreIndex explicitly; see omr_out.")
            index = 0 if index is None else index
            if index >= len(exports):
                raise ValueError(f"input.scoreIndex {index} exceeds {len(exports)} exported scores")
            source = exports[index]
            omr = source.with_suffix(".omr") if source.with_suffix(".omr").exists() else None
            manifest["input"]["exports"] = [str(p.relative_to(output)) for p in exports]
        elif config["input"].get("omr"):
            omr = inputs / "source.omr"
            shutil.copy2(config["input"]["omr"], omr)
        manifest["input"].update(musicxml=str(source.relative_to(output)), musicxmlFileSha256=file_hash(source),
                                 omr=str(omr.relative_to(output)) if omr else None,
                                 correctionsSha256=file_hash(inputs / "corrections.json"))
        finish(started, inputKind=input_kind)

        started = begin("canonical-audit")
        model = prepare_model(config, source, omr, corrections, tempo_context)
        assignments = {part["sourcePart"]: part["part"] for part in model["parts"]}
        manifest["revision"] = model["revision"]
        manifest["partAssignments"] = assignments
        if config.get("reviewFile"):
            attach_review(model, config["reviewFile"], corrections)
            shutil.copy2(config["reviewFile"], inputs / "agent-review.json")
            manifest["reviewRecord"] = model["agentReview"]
        write_canonical(model, assets / "canonical.musicxml", merge_parts=config["mergeLogicalParts"])
        write_json(assets / "audit.json", model["audit"])
        write_json(assets / "rhythm-validation.json", model["rhythmValidation"])
        from workbench.calibration import plan_calibration
        calibration_plan = plan_calibration(model)
        write_json(output / "calibration-plan.json", calibration_plan)
        manifest["calibration"] = {"status": "proposals-unaccepted", "path": "calibration-plan.json",
            "summary": calibration_plan["summary"],
            "nextStep": "Run workbench.calibrate to prepare original-PDF evidence and apply hash-bound review decisions"}
        model["audioValidation"] = require_rhythm(model, config.get("rhythmPolicy", "strict"))
        finish(started, summary=model["audit"]["summary"], sourceXmlSha256=model["provenance"]["sourceSha256"], tempoMetadata=model["tempoMetadata"])

        started = begin("engraving")
        from workbench.engrave import engrave
        pages, ids = engrave(assets / "canonical.musicxml", assets)
        expected = {n["id"] for n in model["notes"]}
        missing = expected - set(ids)
        model["notationCoverage"] = {"notes": len(expected), "rendered": len(expected & set(ids)), "missing": sorted(missing)}
        if missing:
            raise ValueError(f"Engraving lost {len(missing)} note IDs; bundle not validated")
        for note in model["notes"]:
            note["svgPage"] = ids[note["id"]]
        model["pagesInteractive"] = pages
        finish(started, coverage=model["notationCoverage"], pages=len(pages))

        started = begin("audio")
        import mido
        from workbench.playback import fit_audio_duration, make_midi
        from synth.render import midi_to_wav, wav_to_mp3
        midi_path, wav_path, mp3_path = (assets / name for name in ("base.mid", "base.wav", "base.mp3"))
        rendering = make_midi(model, midi_path, config["playback"])
        model["audioRendering"] = rendering
        midi_to_wav(str(midi_path), config["soundfont"], str(wav_path))
        fit_audio_duration(wav_path, model["duration"])
        wav_to_mp3(str(wav_path), str(mp3_path))
        with wave.open(str(wav_path), "rb") as audio:
            if audio.getsampwidth() != 2:
                raise ValueError("Validation requires PCM16 WAV")
            frames, sample_rate = audio.getnframes(), audio.getframerate()
            samples = array("h", audio.readframes(frames))
        if sys.byteorder != "little":
            samples.byteswap()
        peak = max((abs(v) for v in samples), default=0)
        clipped = sum(abs(v) >= 32767 for v in samples)
        midi_duration = mido.MidiFile(str(midi_path)).length
        timing = {"canonicalSeconds": model["duration"], "midiSeconds": midi_duration,
                  "wavSeconds": frames / sample_rate, "sampleRate": sample_rate,
                  "peak": peak / 32768, "clippedSamples": clipped}
        if abs(midi_duration - model["duration"]) > 0.001 or abs(frames / sample_rate - model["duration"]) > 1.1 / sample_rate:
            raise ValueError("Canonical, MIDI and WAV duration checks failed")
        if clipped:
            raise ValueError("Synthesized WAV clipped; reduce configured part gains")
        finish(started, timing=timing, soundfontSha256=file_hash(config["soundfont"]))

        started = begin("package")
        model["audio"] = f"/assets/base.mp3?v={model['revision']}"
        model["playbackSettings"] = config["playback"]
        write_json(assets / "interactive.json", public_model(model))
        packet = review_packet(model, source, manifest["input"])
        packet["calibration"] = calibration_plan
        write_json(output / "review-packet.json", packet)
        issues = model["audit"]["issues"]
        validation = {"artifactStatus": "verified", "scoreStatus": "needs-review" if issues else "unreviewed",
            "rhythmValidation": model["rhythmValidation"], "audioValidation": model["audioValidation"], "audioRendering": rendering,
            "openIssues": len(issues), "bySeverity": model["audit"]["summary"]["bySeverity"],
            "unknownMeterMeasures": [m["number"] for m in model["measures"] if m["timeSignature"] is None],
            "missingSourceMeasures": [i["evidence"] for i in issues if i["kind"] == "missing-source-measure"],
            "playbackRoute": model["provenance"]["playbackRoute"], "tempoFallbackQuarterBpm": model["provenance"]["tempoFallback"],
            "tempoMetadata": model["tempoMetadata"],
            "notationCoverage": model["notationCoverage"], "audio": timing,
            "originalInputUnchanged": file_hash(original) == original_hash,
            "pitchContract": {"referenceA4Hz": config["playback"]["a4"], "transposeSemitones": config["playback"]["transpose"],
                "frequency": "a4 * 2 ** ((midi + transpose - 69) / 12)",
                "deviationCents": "1200 * log2(sungFundamentalHz / targetHz)",
                "mode": "Browser-local single-singer feedback; singing does not alter saved score corrections"}}
        if not validation["originalInputUnchanged"]:
            raise ValueError("Original input changed during build")
        write_json(assets / "validation.json", validation)
        manifest["validation"] = validation
        finish(started)
        manifest["status"] = "complete-with-open-issues" if issues else "complete"
        manifest["completedAt"] = datetime.now(timezone.utc).isoformat()
        manifest["artifacts"] = [{"path": str(p.relative_to(output)), "bytes": p.stat().st_size, "sha256": file_hash(p)}
                                 for p in sorted(output.rglob("*")) if p.is_file() and p != manifest_path]
        write_json(manifest_path, manifest)
        return manifest
    except Exception as error:
        if stage:
            stage.update(status="failed", error=str(error))
        manifest.update(status="failed", error=str(error))
        write_json(manifest_path, manifest)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build a reviewed-score workflow bundle without replacing the current app")
    parser.add_argument("--config", help="Project JSON; relative paths resolve beside this file")
    parser.add_argument("--musicxml", help="MusicXML/MXL source instead of config input")
    parser.add_argument("--pdf", help="PDF source instead of config input; invokes local Audiveris")
    parser.add_argument("--output", help="A new output directory")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--plan", action="store_true", help="Validate configuration and print stage contracts without writing outputs")
    modes.add_argument("--build", action="store_true", help="Execute stages and produce a reproducibility manifest")
    modes.add_argument("--calibrate", action="store_true", help="Scan every measure and prepare source evidence and correction candidates")
    args = parser.parse_args(argv)
    if args.musicxml and args.pdf:
        parser.error("Choose --musicxml or --pdf")
    overrides = {}
    if args.musicxml or args.pdf:
        overrides["input"] = {"musicxml" if args.musicxml else "pdf": str(Path(args.musicxml or args.pdf).resolve())}
    if args.output:
        overrides["output"] = str(Path(args.output).resolve())
    try:
        config = load_config(args.config, overrides)
        if args.calibrate:
            from workbench.calibrate import scan
            result = scan(config, args.output)
            result.pop("candidates", None)
        else:
            result = plan(config) if args.plan else build(config)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, ET.ParseError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
