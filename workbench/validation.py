"""Keep draft audition separate from rhythm-validated generation."""
from __future__ import annotations


POLICIES = {"strict", "draft"}


def rhythm_scope(model, note_ids=None):
    """Use the whole selected time interval, including silence between notes."""
    measures = model.get("measures", [])
    if note_ids is None:
        return {m["index"] for m in measures}
    if not isinstance(note_ids, (list, tuple, set)) or not note_ids:
        raise ValueError("请先选择音符")
    lookup = {n["id"]: n for n in model.get("notes", [])}
    if any(nid not in lookup for nid in note_ids):
        raise ValueError("所选音符已失效，请刷新曲谱")
    selected = [lookup[nid] for nid in note_ids]
    start, end = min(n["start"] for n in selected), max(n["end"] for n in selected)
    return {m["index"] for m in measures if m["start"] < end and m["end"] > start}


def require_rhythm(model, policy="strict", note_ids=None):
    if policy not in POLICIES:
        raise ValueError("rhythmPolicy 必须为 strict 或 draft")
    validation = model.get("rhythmValidation")
    if validation is None:
        raise ValueError("模型缺少节奏校验契约，请重建曲谱")
    scope = rhythm_scope(model, note_ids)
    unresolved = sorted(scope & set(validation.get("blockingMeasureIndices", [])))
    labels = {m["index"]: m["number"] for m in model.get("measures", [])}
    blocking = [labels.get(i, i) for i in unresolved]
    # Missing location must never make a globally invalid model pass strict mode.
    unlocated = validation.get("status") != "validated" and not validation.get("blockingMeasureIndices")
    if policy == "strict" and (blocking or unlocated):
        location = "、".join(map(str, blocking[:12])) or "未定位"
        raise ValueError(f"第 {location} 小节仍有未解决的节奏问题；请先校对，或选择草稿试听。")
    return {"policy": policy, "status": "draft" if blocking or unlocated else "rhythm-validated",
            "blockingMeasures": blocking, "scopeMeasureIndices": sorted(scope),
            "sourceRecognition": "Source-image verification is separate from structural rhythm checks"}
