"""Tempo evidence, beat units and conservative text-only playback estimates.

MusicXML ``sound tempo`` always counts quarter notes. Numeric metronome
marks count their written note unit. A text term is not an author's numeric
tempo: the pinned music21 word-table value below is an editable playback
estimate, applied to our explicitly chosen meter pulse. Relative changes
without a numeric destination remain unresolved, rather than invented ramps.
"""
from __future__ import annotations

from fractions import Fraction
import json
import math
from pathlib import Path
import re

POLICY_VERSION = "tempo-evidence-v2"
FALLBACK_QUARTER_BPM = 120.0
REFERENCES = {
    "soundTempo": "https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/sound/",
    "metronome": "https://www.w3.org/2021/06/musicxml40/musicxml-reference/elements/metronome/",
    "textEstimate": "https://www.music21.org/music21docs/moduleReference/moduleTempo.html",
}
NOTE_QUARTERS = {"maxima": 32, "long": 16, "breve": 8, "whole": 4,
                 "half": 2, "quarter": 1, "eighth": .5, "16th": .25,
                 "32nd": .125, "64th": .0625}
# Pinned defaults from music21. They are conventions, not exact score readings.
TERM_BPM = {"larghissimo": 16, "largamente": 32, "grave": 40, "molto adagio": 40,
            "largo": 46, "lento": 52, "adagio": 56, "slow": 56, "langsam": 56,
            "larghetto": 60, "adagietto": 66, "andante": 72, "andantino": 80,
            "andante moderato": 83, "maestoso": 88, "moderato": 92, "moderate": 92,
            "allegretto": 108, "animato": 120, "allegro moderato": 128,
            "allegro": 132, "fast": 132, "schnell": 132, "allegrissimo": 140,
            "molto allegro": 144, "très vite": 144, "vivace": 160,
            "vivacissimo": 168, "presto": 184, "prestissimo": 208}
RELATIVE = re.compile(r"\b(?:a\s*tempo|tempo\s+primo|rit\w*|rall\w*|ril\w*|pi[uù]\s+moto|meno\s+mosso|acceler\w*)\b|[.=]\s*[=]", re.I)


def meter_pulse(meter):
    """Declare a playback pulse; compound */8 uses a dotted quarter."""
    text = (meter or {}).get("text", "")
    match = re.fullmatch(r"(\d+)/(\d+)", text)
    if not match:
        return {"beatUnit": "quarter", "beatQuarterLength": 1.0,
                "beatUnitSource": "default-quarter-pulse"}
    count, denominator = map(int, match.groups())
    if count <= 0 or denominator <= 0:
        return {"beatUnit": "quarter", "beatQuarterLength": 1.0,
                "beatUnitSource": "default-quarter-pulse"}
    compound = count > 3 and count % 3 == 0 and denominator in (8, 16)
    length = float(Fraction((3 if compound else 1) * 4, denominator))
    base_length = length / 1.5 if compound else length
    base = next((name for name, value in NOTE_QUARTERS.items() if value == base_length), "quarter")
    return {"beatUnit": "dotted-" + base if compound else base,
            "beatQuarterLength": length, "beatUnitSource": "meter-pulse-policy"}


def _number(text):
    if text is None or not re.fullmatch(r"\s*(?:\d+(?:\.\d*)?|\.\d+)\s*", str(text)):
        return None
    value = float(text)
    return value if math.isfinite(value) and 0 < value <= 2000 else None


def infer_term(text):
    normalized = re.sub(r"\s+", " ", text.casefold()).strip()
    for term in sorted(TERM_BPM, key=len, reverse=True):
        if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", normalized):
            return term, float(TERM_BPM[term])
    return None, None


def from_direction(direction, meter=None):
    """Return one tempo candidate or unresolved evidence from a direction."""
    text = " ".join((node.text or "").strip() for node in direction.findall("direction-type/words")).strip()
    metronome = direction.find("direction-type/metronome")
    numeric = None
    if metronome is not None:
        note_unit = metronome.findtext("beat-unit")
        dots = len(metronome.findall("beat-unit-dot"))
        value = _number(metronome.findtext("per-minute"))
        if note_unit in NOTE_QUARTERS and value is not None:
            length = NOTE_QUARTERS[note_unit] * (2 - 2 ** -dots)
            numeric = {"bpm": value * length, "beatBpm": value,
                       "beatQuarterLength": float(length),
                       "beatUnit": ("dotted-" if dots == 1 else f"{dots}-dots-" if dots else "") + note_unit,
                       "beatUnitSource": "written-metronome", "sourceKind": "metronome",
                       "sourceText": text or f"{note_unit}{'.' * dots} = {value:g}",
                       "inferred": False, "priority": 30}
    sound = direction.find("sound")
    sound_bpm = _number(sound.get("tempo")) if sound is not None else None
    if sound_bpm is not None:
        pulse = {key: numeric[key] for key in ("beatUnit", "beatQuarterLength", "beatUnitSource")} if numeric else meter_pulse(meter)
        result = {"bpm": sound_bpm, "beatBpm": sound_bpm / pulse["beatQuarterLength"],
                  **pulse, "sourceKind": "sound-tempo", "sourceText": text or (numeric or {}).get("sourceText") or f"quarter = {sound_bpm:g}",
                  "inferred": False, "priority": 40}
        if numeric:
            result["writtenMetronome"] = {key: value for key, value in numeric.items() if key != "priority"}
            if abs(numeric["bpm"] - sound_bpm) > .0001:
                result["numericConflict"] = [numeric["bpm"], sound_bpm]
        return result
    if numeric:
        return numeric
    if text:
        term, beat_bpm = infer_term(text)
        if beat_bpm is not None:
            pulse = meter_pulse(meter)
            return {"bpm": beat_bpm * pulse["beatQuarterLength"], "beatBpm": beat_bpm,
                    **pulse, "sourceKind": "tempo-text", "sourceText": text, "matchedTerm": term,
                    "inferred": True, "priority": 10,
                    "inferenceReason": "文字速度无数字；采用固定词表与明确标注的拍子脉冲作为可编辑试听值。"}
        if RELATIVE.search(text) or metronome is not None:
            return {"sourceKind": "relative-tempo-text", "sourceText": text,
                    "inferred": False, "unresolved": True, "bpm": None,
                    "inferenceReason": "缺少数值或已确认的等拍关系，保持当前速度等待校对。"}
    if metronome is not None or sound is not None and sound.get("tempo") is not None:
        return {"sourceKind": "unresolved-tempo", "sourceText": text or (metronome.findtext("per-minute", "") if metronome is not None else sound.get("tempo")),
                "inferred": False, "unresolved": True, "bpm": None,
                "inferenceReason": "速度数值或节拍单位不能可靠解析，保持当前速度等待校对。"}
    return None


def load_evidence(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schemaVersion") != 1 or not isinstance(data.get("marks"), list):
        raise ValueError("Tempo evidence requires schemaVersion 1 and marks array")
    return data


def evidence_marks(context, source_hash, measures):
    """Validate separately reviewed PDF text; never accept stale-source evidence."""
    if not context:
        return []
    if context.get("sourceSha256") != source_hash:
        raise ValueError("Tempo evidence belongs to a different MusicXML source")
    marks = []
    for entry in context.get("marks", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("text"), str) or not entry["text"].strip() or not entry.get("evidence"):
            raise ValueError("Tempo evidence mark requires text and visual evidence")
        index = next((i for i, measure in enumerate(measures) if str(measure["number"]) == str(entry.get("measure"))), None)
        if index is None:
            raise ValueError("Tempo evidence refers to an unknown measure")
        offset = entry.get("offset", 0)
        if type(offset) not in (int, float) or not math.isfinite(offset) or not 0 <= offset <= measures[index]["duration"]:
            raise ValueError("Tempo evidence offset is outside its measure")
        term, beat_bpm = infer_term(entry["text"])
        if beat_bpm is None:
            raise ValueError("Reviewed text-only tempo term is not recognized")
        pulse = meter_pulse(measures[index].get("timeSignature"))
        marks.append({"measureIndex": index + 1, "offset": float(offset), "sourcePart": "PDF",
                      "sourceKind": "pdf-reviewed-text", "sourceText": entry["text"],
                      "evidence": entry["evidence"], "matchedTerm": term, "priority": 20,
                      "bpm": beat_bpm * pulse["beatQuarterLength"], "beatBpm": beat_bpm,
                      **pulse, "inferred": True,
                      "inferenceReason": "原图只给文字速度；数字为固定词表与拍子脉冲推定的可编辑试听初值。"})
    return marks


def resolve(marks, measures, issue):
    """Resolve a deterministic timeline and preserve every source candidate."""
    marks = [dict(mark) for mark in marks]
    for mark in marks:
        measure = measures[mark["measureIndex"] - 1]
        mark["onset"] = measure["onset"] + mark["offset"]
        if mark["onset"] < 0:
            issue(measure["number"], "warning", "negative-tempo-offset", "速度标记位于时间线起点之前，起始速度采用该标记。", {"sourceText": mark.get("sourceText"), "onset": mark["onset"]}, "核对原谱速度标记位置。")
            mark["onset"] = 0
    pulse = meter_pulse(measures[0].get("timeSignature") if measures else None)
    fallback = {"onset": 0.0, "bpm": FALLBACK_QUARTER_BPM,
                "beatBpm": FALLBACK_QUARTER_BPM / pulse["beatQuarterLength"], **pulse,
                "sourceKind": "fallback", "sourceText": "无可识别初始速度", "inferred": True,
                "priority": 0, "inferenceReason": "没有初始速度证据，采用明确标注的 120 四分拍/分钟。"}
    chosen = {0.0: fallback}
    seen_unresolved = set()
    for mark in marks:
        number = measures[mark["measureIndex"] - 1]["number"]
        if mark.get("unresolved"):
            key = (mark["onset"], mark.get("sourceText"))
            if key not in seen_unresolved:
                issue(number, "warning", "unresolved-tempo-change", "此速度变化没有可靠数字或等拍关系，尚未模拟。", {"sourceText": mark.get("sourceText"), "sourcePart": mark.get("sourcePart"), "onset": mark["onset"]}, "对照原图确认变化方式；文字渐慢/回原速保留为待审。")
                seen_unresolved.add(key)
            continue
        old = chosen.get(mark["onset"])
        if old is not None and old["sourceKind"] != "fallback" and abs(old["bpm"] - mark["bpm"]) > .0001:
            issue(number, "warning", "tempo-conflict", "同一时间存在不同速度候选。", {"onset": mark["onset"], "bpms": [old["bpm"], mark["bpm"]], "sourceKinds": [old["sourceKind"], mark["sourceKind"]]}, "优先明确数值，再用已确认原图文字；同级候选保留第一项并等待校对。")
        if old is None or mark["priority"] > old["priority"]:
            chosen[mark["onset"]] = mark
        if mark.get("numericConflict"):
            issue(number, "warning", "metronome-sound-conflict", "书写速度与 MusicXML 播放速度不一致。", {"quarterBpms": mark["numericConflict"], "sourcePart": mark.get("sourcePart")}, "暂按显式 sound tempo 播放，核对原谱与导出。")
    initial = chosen[0.0]
    if initial["inferred"]:
        evidence = {key: initial[key] for key in ("bpm", "beatBpm", "beatUnit", "sourceKind", "sourceText")}
        if initial.get("evidence"):
            evidence["sourceEvidence"] = initial["evidence"]
        issue(measures[0]["number"] if measures else 1, "info", "initial-tempo-inferred", "初始速度是可编辑试听推定值，原谱没有确切数字。", evidence, "可按排练要求调整速度。")
    changes = [{key: value for key, value in mark.items() if key != "priority"}
               for mark in sorted(marks, key=lambda item: item["onset"])]
    tempos = [{key: value for key, value in mark.items() if key not in ("priority", "numericConflict")} for _, mark in sorted(chosen.items())]
    metadata = {"initialQuarterBpm": initial["bpm"], "initialBeatBpm": initial["beatBpm"],
                "beatUnit": initial["beatUnit"], "beatQuarterLength": initial["beatQuarterLength"],
                "sourceKind": initial["sourceKind"], "sourceText": initial["sourceText"],
                "inferred": initial["inferred"], "inferenceReason": initial.get("inferenceReason"),
                "evidence": initial.get("evidence"),
                "beatUnitSource": initial["beatUnitSource"], "policyVersion": POLICY_VERSION,
                "references": REFERENCES, "changes": changes,
                "unresolvedChanges": sum(bool(mark.get("unresolved")) for mark in marks)}
    return tempos, metadata
