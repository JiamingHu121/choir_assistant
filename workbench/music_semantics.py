"""Exact notation, relation and rhythm checks for the canonical score.

These checks locate contradictions; they do not guess pitches, delete symbols,
or stretch a voice to fit a meter.  A notation-quarter is deliberately not
treated as a common performed beat when independent staff meters disagree.
"""
from __future__ import annotations

from collections import defaultdict
from fractions import Fraction

POLICY_VERSION = "music-semantics-v2.4"
BLOCKING_ISSUE_KINDS = frozenset({
    "missing-source-measure", "missing-meter", "conflicting-meters",
    "unresolved-meter-mapping", "negative-backup", "ambiguous-backup",
    "requested-bar-shorter-than-notes", "bar-overflow", "lane-overflow",
    "staff-underfill", "lane-gap", "lane-overlap", "invalid-note-duration",
    "beam-type-contradiction", "invalid-time-modification",
    "notation-duration-mismatch", "tie-notation-conflict",
    "unmatched-tie-start", "unmatched-tie-stop", "noncontiguous-tie",
    "unmatched-slur-start", "unmatched-slur-stop", "overlapping-slur-start",
    "invalid-slur-order", "invalid-relation-type", "unresolved-metric-modulation",
    "invalid-forward-duration", "chord-tone-too-long", "orphan-chord-tone",
    "chord-voice-mismatch", "ambiguous-tie", "unsupported-pitch-transposition",
})
BEAM_LEVELS = {"eighth": 1, "16th": 2, "32nd": 3, "64th": 4,
               "128th": 5, "256th": 6, "512th": 7, "1024th": 8}


def fraction(value):
    if isinstance(value, dict):
        return Fraction(value["numerator"], value["denominator"])
    if isinstance(value, Fraction):
        return value
    return Fraction(str(value)).limit_denominator(10080)


def rational(value):
    value = fraction(value)
    return {"numerator": value.numerator, "denominator": value.denominator}


def position(event, name):
    return fraction(event.get("rational", {}).get(name, event[name]))


def notation(note):
    mod = note.find("time-modification")
    ratio = None
    if mod is not None:
        def integer(tag):
            try:
                return int(mod.findtext(tag, "0"))
            except ValueError:
                return 0
        actual, normal = integer("actual-notes"), integer("normal-notes")
        ratio = {"actualNotes": actual, "normalNotes": normal,
                 "normalType": mod.findtext("normal-type"),
                 "normalDots": len(mod.findall("normal-dot")),
                 "multiplier": rational(Fraction(normal, actual)) if actual > 0 and normal > 0 else None}
    def tuplet_mark(mark):
        result = {"type": mark.get("type"), "number": mark.get("number", "1"), "attributes": dict(mark.attrib)}
        for name, attr in (("showNumber", "show-number"), ("showType", "show-type"),
                           ("bracket", "bracket"), ("placement", "placement")):
            if mark.get(attr) is not None:
                result[name] = mark.get(attr)
        for name, path in (("actualNumber", "tuplet-actual/tuplet-number"), ("normalNumber", "tuplet-normal/tuplet-number")):
            if mark.findtext(path) is not None:
                text = mark.findtext(path)
                result[name] = int(text) if text.isdigit() else text
        return result
    return {"type": note.findtext("type"), "dots": len(note.findall("dot")),
            "timeModification": ratio,
            "beams": [{"number": int(beam.get("number", "1")), "value": (beam.text or "").strip(),
                       "attributes": dict(beam.attrib)} for beam in note.findall("beam")],
            "articulations": [{"type": mark.tag, "attributes": dict(mark.attrib), "text": mark.text}
                              for container in note.findall("notations/articulations") for mark in container],
            "tied": [{"type": mark.get("type"), "number": mark.get("number", "1"),
                      "attributes": dict(mark.attrib)} for mark in note.findall("notations/tied")],
            "slurs": [{"type": mark.get("type"), "number": mark.get("number", "1"),
                       "attributes": dict(mark.attrib)} for mark in note.findall("notations/slur")],
            "tuplets": [tuplet_mark(mark) for mark in note.findall("notations/tuplet")],
            "accidental": note.findtext("accidental"),
            "musicxmlPitch": ({node.tag: node.text for node in note.find("pitch")}
                              if note.find("pitch") is not None else None),
            "pitchBasis": "MusicXML pitch subtree; octave-clef display is not independently reinterpreted; instrument transpose requires explicit support"}


def validate_notation(event, issue):
    if event["grace"]:
        return
    current = event["notation"]
    evidence = {"noteIds": [event["id"]], "sourcePart": event["sourcePart"],
                "measureIndices": [event["measureIndex"]]}
    if position(event, "duration") <= 0:
        issue(event["measure"], "error", "invalid-note-duration", "非装饰音的时值必须为正。",
              evidence | {"duration": event["duration"]}, "核对原谱时值；不删除或猜补符号。")
    mod = current["timeModification"]
    if mod and mod["multiplier"] is None:
        issue(event["measure"], "error", "invalid-time-modification", "连音实际/正常音符数不是有效正整数。",
              evidence | {"timeModification": mod}, "核对连音组及其作用范围。")
    # Standard beams count flag levels, starting with an eighth-note beam.
    # Fan/feathered beams intentionally depict acceleration and need a separate
    # policy; they cannot independently prove a standard note-type error.
    normal = [beam for beam in current["beams"] if beam["attributes"].get("fan", "none") == "none"]
    if normal and max(beam["number"] for beam in normal) > BEAM_LEVELS.get(current["type"], 0):
        issue(event["measure"], "error", "beam-type-contradiction", "普通连梁层数与当前音符类型矛盾。",
              evidence | {"type": current["type"], "beams": normal},
              "根据原图确认漏梁、音符类型或误认连梁；不自动缩短时值。")
    sound_types = set(event["tieTypes"])
    written_types = {mark["type"] for mark in current["tied"] if mark["type"] in ("start", "stop")}
    if sound_types and written_types and sound_types != written_types:
        issue(event["measure"], "error", "tie-notation-conflict", "声音延音与图形延音的起止标记冲突。",
              evidence | {"soundTieTypes": sorted(sound_types), "writtenTieTypes": sorted(written_types)},
              "对照原图明确延音端点；不使用弧线跨度增加时值。")


def validate_chord_membership(event, preceding, issue):
    if not event["chordTone"]:
        return
    symbols = [symbol for symbol in (preceding, event) if symbol is not None]
    evidence = {"noteIds": [symbol["id"] for symbol in symbols], "measureIndices": [event["measureIndex"]],
                "sourcePart": event["sourcePart"]}
    if preceding is None or preceding["kind"] != "note" or preceding["grace"] != event["grace"]:
        issue(event["measure"], "error", "orphan-chord-tone", "和弦附加音缺少同小节的有效前置音符。",
              evidence, "核对chord标记及音符归属；不猜补锚点。")
        return
    if event["voice"] != preceding["voice"]:
        issue(event["measure"], "error", "chord-voice-mismatch", "和弦附加音与前置音符不是同一音乐声部。",
              evidence | {"voices": [preceding["voice"], event["voice"]]}, "允许跨谱表和弦，但须保持同一音乐声部。")
    if not event["grace"] and position(event, "duration") > position(preceding, "duration"):
        issue(event["measure"], "error", "chord-tone-too-long", "和弦附加音的时值长于前置音符。",
              evidence | {"precedingDuration": preceding["duration"], "chordToneDuration": event["duration"]},
              "根据原谱核对时值或chord归属；合法较短和弦音保留，不强行改时值。")


def _order(event):
    return (position(event, "onset"), event["measureIndex"],
            event["sourcePart"], tuple(event["source"].get("order", [event["source"]["noteOrdinal"], 0])))


def build_relations(notes, issue):
    """Resolve sound ties and notation slurs to stable symbol endpoints.

    No inferred physical-lane mapping is used across Audiveris part IDs.
    Slur numbers are scoped to a source part, allowing a written cross-staff
    slur without requiring adjacent times or matching pitches.
    """
    relations = {"ties": [], "slurs": []}
    pending_ties, pending_slurs = {}, {}
    ordered = sorted(notes, key=_order)

    def report(kind, start, end, message):
        symbols = [symbol for symbol in (start, end) if symbol is not None]
        return issue(symbols[-1]["measure"], "error", kind, message,
                     {"noteIds": [symbol["id"] for symbol in symbols],
                      "measureIndices": sorted({symbol["measureIndex"] for symbol in symbols}),
                      "sourcePart": symbols[-1]["sourcePart"]}, "核对原谱端点、音高、声部及相邻时值。")

    def tie_edge(start, end, status, item=None):
        symbols = [symbol for symbol in (start, end) if symbol is not None]
        edge = {"id": f"tie-{len(relations['ties']) + 1}", "from": start["id"] if start else None,
                "to": end["id"] if end else None, "noteIds": [symbol["id"] for symbol in symbols],
                "status": status, "sourcePart": symbols[0]["sourcePart"],
                "staff": symbols[0]["staff"], "voice": symbols[0]["voice"],
                "staffs": sorted({symbol["staff"] for symbol in symbols}),
                "pitch": symbols[0]["pitch"], "issueIds": [item["id"]] if item else []}
        relations["ties"].append(edge)
        return edge

    for note in ordered:
        if note["grace"] or position(note, "duration") <= 0 or note["pitch"] is None:
            continue
        key = (note["sourcePart"], note["voice"], note["pitch"])
        types = set(note["tieTypes"])
        candidates = pending_ties.get(key, [])
        if "stop" in types:
            contiguous = [previous for previous in candidates if position(previous, "onset") + position(previous, "duration") == position(note, "onset")]
            available = list(contiguous or candidates)
            if len(available) == 1:
                previous = available[0]
                contiguous = position(previous, "onset") + position(previous, "duration") == position(note, "onset")
                item = None if contiguous else report("noncontiguous-tie", previous, note,
                                                      "延音端点同音高但时间不相邻；不跨越未知间隙合并。")
                tie_edge(previous, note, "validated" if contiguous else "needs-review", item)
                candidates.remove(previous)
            elif len(available) > 1:
                item = issue(note["measure"], "error", "ambiguous-tie", "同声部同音高有多个候选延音起点，不能唯一确定终点。",
                             {"noteIds": [previous["id"] for previous in available] + [note["id"]],
                              "measureIndices": sorted({previous["measureIndex"] for previous in available} | {note["measureIndex"]}),
                              "sourcePart": note["sourcePart"], "voice": note["voice"]}, "对照原谱逐条确认；不按谱表或先后顺序猜配对。")
                for previous in available:
                    tie_edge(previous, note, "needs-review", item)
                    candidates.remove(previous)
            else:
                item = report("unmatched-tie-stop", None, note, "延音终点没有同源声部、同音高的起点。")
                tie_edge(None, note, "needs-review", item)
        else:
            expired = [previous for previous in candidates if position(previous, "onset") + position(previous, "duration") <= position(note, "onset")]
            for previous in expired:
                item = report("unmatched-tie-start", previous, None, "延音起点之后出现同音高新音，但没有相应终点。")
                tie_edge(previous, None, "needs-review", item)
                candidates.remove(previous)
        if "start" in types:
            candidates.append(note)
        pending_ties[key] = candidates
        invalid = types - {"start", "stop"}
        if invalid:
            report("invalid-relation-type", None, note, "声音延音标记含不支持的类型。")

    for candidates in pending_ties.values():
        for start in candidates:
            item = report("unmatched-tie-start", start, None, "延音起点直到曲末仍没有同音高终点。")
            tie_edge(start, None, "needs-review", item)

    def slur_edge(start, end, number, status, item=None):
        symbols = [symbol for symbol in (start, end) if symbol is not None]
        members = []
        if start and end:
            voices = {symbol["voice"] for symbol in symbols}
            members = [symbol["id"] for symbol in ordered if symbol["sourcePart"] == start["sourcePart"]
                       and symbol["voice"] in voices
                       and _order(start) <= _order(symbol) <= _order(end)]
        relations["slurs"].append({"id": f"slur-{len(relations['slurs']) + 1}",
                                  "from": start["id"] if start else None, "to": end["id"] if end else None,
                                  "noteIds": [symbol["id"] for symbol in symbols], "memberNoteIds": members,
                                  "sourcePart": symbols[0]["sourcePart"], "number": number,
                                  "status": status, "issueIds": [item["id"]] if item else []})

    for note in ordered:
        # Stop before start supports two consecutive phrases on one symbol.
        marks = sorted(note["slurs"], key=lambda mark: {"stop": 0, "continue": 1, "start": 2}.get(mark["type"], 3))
        for mark in marks:
            key = (note["sourcePart"], mark["number"])
            previous = pending_slurs.get(key)
            if mark["type"] == "stop":
                if previous:
                    item = None
                    if _order(note) <= _order(previous):
                        item = report("invalid-slur-order", previous, note, "连音线终点没有位于起点之后。")
                    slur_edge(previous, note, mark["number"], "needs-review" if item else "validated", item)
                    del pending_slurs[key]
                else:
                    item = report("unmatched-slur-stop", None, note, "连音线终点缺少同源声部与编号的起点。")
                    slur_edge(None, note, mark["number"], "needs-review", item)
            elif mark["type"] == "start":
                if previous:
                    item = report("overlapping-slur-start", previous, note, "同编号连音线未闭合时再次开始。")
                    slur_edge(previous, None, mark["number"], "needs-review", item)
                pending_slurs[key] = note
            elif mark["type"] == "continue":
                if previous is None:
                    report("unmatched-slur-stop", None, note, "连音线跨系统续标记缺少已知起点。")
            else:
                report("invalid-relation-type", None, note, "连音线标记含不支持的类型。")
    for (_, number), start in pending_slurs.items():
        item = report("unmatched-slur-start", start, None, "连音线起点直到曲末仍未闭合。")
        slur_edge(start, None, number, "needs-review", item)
    return relations


def performance_events(notes, relations, seconds):
    """One attack stream, exclusively using validated sound-tie edges."""
    events, by_note = [], {}
    incoming = {edge["to"]: edge["from"] for edge in relations["ties"] if edge["status"] == "validated"}
    for note in sorted(notes, key=_order):
        if note["grace"] or position(note, "duration") <= 0 or note["pitch"] is None:
            continue
        previous = by_note.get(incoming.get(note["id"]))
        if previous:
            previous["noteIds"].append(note["id"])
            duration = fraction(previous["rational"]["duration"]) + position(note, "duration")
            end = fraction(previous["rational"]["onset"]) + duration
            previous["duration"], previous["end"] = float(duration), seconds(end)
            previous["rational"].update(duration=rational(duration), end=rational(end))
            note["performanceId"], note["attack"] = previous["id"], False
            by_note[note["id"]] = previous
            continue
        onset, duration = position(note, "onset"), position(note, "duration")
        event = {key: note[key] for key in ("part", "sourcePart", "staff", "voice", "pitch", "pitchName")}
        event.update(id=f"p-{note['id']}", noteIds=[note["id"]], onset=float(onset), duration=float(duration),
                     start=seconds(onset), end=seconds(onset + duration),
                     rational={"onset": rational(onset), "duration": rational(duration), "end": rational(onset + duration)})
        events.append(event)
        by_note[note["id"]] = event
        note["performanceId"], note["attack"] = event["id"], True
    return events


def _merged_intervals(groups):
    merged = []
    for group in sorted(groups, key=lambda item: (item["start"], item["end"])):
        if merged and group["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], group["end"])
            merged[-1]["noteIds"].extend(group["noteIds"])
        else:
            merged.append({"start": group["start"], "end": group["end"], "noteIds": list(group["noteIds"])})
    return merged


def validate_coverage(events, measures, spacers, issue):
    """Validate musical voices; keep physical staff coverage as display data.

    A MusicXML voice can move between staves. Omitted leading/trailing spans
    in a secondary voice are review candidates, while a hole within a voice
    without an explicit forward is an actual encoding contradiction.
    """
    lanes, voices = defaultdict(list), defaultdict(list)
    for event in events:
        if not event["grace"] and position(event, "duration") > 0:
            lanes[(event["measureIndex"], event["sourcePart"], event["staff"], event["voice"])].append(event)
            if not event.get("placeholder"):
                voices[(event["measureIndex"], event["sourcePart"], event["voice"])].append(event)
    for spacer in spacers:
        if position(spacer, "duration") > 0:
            lanes[(spacer["measureIndex"], spacer["sourcePart"], spacer["staff"], spacer["voice"])].append(spacer)
            voices[(spacer["measureIndex"], spacer["sourcePart"], spacer["voice"])].append(spacer)

    def intervals_for(symbols):
        groups = {}
        for symbol in symbols:
            start = position(symbol, "offset")
            end = start + position(symbol, "duration")
            key = symbol.get("chordId", symbol["id"])
            if key in groups:
                groups[key]["end"] = max(groups[key]["end"], end)
                groups[key]["noteIds"].append(symbol["id"])
            else:
                groups[key] = {"start": start, "end": end, "noteIds": [symbol["id"]]}
        return sorted(groups.values(), key=lambda group: (group["start"], group["end"]))

    def public_intervals(groups):
        return [{"start": float(group["start"]), "end": float(group["end"]),
                 "rational": {"start": rational(group["start"]), "end": rational(group["end"])},
                 "noteIds": group["noteIds"]} for group in groups]

    def gaps_for(groups, expected):
        cursor, gaps = Fraction(0), []
        for group in groups:
            if group["start"] > cursor:
                gaps.append((cursor, min(group["start"], expected)))
            cursor = max(cursor, group["end"])
        if cursor < expected:
            gaps.append((cursor, expected))
        return [[float(start), float(end)] for start, end in gaps if start < expected and end > start]

    def complete(groups, expected):
        return expected is not None and len(groups) == 1 and groups[0]["start"] == 0 and groups[0]["end"] == expected

    placeholder_parts = set()
    for (index, part, staff, voice), symbols in sorted(lanes.items()):
        measure = measures[index - 1]
        meter = measure["staffMeters"].get(f"{part}/staff{staff}")
        expected = fraction(meter["beats"]) if meter else None
        merged = _merged_intervals(intervals_for(symbols))
        real = [symbol for symbol in symbols if symbol.get("kind") != "forward"]
        placeholder = bool(real) and all(symbol.get("placeholder") for symbol in real)
        data = {"sourcePart": part, "staff": staff, "voice": voice, "placeholder": placeholder,
                "expectedDuration": float(expected) if expected is not None else None,
                "expectedRational": rational(expected) if expected is not None else None,
                "coverageMode": "physical-staff-display; continuity checked by sourcePart+voice",
                "intervals": public_intervals(merged), "issueIds": [],
                "uncoveredIntervals": gaps_for(merged, expected) if expected is not None else [],
                "explicitForwardIds": [symbol["id"] for symbol in symbols if symbol.get("kind") == "forward"],
                "status": "placeholder" if placeholder else "complete" if complete(merged, expected) else "sparse"}
        measure.setdefault("laneCoverage", []).append(data)
        if placeholder:
            placeholder_parts.add((index, part))

    part_groups = defaultdict(list)
    for (index, part, voice), symbols in sorted(voices.items()):
        measure = measures[index - 1]
        staffs = sorted({symbol["staff"] for symbol in symbols})
        local_meters = [measure["staffMeters"].get(f"{part}/staff{staff}") for staff in staffs]
        lengths = {fraction(meter["beats"]) for meter in local_meters if meter}
        expected = next(iter(lengths)) if len(lengths) == 1 and all(local_meters) else None
        intervals = intervals_for(symbols)
        merged = _merged_intervals(intervals)
        data = {"sourcePart": part, "voice": voice, "staffs": staffs, "crossStaff": len(staffs) > 1,
                "expectedDuration": float(expected) if expected is not None else None,
                "expectedRational": rational(expected) if expected is not None else None,
                "coverageMode": "sourcePart+voice musical stream",
                "intervals": public_intervals(merged), "issueIds": [],
                "uncoveredIntervals": gaps_for(merged, expected) if expected is not None else [],
                "explicitForwardIds": [symbol["id"] for symbol in symbols if symbol.get("kind") == "forward"]}
        measure.setdefault("voiceCoverage", []).append(data)
        evidence = {"sourcePart": part, "staffs": staffs, "voice": voice, "measureIndices": [index]}
        longest_before = intervals[0] if intervals else None
        for after in intervals[1:]:
            before = longest_before
            if after["start"] < before["end"]:
                item = issue(measure["number"], "error", "lane-overlap", "同一音乐声部出现非和弦的重叠时值。",
                             evidence | {"noteIds": before["noteIds"] + after["noteIds"],
                                         "overlap": [float(after["start"]), float(min(before["end"], after["end"]))]},
                             "核对chord、backup、voice归属；不将同时和弦串行相加。")
                data["issueIds"].append(item["id"])
            if after["end"] > longest_before["end"]:
                longest_before = after
        for before, after in zip(merged, merged[1:]):
            if after["start"] > before["end"]:
                item = issue(measure["number"], "error", "lane-gap", "此声部的已编码跨度内存在未说明的时间缺口。",
                             evidence | {"noteIds": before["noteIds"] + after["noteIds"],
                                         "gap": [float(before["end"]), float(after["start"])]},
                             "核对缺失休止或显式forward；保留合法次声部前后留白。")
                data["issueIds"].append(item["id"])
        if expected is not None and not measure["implicit"]:
            overflowing = [group for group in intervals if group["start"] < 0 or group["end"] > expected]
            if overflowing:
                item = issue(measure["number"], "error", "lane-overflow", "此音乐声部符号超出其明确拍号的范围。",
                             evidence | {"expected": float(expected), "noteIds": [note_id for group in overflowing for note_id in group["noteIds"]]},
                             "逐音核对类型、附点、连音及起点；不改变拍号或缩放音符来合拍。")
                data["issueIds"].append(item["id"])
        data["status"] = "needs-review" if data["issueIds"] else "complete" if complete(merged, expected) else "sparse"
        part_groups[(index, part)].extend(intervals)
        if data["status"] == "sparse" and not measure["implicit"]:
            issue(measure["number"], "warning", "sparse-voice-unverified", "此音乐声部前后未覆盖整小节；合法稀疏编码不能证明源谱逐声部完整。",
                  evidence | {"noteIds": [symbol["id"] for symbol in symbols],
                              "uncoveredIntervals": data["uncoveredIntervals"], "explicitForwardIds": data["explicitForwardIds"]},
                  "原图核对是否为合法声部进出或漏音/休止；不据此猜补，也不一律阻断合法次声部。")
        for physical in measure["laneCoverage"]:
            if physical["sourcePart"] == part and physical["voice"] == voice and not physical["placeholder"]:
                physical["crossStaffVoice"] = len(staffs) > 1
                physical["issueIds"] = list(data["issueIds"])
                if data["issueIds"]:
                    physical["status"] = "needs-review"

    targets = set(part_groups)
    for measure in measures:
        for key in measure["staffMeters"]:
            part, _ = key.rsplit("/staff", 1)
            target = (measure["index"], part)
            if target not in placeholder_parts:
                targets.add(target)
    for index, part in sorted(targets):
        intervals = part_groups[(index, part)]
        measure = measures[index - 1]
        meters = [meter for key, meter in measure["staffMeters"].items() if key.rsplit("/staff", 1)[0] == part]
        lengths = {fraction(meter["beats"]) for meter in meters if meter}
        if len(lengths) != 1 or not all(meters) or measure["implicit"]:
            continue
        expected = next(iter(lengths))
        merged = _merged_intervals(intervals)
        gaps = gaps_for(merged, expected)
        if gaps:
            issue(measure["number"], "error", "staff-underfill", "此源部件所有音乐声部的时间并集未完整覆盖明确拍号。",
                  {"sourcePart": part, "coverageBasis": "source-part-voice-union", "measureIndices": [index], "expected": float(expected),
                   "gaps": gaps,
                   "noteIds": [note_id for group in intervals for note_id in group["noteIds"]]},
                  "核对漏识音符、休止或位移；不猜补、不自动拉长前音。")


def rhythm_validation(measures, issues):
    by_label = {str(measure["number"]): measure["index"] for measure in measures}
    blocking = [item for item in issues if item["kind"] in BLOCKING_ISSUE_KINDS]
    indices = set()
    for item in blocking:
        affected = set(item["evidence"].get("measureIndices", []))
        for number in item["evidence"].get("measures", []):
            if str(number) in by_label:
                affected.add(by_label[str(number)])
        if not affected and str(item["measure"]) in by_label:
            affected.add(by_label[str(item["measure"])])
        item["blockingMeasureIndices"] = sorted(affected)
        item["rhythmBlocking"] = True
        indices.update(affected)
    for measure in measures:
        measure["rhythmStatus"] = "needs-review" if measure["index"] in indices else "validated"
        measure["rhythmIssueIds"] = [item["id"] for item in blocking if measure["index"] in item["blockingMeasureIndices"]]
        measure["timelineStatus"] = "draft" if any(index <= measure["index"] for index in indices) else "validated"
    return {"status": "needs-review" if blocking else "validated", "policyVersion": POLICY_VERSION,
            "blockingIssueIds": [item["id"] for item in blocking],
            "blockingMeasureIndices": sorted(indices),
            "blockingMeasures": [measure["number"] for measure in measures if measure["index"] in indices],
            "timelinePolicy": "draft-preserve-unresolved-extents" if blocking else "validated-meter-and-relations",
            "coveragePolicy": "sourcePart+voice continuity; part-voice union; physical staff is display; legal sparse does not certify source completeness",
            "blockingKinds": sorted(BLOCKING_ISSUE_KINDS),
            "scope": "structural-rhythm-consistency; original-score fidelity requires visual review"}
