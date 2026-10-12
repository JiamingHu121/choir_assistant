"""Reusable, bounded OMR calibration proposals; never meter-force-fit a score.

The model's musical checks provide contradictions, not ground truth. This
module scans every original symbol, clusters related findings by musical voice,
and enumerates only alternatives supported by encoded notation or a repeated
reviewed pattern. OMR glyph grades and unique meter solutions are deliberately
insufficient for automatic acceptance. A source-verified, hash-bound evidence
record is the only way a musical edit becomes ``auto-eligible``.

``plan_calibration(model, evidence=None)`` is pure and JSON-ready. Evidence is
``{note_id: {sourceSha256, status: 'source-verified', duration?, tuplets?,
tieTypes?, slurs?, articulations?, reference, reason}}``. The calling workflow
must still rebuild and reject any newly introduced structural contradiction.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction
from hashlib import sha256
import itertools
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from workbench.music_semantics import BEAM_LEVELS, fraction, rational

POLICY_VERSION = "calibration-proposals-v1"
TYPE_BEATS = {"maxima": Fraction(32), "long": Fraction(16), "breve": Fraction(8),
              "whole": Fraction(4), "half": Fraction(2), "quarter": Fraction(1),
              "eighth": Fraction(1, 2), "16th": Fraction(1, 4),
              "32nd": Fraction(1, 8), "64th": Fraction(1, 16),
              "128th": Fraction(1, 32), "256th": Fraction(1, 64),
              "512th": Fraction(1, 128), "1024th": Fraction(1, 256)}
REST_TYPES = {"WHOLE_REST": "whole", "HALF_REST": "half", "QUARTER_REST": "quarter",
              "EIGHTH_REST": "eighth", "ONE_16TH_REST": "16th",
              "ONE_32ND_REST": "32nd", "ONE_64TH_REST": "64th"}
RULES = {
    "beam-type-duration": "Ordinary connected beam levels contradict encoded note type.",
    "omr-glyph-duration": "Independently retained OMR glyph category contradicts notation.",
    "omr-stem-duration": "Preserved OMR beam/flag/stem graph contradicts encoded duration, including internally consistent XML.",
    "notation-duration": "Written type/dots/ratio and encoded duration disagree; expressive timing may be legal.",
    "tuplet-without-ratio": "Tuplet marks without a time-modification ratio require source review.",
    "tuplet-ratio-removal": "Anomalous voice carries a coupled unscaled-note hypothesis; valid tuplets remain a competing explanation.",
    "compound-duplet": "Two ordinary beamed eighths in an anomalous compound-meter stream may be an omitted printed duplet.",
    "compound-quarter-three": "Three quarter attacks under a printed 3 in compound meter may span two dotted-quarter beats.",
    "verified-pattern": "A topology-identical, reviewed rhythm gives a transferable hypothesis, not a replacement rule.",
    "source-verified": "Original-score evidence explicitly fixes a symbol field and is bound to the source hash.",
    "structural-review": "Structural anomaly requires original-score review; no guessed edit is supplied.",
}
EVIDENCE_FIELDS = frozenset({"duration", "pitch", "notation", "tuplets", "tieTypes", "slurs", "articulations"})


def read_omr_features(model, omr_path=None):
    """Read saved OMR geometry without running recognition or rewriting inputs.

    Only already count/kind-checked stable-ID joins are used. Glyphs are another
    recognition layer, not a visual verification; their disagreement is useful
    even when MusicXML type, dots and duration all repeat the same mistake.
    """
    path = Path(omr_path or model.get("provenance", {}).get("omr") or "")
    if not path.is_file():
        return {}
    omr_hash = sha256(path.read_bytes()).hexdigest()
    wanted = defaultdict(list)
    for symbol in model.get("notes", []) + model.get("rests", []):
        omr = symbol.get("source", {}).get("omr") or {}
        if omr.get("method") == "checked-part-voice-chord-order":
            wanted[(omr["page"], omr["system"], str(omr["headId"]))].append(symbol["id"])
    result = {}
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if re.fullmatch(r"sheet#\d+/sheet#\d+\.xml", name)]
        for name in names:
            page = int(name.split("#")[1].split("/")[0])
            sheet = ET.fromstring(archive.read(name))
            for system in sheet.findall(".//system"):
                system_id = int(system.get("id"))
                inters = {node.get("id"): node for node in system.findall("sig/inters/*")}
                stems, beams, flags, dots = defaultdict(set), defaultdict(set), defaultdict(set), defaultdict(set)
                for relation in system.findall("sig/relations/relation"):
                    if not len(relation):
                        continue
                    a, b = relation.get("source"), relation.get("target")
                    if a not in inters or b not in inters:
                        continue
                    kind = relation[0].tag
                    if kind == "head-stem":
                        head, stem = (a, b) if inters[a].tag == "head" else (b, a)
                        stems[head].add(stem)
                    elif kind in {"beam-stem", "flag-stem"}:
                        inter, stem = (a, b) if inters[a].tag != "stem" else (b, a)
                        (beams if kind == "beam-stem" else flags)[stem].add(inter)
                    elif kind == "augmentation":
                        dot, head = (a, b) if inters[a].tag == "augmentation-dot" else (b, a)
                        if inters[dot].tag == "augmentation-dot":
                            dots[head].add(dot)
                for head_id, node in inters.items():
                    ids = wanted.get((page, system_id, head_id), [])
                    if not ids:
                        continue
                    stem_ids = stems[head_id]
                    beam_ids = set().union(*(beams[stem] for stem in stem_ids)) if stem_ids else set()
                    flag_ids = set().union(*(flags[stem] for stem in stem_ids)) if stem_ids else set()
                    flag_levels = []
                    for flag_id in flag_ids:
                        match = re.search(r"FLAG_(\d+)", inters[flag_id].get("shape", ""))
                        if match:
                            flag_levels.append(int(match.group(1)))
                    # Beam hooks count a level too; feathered beams cannot prove a duration.
                    standard = all(inters[beam].get("shape") in {"BEAM", "BEAM_HOOK"} for beam in beam_ids)
                    level = max(len(beam_ids) if standard else 0, max(flag_levels, default=0))
                    record = {"method": "preserved-omr-relation-graph", "page": page, "system": system_id,
                              "headId": head_id, "shape": node.get("shape"), "stemIds": sorted(stem_ids),
                              "beamIds": sorted(beam_ids), "flagIds": sorted(flag_ids),
                              "ordinaryFlagLevel": level if len(stem_ids) <= 1 else None,
                              "dotIds": sorted(dots[head_id]), "dotCount": len(dots[head_id]),
                              "ambiguousStem": len(stem_ids) > 1,
                              "sourceSha256": model.get("provenance", {}).get("sourceSha256"),
                              "omrSha256": omr_hash, "omrPath": str(path.resolve()),
                              "isVerifiedImage": False}
                    for object_id in ids:
                        result[object_id] = deepcopy(record)
    return result


def _pos(symbol, field):
    return fraction(symbol.get("rational", {}).get(field, symbol.get(field, 0)))


def _notated(notation, type_name=None, dots=None, use_ratio=True):
    base = TYPE_BEATS.get(type_name or notation.get("type"))
    if base is None:
        return None
    dots = notation.get("dots", 0) if dots is None else dots
    if not isinstance(dots, int) or not 0 <= dots <= 3:
        return None
    result = base * (2 - Fraction(1, 2 ** dots))
    mod = notation.get("timeModification")
    if use_ratio and mod:
        actual, normal = mod.get("actualNotes", 0), mod.get("normalNotes", 0)
        if not isinstance(actual, int) or not isinstance(normal, int) or actual <= 0 or normal <= 0:
            return None
        result *= Fraction(normal, actual)
    return result


def _standard_beams(symbol):
    return [beam for beam in symbol.get("notation", {}).get("beams", [])
            if beam.get("attributes", {}).get("fan", "none") == "none"
            and beam.get("value") in {"begin", "continue", "end", "forward hook", "backward hook"}
            and isinstance(beam.get("number"), int) and beam["number"] > 0]


def _topology(symbols):
    """Transposition-invariant motif key; excludes duration, not source symbols."""
    pitches = [symbol.get("pitch") for symbol in symbols if symbol["kind"] == "note"]
    origin = next((pitch for pitch in pitches if pitch is not None), 0)
    return tuple((symbol["kind"], None if symbol.get("pitch") is None else symbol["pitch"] - origin,
                  bool(symbol.get("chordTone")), tuple(sorted(beam["number"] for beam in _standard_beams(symbol))),
                  (symbol.get("source", {}).get("omr") or {}).get("shape"),
                  bool(symbol.get("notation", {}).get("timeModification"))) for symbol in symbols)


def _field(symbol, field):
    return symbol.get("notation", {}).get(field) if field in {"tuplets", "articulations"} else symbol.get(field)


def _matches_reviewed(current, desired):
    """Reviewed contracts name source facts; public objects contain extra data."""
    if isinstance(desired, dict):
        return isinstance(current, dict) and all(key in current and _matches_reviewed(current[key], value)
                                                 for key, value in desired.items())
    if isinstance(desired, list):
        return isinstance(current, list) and len(current) == len(desired) and all(
            _matches_reviewed(left, right) for left, right in zip(current, desired))
    return current == desired


def _candidate(rule, symbols, edits, reason, evidence, status="proposed", confidence="hypothesis"):
    identity = {"ruleId": rule, "objectIds": [symbol["id"] for symbol in symbols], "edits": edits}
    return {"id": "cal-" + sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16],
            "ruleId": rule, "status": status, "confidence": confidence,
            "measureIndices": sorted({symbol["measureIndex"] for symbol in symbols}),
            "sourcePart": symbols[0]["sourcePart"] if symbols else None,
            "voice": symbols[0].get("voice") if symbols else None,
            "objectIds": [symbol["id"] for symbol in symbols], "edits": edits,
            "reason": reason, "evidence": evidence,
            "acceptanceRequires": ["source-verified-evidence", "rebuild-without-new-structural-errors"]
            if status == "proposed" else ["rebuild-without-new-structural-errors"]}


def _edit(symbol, field, value):
    return {"objectId": symbol["id"], "field": field,
            "before": deepcopy(_field(symbol, field)), "after": deepcopy(value)}


def _linear_projection(symbols, measure, forward_ids):
    """Project sequential notation only when actual source topology permits it.

    A backup, legal sparse entry, explicit forward, unequal staff meter, or
    nonchord overlap must never be interpreted as missing/extra duration.
    """
    if measure.get("implicit"):
        return None, "implicit-or-pickup-measure"
    if forward_ids:
        return None, "explicit-forward-preserved"
    staffs = {symbol.get("staff", 1) for symbol in symbols}
    meters = [measure.get("staffMeters", {}).get(f"{symbols[0]['sourcePart']}/staff{staff}") for staff in staffs]
    if not meters or not all(meters):
        return None, "missing-staff-meter"
    lengths = {fraction(meter.get("beatsRational", meter["beats"])) for meter in meters}
    if len(lengths) != 1:
        return None, "independent-staff-meters"
    groups, cursor = [], Fraction(0)
    for symbol in symbols:
        if symbol.get("chordTone"):
            if not groups or symbol.get("chordId") != groups[-1][0].get("chordId"):
                return None, "orphan-or-disordered-chord"
            groups[-1].append(symbol)
        else:
            if _pos(symbol, "offset") != cursor:
                return None, "sparse-gap-or-overlapping-voice"
            groups.append([symbol])
            cursor += _pos(symbol, "duration")
    return (groups, next(iter(lengths))), None


def _enumerate_hypotheses(symbols, measure, proposals, forward_ids, limit):
    projection, blocker = _linear_projection(symbols, measure, forward_ids)
    if blocker:
        return {"status": "not-enumerated", "reason": blocker, "solutions": [], "truncated": False}
    chord_groups, expected = projection
    symbol_ids = {symbol["id"] for symbol in symbols}
    # A tuplet/pattern proposal is atomic. Enumerating independent per-note
    # changes would invent partial tuplet removal that no rule proposed.
    variables = [proposal for proposal in proposals if any(edit["field"] == "duration" for edit in proposal["edits"])
                 and all(edit["objectId"] in symbol_ids for edit in proposal["edits"])]
    total = 2 ** len(variables)
    solutions, searched = [], 0
    # Cap search separately from output to make combinatorial uncertainty visible.
    search_limit = max(256, limit * 64)
    seen = set()
    for combination in itertools.islice(itertools.product((False, True), repeat=len(variables)), search_limit):
        searched += 1
        selected = [proposal for proposal, enabled in zip(variables, combination) if enabled]
        combined, conflict = {}, False
        for proposal in selected:
            for edit in proposal["edits"]:
                key = (edit["objectId"], edit["field"])
                if key in combined and combined[key]["after"] != edit["after"]:
                    conflict = True
                combined[key] = edit
        if conflict:
            continue
        values = {object_id: fraction(edit["after"]) for (object_id, field), edit in combined.items() if field == "duration"}
        total_duration = sum((max(values.get(symbol["id"], _pos(symbol, "duration")) for symbol in group)
                              for group in chord_groups), Fraction(0))
        if total_duration != expected:
            continue
        edits = [deepcopy(edit) for _, edit in sorted(combined.items())]
        if not edits:
            continue
        signature = json.dumps(edits, sort_keys=True)
        if signature in seen:
            continue
        seen.add(signature)
        proposal_ids = sorted(proposal["id"] for proposal in selected)
        solutions.append({"edits": edits, "supportingCandidateIds": proposal_ids,
                          "quarterDuration": rational(total_duration), "status": "meter-compatible-hypothesis"})
        if len(solutions) >= limit:
            break
    return {"status": "enumerated", "targetDuration": rational(expected), "solutions": solutions,
            "searched": searched, "combinationCount": total,
            "truncated": searched < total, "uniquenessProvesSourceAccuracy": False}


def plan_calibration(model, evidence=None, max_hypotheses=64, omr_features=None):
    """Inspect all bars and return conservative, traceable calibration proposals."""
    if model.get("schemaVersion", 0) < 2:
        raise ValueError("Calibration requires the canonical schema-2 symbol model")
    if not isinstance(max_hypotheses, int) or not 1 <= max_hypotheses <= 256:
        raise ValueError("max_hypotheses must be an integer from 1 to 256")
    source_hash = model.get("provenance", {}).get("sourceSha256")
    if not source_hash:
        raise ValueError("Calibration requires original sourceSha256 provenance")
    evidence = evidence or {}
    omr_features = omr_features or {}
    all_symbols = model.get("notes", []) + model.get("rests", [])
    symbols = [symbol for symbol in all_symbols
               if not symbol.get("grace") and not symbol.get("placeholder") and not symbol.get("source", {}).get("generated")]
    by_id = {symbol["id"]: symbol for symbol in symbols}
    if len(by_id) != len(symbols):
        raise ValueError("Duplicate canonical symbol IDs")
    measures = {measure["index"]: measure for measure in model.get("measures", [])}
    lanes = defaultdict(list)
    for symbol in symbols:
        if symbol["measureIndex"] not in measures:
            raise ValueError("Symbol has no canonical measure")
        lanes[(symbol["measureIndex"], symbol["sourcePart"], symbol.get("voice", "1"))].append(symbol)
    for lane in lanes.values():
        lane.sort(key=lambda symbol: tuple(symbol.get("source", {}).get("order", [symbol.get("source", {}).get("noteOrdinal", 0), 0])))
    reviewed_ids = {record["objectId"] for record in model.get("audit", {}).get("corrections", [])
                    if not record.get("autoApplied") and record.get("field") == "duration"}
    candidates = []
    for symbol in symbols:
        current, duration = symbol.get("notation", {}), _pos(symbol, "duration")
        omr = symbol.get("source", {}).get("omr") or {}
        provenance = {"notation": deepcopy(current), "sourceNotation": deepcopy(symbol.get("source", {}).get("notation")),
                      "omr": deepcopy(omr) or None, "sourceSha256": source_hash,
                      "omrIsIndependentRecognitionNotVerifiedImage": bool(omr)}
        # Previously reviewed values cannot be overruled by the same imperfect OMR.
        if symbol["id"] not in reviewed_ids and not symbol.get("measureRest"):
            beams = _standard_beams(symbol)
            level = max((beam["number"] for beam in beams), default=0)
            beam_type = next((type_name for type_name, value in BEAM_LEVELS.items() if value == level), None)
            if beam_type and level > BEAM_LEVELS.get(current.get("type"), 0):
                target = _notated(current, type_name=beam_type)
                if target and target != duration:
                    candidates.append(_candidate("beam-type-duration", [symbol], [_edit(symbol, "duration", float(target))],
                                      "普通梁层数指向更短的音符类型；需要原图确认梁与附点，不按弧线长度或拍数缩放。", provenance))
            features = omr_features.get(symbol["id"], {})
            if features and features.get("sourceSha256") != source_hash:
                raise ValueError(f"OMR feature hash mismatch for {symbol['id']}")
            omr_level = features.get("ordinaryFlagLevel")
            graph_type = next((type_name for type_name, value in BEAM_LEVELS.items() if value == omr_level), None)
            if graph_type and not features.get("ambiguousStem") and features.get("shape") == "NOTEHEAD_BLACK":
                target = _notated(current, type_name=graph_type, dots=features.get("dotCount", current.get("dots", 0)))
                if target and target != duration:
                    candidates.append(_candidate("omr-stem-duration", [symbol], [_edit(symbol, "duration", float(target))],
                                      "保留的原始OMR梁/旗/附点图给出不同节奏；即使XML内部一致也列入原图校对，不自动采信OMR。",
                                      provenance | {"omrGraph": deepcopy(features)}))
            inferred_types = []
            shape = omr.get("shape")
            if symbol["kind"] == "rest" and shape in REST_TYPES:
                inferred_types = [REST_TYPES[shape]]
            elif shape == "WHOLE_NOTE":
                inferred_types = ["whole"]
            elif shape == "NOTEHEAD_VOID" and current.get("type") not in {"whole", "half", "breve", "long", "maxima"}:
                inferred_types = ["half"]
            elif shape == "NOTEHEAD_BLACK" and current.get("type") in {"whole", "half"}:
                # A filled head alone does not distinguish a quarter from a flagged eighth.
                inferred_types = [beam_type] if beam_type else ["quarter", "eighth"]
            for inferred in inferred_types:
                target = _notated(current, type_name=inferred)
                if target and target != duration:
                    candidates.append(_candidate("omr-glyph-duration", [symbol], [_edit(symbol, "duration", float(target))],
                                      "保留的OMR符号类别与记谱类型矛盾；受损墨迹、漏旗或误梁仍需原图判定。", provenance))
            written = _notated(current)
            if written and written != duration:
                candidates.append(_candidate("notation-duration", [symbol], [_edit(symbol, "duration", float(written))],
                                  "type、附点和连音比例与duration不一致；也可能是合法演奏时值编码，需源谱复核。", provenance))
        if current.get("tuplets") and not current.get("timeModification"):
            candidates.append(_candidate("tuplet-without-ratio", [symbol], [_edit(symbol, "tuplets", [])],
                              "图形连音记号没有时值比例；可为误识或遗漏比例，清除与补比例是互斥假设。", provenance | {
                                  "alternatives": ["remove-false-tuplet-marks", "restore-source-confirmed-time-modification"],
                                  "requires": "original printed numeral/bracket and full tuplet group"}))
        record = evidence.get(symbol["id"])
        if record:
            if record.get("sourceSha256") != source_hash:
                raise ValueError(f"Source evidence hash mismatch for {symbol['id']}")
            if record.get("status") != "source-verified" or not record.get("reference"):
                raise ValueError(f"Evidence must be source-verified with a reference for {symbol['id']}")
            edits = []
            for field in sorted(EVIDENCE_FIELDS & record.keys()):
                value = record[field]
                if field == "duration" and (isinstance(value, bool) or not 0 < fraction(value) <= 64):
                    raise ValueError(f"Invalid source-verified duration for {symbol['id']}")
                if field == "pitch" and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 127):
                    raise ValueError(f"Invalid source-verified pitch for {symbol['id']}")
                if not _matches_reviewed(_field(symbol, field), value):
                    edits.append(_edit(symbol, field, value))
            if edits:
                candidates.append(_candidate("source-verified", [symbol], edits,
                                  record.get("reason", "原谱逐符号确认，保留原对象ID。"), deepcopy(record),
                                  status="auto-eligible", confidence="source-verified"))
    unknown = set(evidence) - set(by_id)
    if unknown:
        raise ValueError(f"Source evidence references unavailable symbol IDs: {sorted(unknown)}")

    # False tuplets repeat both the incorrect numeric duration and the notation
    # ratio, so an internal equality check cannot discover the problem. Offer a
    # whole coupled group, never independent notes that would destroy a tuplet.
    issues = model.get("audit", {}).get("issues", [])
    timing_kinds = {"staff-underfill", "lane-overflow", "bar-overflow", "lane-gap", "notation-duration-mismatch"}
    for key, lane in lanes.items():
        index, part, voice = key
        anomalous = any(issue.get("kind") in timing_kinds
                        and index in (issue.get("blockingMeasureIndices", []) or issue.get("evidence", {}).get("measureIndices", []))
                        and issue.get("evidence", {}).get("sourcePart", part) == part
                        and issue.get("evidence", {}).get("voice", voice) == voice for issue in issues)
        if not anomalous or measures[index].get("implicit"):
            continue
        meters = [measures[index].get("staffMeters", {}).get(f"{part}/staff{symbol['staff']}") for symbol in lane]
        meter_counts = [re.fullmatch(r"(\d+)/8", meter.get("text", "")) if meter else None for meter in meters]
        compound = all(match and int(match.group(1)) >= 6 and int(match.group(1)) % 3 == 0 for match in meter_counts)
        if compound:
            for first, last in zip(lane, lane[1:]):
                group = [first, last]
                if any(symbol.get("chordTone") or symbol.get("id") in reviewed_ids or symbol["kind"] != "note"
                       or symbol["notation"].get("type") != "eighth" or symbol["notation"].get("dots") != 0
                       or symbol["notation"].get("timeModification") for symbol in group):
                    continue
                beam_values = [{beam["value"] for beam in _standard_beams(symbol) if beam["number"] == 1} for symbol in group]
                if beam_values != [{"begin"}, {"end"}] or _pos(first, "offset") + _pos(first, "duration") != _pos(last, "offset"):
                    continue
                edits = []
                for symbol, endpoint in ((first, "start"), (last, "stop")):
                    desired = {"type": "eighth", "dots": 0,
                               "timeModification": {"actualNotes": 2, "normalNotes": 3, "normalType": "eighth", "normalDots": 0}}
                    edits.extend([_edit(symbol, "duration", .75), _edit(symbol, "notation", desired),
                                  _edit(symbol, "tuplets", [{"type": endpoint, "number": "1", "showNumber": "actual", "actualNumber": 2}])])
                candidates.append(_candidate("compound-duplet", group, edits,
                                  "复拍子异常声部中的两八分连梁可能漏掉数字2；与漏休止/其他时值误识并列，不能仅为补满小节而采纳。",
                                  {"sourceSha256": source_hash, "beamTopology": "begin/end, two undotted eighth attacks",
                                   "localMeters": [deepcopy(meter) for meter in meters],
                                   "alternatives": ["printed-duplet-2", "ordinary-eighth-pair-and-another-missing-symbol"],
                                   "requires": "original printed 2 and its group endpoints"}))
        groups, group = [], []
        ratio = None
        for symbol in lane:
            mod = symbol.get("notation", {}).get("timeModification")
            current_ratio = (mod.get("actualNotes"), mod.get("normalNotes")) if mod else None
            if current_ratio != ratio or not mod:
                if group:
                    groups.append(group)
                group = []
            ratio = current_ratio
            if mod:
                group.append(symbol)
        if group:
            groups.append(group)
        for group in groups:
            if any(symbol["id"] in reviewed_ids or symbol.get("source", {}).get("calibration") for symbol in group):
                continue
            edits = []
            for symbol in group:
                current = symbol["notation"]
                target = _notated(current, use_ratio=False)
                if target and target != _pos(symbol, "duration"):
                    edits.append(_edit(symbol, "duration", float(target)))
                if current.get("tuplets"):
                    edits.append(_edit(symbol, "tuplets", []))
            if edits:
                candidates.append(_candidate("tuplet-ratio-removal", group, edits,
                                  "异常声部中的连音比例可能为误识；提供整组恢复普通记谱时值的候选，须确认原图数字/括号。",
                                  {"sourceSha256": source_hash,
                                   "timeModifications": [deepcopy(symbol["notation"]["timeModification"]) for symbol in group],
                                   "originalTuplets": [deepcopy(symbol["notation"].get("tuplets", [])) for symbol in group],
                                   "alternatives": ["valid-tuplet-with-other-missing-symbols", "false-tuplet-recognition"],
                                   "requires": "verify whole printed tuplet group and notehead/beam durations"}))
            if compound and len(group) == 3 and all(symbol["notation"].get("type") == "quarter"
                    and symbol["notation"].get("dots") == 0 and not symbol.get("chordTone")
                    and symbol["notation"]["timeModification"].get("actualNotes") == 3
                    and symbol["notation"]["timeModification"].get("normalNotes") == 2 for symbol in group):
                edits = []
                for ordinal, symbol in enumerate(group):
                    desired = {"type": "quarter", "dots": 0,
                               "timeModification": {"actualNotes": 3, "normalNotes": 3, "normalType": "quarter", "normalDots": 0}}
                    endpoints = ([{"type": "start" if ordinal == 0 else "stop", "number": "1", "showNumber": "actual", "actualNumber": 3}]
                                 if ordinal in (0, 2) else [])
                    edits.extend([_edit(symbol, "duration", 1), _edit(symbol, "notation", desired), _edit(symbol, "tuplets", endpoints)])
                candidates.append(_candidate("compound-quarter-three", group, edits,
                                  "复拍子中数字3可能标示三四分攻击跨两个附点四分拍；提供保留数字3且每音一四分单位的候选。",
                                  {"sourceSha256": source_hash, "quarterSpan": 3,
                                   "alternatives": ["compound-quarter-three", "simple-meter-three-in-two", "false-tuplet-marks"],
                                   "requires": "original printed 3, group span and local compound meter"}))

    motifs = defaultdict(list)
    for key, lane in lanes.items():
        if 2 <= len(lane) <= 32:
            motifs[_topology(lane)].append((key, lane))
    for matches in motifs.values():
        verified = [(key, lane) for key, lane in matches if any(symbol["id"] in reviewed_ids for symbol in lane)
                    and measures[key[0]].get("rhythmStatus") == "validated"]
        if not verified:
            continue
        patterns = {tuple(_pos(symbol, "duration") for symbol in lane) for _, lane in verified}
        if len(patterns) != 1:
            continue  # A repeated topology has legitimate alternate rhythms.
        durations = next(iter(patterns))
        for key, lane in matches:
            if any(symbol["id"] in reviewed_ids for symbol in lane):
                continue
            edits = [_edit(symbol, "duration", float(duration)) for symbol, duration in zip(lane, durations)
                     if _pos(symbol, "duration") != duration]
            if edits and measures[key[0]].get("rhythmStatus") != "validated":
                candidates.append(_candidate("verified-pattern", lane, edits,
                                  "相同符号/梁/音高轮廓的已校对片段给出重复节奏候选；不能仅凭重复相似性自动改谱。", {
                                      "templateObjectIds": [symbol["id"] for _, template in verified for symbol in template],
                                      "sourceSha256": source_hash, "patternBasis": "kind/chord/beam/glyph/transposed-pitch-contour",
                                      "allTemplateSymbolsVisuallyVerified": False}))

    # Every blocking/sparse scope enters the queue even when no safe edit exists.
    for issue in issues:
        if not (issue.get("rhythmBlocking") or issue.get("kind") == "sparse-voice-unverified"):
            continue
        ids = [object_id for object_id in issue.get("evidence", {}).get("noteIds", []) if object_id in by_id]
        affected = [by_id[object_id] for object_id in ids]
        item = _candidate("structural-review", affected, [], issue.get("message", "原谱结构复核"),
                          {"issueIds": [issue["id"]], "kind": issue["kind"],
                           "originalEvidence": deepcopy(issue.get("evidence", {})), "sourceSha256": source_hash},
                          confidence="anomaly-only")
        item["measureIndices"] = sorted(set(issue.get("blockingMeasureIndices", []) or
                                            issue.get("evidence", {}).get("measureIndices", []) or item["measureIndices"]))
        item["sourcePart"] = issue.get("evidence", {}).get("sourcePart") or (item["sourcePart"] if len({symbol["sourcePart"] for symbol in affected}) == 1 else None)
        item["voice"] = issue.get("evidence", {}).get("voice") or (item["voice"] if len({symbol["voice"] for symbol in affected}) == 1 else None)
        # Issue identity distinguishes different empty-source relation/meter scopes.
        item["id"] = "cal-" + sha256((item["id"] + issue["id"]).encode()).hexdigest()[:16]
        candidates.append(item)
    candidates = list({candidate["id"]: candidate for candidate in candidates}.values())
    groups = []
    forwards = model.get("timelineSpacers", [])
    for key, lane in sorted(lanes.items()):
        index, part, voice = key
        relevant = [candidate for candidate in candidates if index in candidate["measureIndices"]
                    and candidate["sourcePart"] in {None, part} and candidate["voice"] in {None, voice}]
        forward_ids = [spacer["id"] for spacer in forwards if
                       (spacer["measureIndex"], spacer["sourcePart"], spacer["voice"]) == key]
        coverage = next((item for item in measures[index].get("voiceCoverage", [])
                         if item["sourcePart"] == part and item["voice"] == voice), None)
        groups.append({"id": f"scope-{index}-{part}-voice{voice}", "measureIndex": index,
                       "sourcePart": part, "voice": voice, "objectIds": [symbol["id"] for symbol in lane],
                       "coverage": deepcopy(coverage), "candidateIds": [candidate["id"] for candidate in relevant],
                       "hypotheses": _enumerate_hypotheses(lane, measures[index], relevant, forward_ids, max_hypotheses)})
    clusters = []
    for rule in RULES:
        members = [candidate for candidate in candidates if candidate["ruleId"] == rule]
        if members:
            clusters.append({"ruleId": rule, "count": len(members), "candidateIds": [candidate["id"] for candidate in members],
                             "measureIndices": sorted({index for candidate in members for index in candidate["measureIndices"]})})
    return {"schemaVersion": 1, "policyVersion": POLICY_VERSION, "sourceSha256": source_hash,
            "revision": model.get("revision", model.get("version")), "candidates": candidates,
            "groups": groups, "clusters": clusters, "rules": RULES,
            "summary": {"scannedMeasures": len(measures), "scannedSymbols": len(all_symbols), "activeOriginalSymbols": len(symbols), "scannedVoices": len(lanes),
                        "candidateCount": len(candidates), "editableCandidates": sum(bool(item["edits"]) for item in candidates),
                        "omrGraphSymbols": len(omr_features),
                        "autoEligible": sum(item["status"] == "auto-eligible" for item in candidates),
                        "byRule": dict(Counter(item["ruleId"] for item in candidates)),
                        "reviewMeasures": sorted({index for candidate in candidates for index in candidate["measureIndices"]}),
                        "meterCompatibleHypotheses": sum(len(group["hypotheses"]["solutions"]) for group in groups),
                        "claim": "Whole-score proposal coverage; source recognition accuracy is not certified by musical consistency."}}


analyze = plan_calibration
