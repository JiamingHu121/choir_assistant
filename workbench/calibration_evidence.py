"""Bind whole-score calibration decisions to independently readable PDF evidence.

The clean reviewer queue deliberately omits engine edits and recognized durations.
Its images come from the original PDF, never from corrected SVG or MusicXML.  OMR
geometry is a locator, not a second recognition vote or evidence of correctness.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
from html import escape
from math import ceil, floor, isfinite
from pathlib import Path
import json
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile

from PIL import Image
from runtime import find_binary

EVIDENCE_POLICY = "original-pdf-context-v1"
DEFAULT_DPI = 300


def json_sha256(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


def file_sha256(path):
    digest = sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _binary(name, explicit=None):
    return find_binary(name, explicit)


def render_pdf(pdf, output, dpi=DEFAULT_DPI, renderer=None):
    """Render original pages once, and verify hashes before reusing any cache."""
    pdf, output = Path(pdf).resolve(), Path(output).resolve()
    if not pdf.is_file():
        raise ValueError(f"PDF source does not exist: {pdf}")
    if type(dpi) is not int or not 100 <= dpi <= 600:
        raise ValueError("Evidence DPI must be an integer between 100 and 600")
    output.mkdir(parents=True, exist_ok=True)
    source_hash = file_sha256(pdf)
    manifest_path = output / "render-manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        pages = previous.get("pages", [])
        if (previous.get("sourceSha256") == source_hash and previous.get("dpi") == dpi
                and pages and all((output / item["path"]).is_file()
                                 and file_sha256(output / item["path"]) == item["sha256"] for item in pages)):
            return previous
    # A clean temporary prefix keeps an interrupted or differently sized render
    # from masquerading as a complete cache on the next invocation.
    prefix = output / "rendering"
    for stale in output.glob("rendering-*.png"):
        stale.unlink()
    command = [_binary("pdftoppm", renderer), "-png", "-r", str(dpi), str(pdf), str(prefix)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f"Original PDF render failed: {result.stderr[-2000:]}")
    images = sorted(output.glob("rendering-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
    if not images:
        raise RuntimeError("Original PDF renderer produced no pages")
    pages = []
    for position, image_path in enumerate(images, 1):
        actual_page = int(image_path.stem.rsplit("-", 1)[1])
        if actual_page != position:
            raise RuntimeError("Original PDF render has a missing page")
        target = output / f"page-{position:03d}.png"
        image_path.replace(target)
        with Image.open(target) as image:
            image.verify()
        with Image.open(target) as image:
            width, height = image.size
        pages.append({"page": position, "path": target.name, "sha256": file_sha256(target),
                      "width": width, "height": height})
    manifest = {"schemaVersion": 1, "sourceSha256": source_hash, "sourcePath": str(pdf),
                "dpi": dpi, "renderer": "pdftoppm", "pages": pages}
    _write_json(manifest_path, manifest)
    return manifest


def _point_bounds(points, width, height, pad=0):
    values = [(float(p.get("x")), float(p.get("y"))) for p in points]
    if not values:
        return None
    x0, x1 = min(p[0] for p in values), max(p[0] for p in values)
    y0, y1 = min(p[1] for p in values), max(p[1] for p in values)
    return {"x": (x0 - pad) / width, "y": (y0 - pad) / height,
            "w": (x1 - x0 + 2 * pad) / width, "h": (y1 - y0 + 2 * pad) / height}


def omr_geometry(omr):
    """Extract only page/system/staff/stack coordinates, without inferred notes."""
    if omr is None:
        return {"pages": [], "measures": []}
    omr = Path(omr)
    if not omr.is_file():
        raise ValueError(f"OMR locator does not exist: {omr}")
    pages, measures = [], []
    with zipfile.ZipFile(omr) as archive:
        sheets = sorted((n for n in archive.namelist() if re.fullmatch(r"sheet#\d+/sheet#\d+\.xml", n)),
                        key=lambda n: int(n.split("#")[1].split("/")[0]))
        for name in sheets:
            sheet = ET.fromstring(archive.read(name))
            picture = sheet.find("picture")
            page = int(name.split("#")[1].split("/")[0])
            width, height = int(picture.get("width")), int(picture.get("height"))
            if width <= 0 or height <= 0:
                raise ValueError("Invalid OMR image dimensions")
            pages.append({"page": page, "width": width, "height": height})
            for system in sheet.findall(".//system"):
                system_id = int(system.get("id"))
                systems_points = system.findall("part/staff/lines/line/point")
                # Tempo/equal-beat directions can sit well above the top line;
                # keep that semantic context rather than cropping to noteheads.
                system_box = _point_bounds(systems_points, width, height, 45)
                if system_box is None:
                    continue
                system_box["y"] -= 75 / height
                system_box["h"] += 95 / height
                staves = []
                for part in system.findall("part"):
                    for staff_index, staff in enumerate(part.findall("staff"), 1):
                        points = staff.findall("lines/line/point")
                        box = _point_bounds(points, width, height, 38)
                        if box:
                            staves.append({"sourcePart": f"P{part.get('id')}", "staff": staff_index,
                                           "physicalStaffId": staff.get("id"), "region": box})
                for stack in system.findall("stack"):
                    left, right = float(stack.get("left")), float(stack.get("right"))
                    measures.append({"index": len(measures) + 1, "page": page, "system": system_id,
                                     "region": {"x": left / width, "y": system_box["y"],
                                                "w": (right - left) / width, "h": system_box["h"]},
                                     "systemRegion": system_box, "staves": deepcopy(staves)})
    return {"pages": pages, "measures": measures}


def _region(region, padding=0):
    if not isinstance(region, dict) or any(k not in region for k in ("x", "y", "w", "h")):
        raise ValueError("Invalid normalized evidence region")
    x, y, width, height = (float(region[k]) for k in ("x", "y", "w", "h"))
    if not all(isfinite(v) for v in (x, y, width, height, padding)) or width <= 0 or height <= 0:
        raise ValueError("Evidence region must have finite positive dimensions")
    x0, y0 = max(0, x - padding), max(0, y - padding)
    x1, y1 = min(1, x + width + padding), min(1, y + height + padding)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("Evidence region lies outside the source page")
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def _crop(page_image, region, destination, root, source_hash, page):
    region = _region(region)
    own_image = not isinstance(page_image, Image.Image)
    image = Image.open(page_image) if own_image else page_image
    try:
        width, height = image.size
        pixels = [floor(region["x"] * width), floor(region["y"] * height),
                  ceil((region["x"] + region["w"]) * width), ceil((region["y"] + region["h"]) * height)]
        image.crop(pixels).save(destination)
    finally:
        if own_image:
            image.close()
    return {"path": destination.relative_to(root).as_posix(), "sha256": file_sha256(destination),
            "sourceSha256": source_hash, "page": page, "region": region, "pixels": pixels,
            "source": "original-pdf", "locator": "OMR geometry only; notation not trusted"}


def _issue_indices(issue, measures):
    indices = issue.get("blockingMeasureIndices") or issue.get("evidence", {}).get("measureIndices")
    if indices:
        return [index for index in indices if index in measures]
    number = issue.get("measure")
    return [index for index, measure in measures.items() if str(measure.get("number")) == str(number)]


def _candidates(proposals):
    if proposals is None:
        return []
    if isinstance(proposals, list):
        return proposals
    if not isinstance(proposals, dict):
        raise ValueError("Calibration proposals must be a list or plan object")
    return proposals.get("candidates", proposals.get("proposals", []))


def _safe_symbol(event):
    return {key: deepcopy(event.get(key)) for key in
            ("id", "kind", "measureIndex", "sourcePart", "part", "staff", "voice", "pitch", "pitchName",
             "offset", "duration", "rational", "notation", "tieTypes", "slurs", "chordId", "chordTone", "source")}


def _html(packet, root):
    cards = []
    for measure in packet["measures"]:
        context = measure.get("crop")
        missing_picture = ("<p>原图定位待确认，请查看整页。</p>" if packet["summary"]["sourceImagesAvailable"] else
                           "<p>暂无原谱图像。结构候选与源 XML 哈希已保留，等待补充原图审读。</p>")
        picture = (f'<a href="{escape(context["path"])}"><img loading="lazy" src="{escape(context["path"])}" '
                   f'alt="第 {escape(str(measure["number"]))} 小节原谱"></a>') if context else missing_picture
        issues = " · ".join(escape(kind) for kind in measure["issueKinds"]) or "未触发结构疑点，仍需原图覆盖检查"
        cards.append(f'<section id="m{measure["index"]}"><h2>第 {escape(str(measure["number"]))} 小节</h2>'
                     f'<p>PDF 第 {measure.get("page", "待确认")} 页 · {issues}</p>{picture}'
                     f'<p>{len(measure["symbols"])} 个源对象 · {len(measure["candidateIds"])} 个修改候选</p></section>')
    source_message = ("图像直接渲染原始 PDF；结构约束用于提出疑点，不能替代读谱。"
                      if packet["summary"]["sourceImagesAvailable"] else
                      "暂无原始 PDF 图像。当前仅完成结构候选检查，所有小节保留待审；需提供原谱图像后才能接受原图修订。")
    content = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>全谱校准原图审核包</title><style>body{{font:15px system-ui;margin:0 auto;max-width:1250px;padding:24px;background:#f3f5f8;color:#182235}}section{{background:white;padding:18px;margin:18px 0;border-radius:12px}}img{{display:block;width:100%;height:auto}}h1{{font-size:24px}}h2{{font-size:18px}}nav{{display:flex;gap:8px;flex-wrap:wrap}}a{{color:#285baf}}p{{line-height:1.6}}</style>
<h1>全谱校准原图审核包</h1><p>所有 {packet['summary']['measures']} 小节都进入检查范围。{source_message}</p>
<p>独立审读先打开 <a href="source-review-queue.json">无修改建议的原谱队列</a>，再与 <a href="packet.json">候选及逐音结构</a> 比较。决定写入 <a href="decisions.template.json">决策模板</a>；模板中的 uncertain 不代表接受。</p>
<nav>{''.join(f'<a href="#m{m["index"]}">{escape(str(m["number"]))}</a>' for m in packet['measures'])}</nav>
{''.join(cards)}</html>'''
    (root / "index.html").write_text(content, encoding="utf-8")


def build_evidence_packet(model, pdf, omr, output, proposals=None, *, dpi=DEFAULT_DPI, renderer=None):
    """Build all-measure original-PDF crops and a hash-bound independent review queue.

    A packet can be produced without exact OMR locations.  Such measures are
    explicitly unresolved and retain whole-page references; no positional match
    or source verification is fabricated from a plausible MusicXML sequence.
    """
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    crop_dir = output / "crops"
    crop_dir.mkdir(exist_ok=True)
    renders = (render_pdf(pdf, output / "pages", dpi, renderer) if pdf is not None else
               {"sourceSha256": None, "sourcePath": None, "dpi": None, "pages": []})
    source_hash = renders["sourceSha256"]
    geometry = omr_geometry(omr)
    pages = {page["page"]: page for page in renders["pages"]}
    measures = {measure.get("index", position): measure for position, measure in enumerate(model.get("measures", []), 1)}
    if not measures or len(measures) != len(model["measures"]):
        raise ValueError("Model needs uniquely indexed measures")
    geometry_join = len(geometry["measures"]) == len(measures)
    geometry_error = "sourceimage-unavailable: no original PDF was supplied" if pdf is None else None
    if geometry["measures"] and not geometry_join and pdf is not None:
        geometry_error = "OMR stack count differs from model measures; positional join was disabled"
    if geometry_join and pdf is not None:
        # Ordinal geometry is accepted only if any existing checked model page/
        # system locators agree, and each OMR page has the rendered aspect ratio.
        for location in geometry["measures"]:
            measure = measures.get(location["index"])
            if measure is None or any(measure.get(key) is not None and measure[key] != location[key] for key in ("page", "system")):
                geometry_join, geometry_error = False, "OMR ordinal geometry disagrees with model locators"
                break
        for omr_page in geometry["pages"]:
            rendered = pages.get(omr_page["page"])
            if rendered is None or abs(omr_page["width"] / omr_page["height"] - rendered["width"] / rendered["height"]) > .03:
                geometry_join, geometry_error = False, "OMR/PDF page count or aspect ratio disagrees"
                break
    if pdf is None:
        geometry_join = False
    locations = {item["index"]: item for item in geometry["measures"]} if geometry_join else {}
    events = model.get("notes", []) + model.get("rests", [])
    by_measure, by_id = defaultdict(list), {}
    for event in events:
        if event.get("id") in by_id:
            raise ValueError(f"Duplicate source object ID: {event['id']}")
        by_id[event["id"]] = event
        by_measure[event["measureIndex"]].append(event)
    issues_by_measure, unlocated_issues = defaultdict(list), []
    for issue in model.get("audit", {}).get("issues", []):
        indices = _issue_indices(issue, measures)
        if not indices:
            unlocated_issues.append(deepcopy(issue))
        for index in indices:
            issues_by_measure[index].append(issue)
    candidates = _candidates(proposals)
    candidates_by_measure, candidate_records = defaultdict(list), []
    for candidate in candidates:
        record = deepcopy(candidate)
        indices = candidate.get("measureIndices", [])
        if not indices:
            indices = sorted({by_id[edit["objectId"]]["measureIndex"] for edit in candidate.get("edits", [])
                              if edit.get("objectId") in by_id})
        record["measureIndices"] = indices
        candidate_records.append(record)
        for index in indices:
            if index in measures:
                candidates_by_measure[index].append(record)
    packet_measures, clean_measures, contexts = [], [], {}
    loaded_page, current_image = None, None
    for index, measure in measures.items():
        record = {"index": index, "number": measure.get("number", index), "locatorStatus": "unresolved",
                  "issueIds": [issue["id"] for issue in issues_by_measure[index]],
                  "issueKinds": sorted({issue["kind"] for issue in issues_by_measure[index]}),
                  "issues": deepcopy(issues_by_measure[index]),
                  "candidateIds": [candidate["id"] for candidate in candidates_by_measure[index]],
                  "symbols": [_safe_symbol(event) for event in sorted(by_measure[index], key=lambda e: (e['sourcePart'], str(e['voice']), e.get('source', {}).get('noteOrdinal') or 10**9))],
                  "voiceCoverage": deepcopy(measure.get("voiceCoverage", [])), "staffContexts": [],
                  "pageReferences": [dict(page, path=f"pages/{page['path']}") for page in renders["pages"]]}
        location = locations.get(index)
        if location:
            page, system = location["page"], location["system"]
            image_path = output / "pages" / pages[page]["path"]
            if loaded_page != page:
                if current_image is not None:
                    current_image.close()
                current_image = Image.open(image_path)
                current_image.load()
                loaded_page = page
            record.update(page=page, system=system, locatorStatus="checked-ordinal-geometry")
            record["pageReferences"] = [dict(pages[page], path=f"pages/{pages[page]['path']}")]
            record["crop"] = _crop(current_image, _region(location["region"], .009), crop_dir / f"measure-{index:03d}.png", output, source_hash, page)
            key = (page, system)
            if key not in contexts:
                contexts[key] = _crop(current_image, _region(location["systemRegion"], .005), crop_dir / f"page-{page:03d}-system-{system:02d}.png", output, source_hash, page)
            record["systemContext"] = contexts[key]
            for staff in location["staves"]:
                staff_region = dict(staff["region"])
                staff_region.update(x=max(0, location["region"]["x"] - .015), w=min(1, location["region"]["w"] + .03))
                reference = _crop(current_image, staff_region, crop_dir / f"measure-{index:03d}-{staff['sourcePart']}-staff{staff['staff']}.png", output, source_hash, page)
                record["staffContexts"].append({key: staff[key] for key in ("sourcePart", "staff", "physicalStaffId")} | {"crop": reference})
        packet_measures.append(record)
        clean_measures.append({key: deepcopy(record[key]) for key in ("index", "number", "locatorStatus", "staffContexts", "pageReferences")} |
                              {key: deepcopy(record[key]) for key in ("page", "system", "crop", "systemContext") if key in record} |
                              {"transcription": [], "reviewStatus": "pending", "instructions": "Read original notes, rests, beams, dots, ties/slurs, meter and tempo first; do not infer missing notes from harmonic expectations."})
    if current_image is not None:
        current_image.close()
    measure_records = {measure["index"]: measure for measure in packet_measures}
    for record in candidate_records:
        record["sourceEvidence"] = [measure_records[i]["crop"] for i in record["measureIndices"]
                                    if i in measure_records and measure_records[i].get("crop")]
        record["sourceEvidenceStatus"] = "located-unreviewed" if record["sourceEvidence"] else "unresolved"
    source = {"path": str(Path(pdf).resolve()) if pdf is not None else None, "sha256": source_hash,
              "omr": {"path": str(Path(omr).resolve()), "sha256": file_sha256(omr)} if omr else None,
              "musicxmlSha256": model.get("provenance", {}).get("sourceSha256")}
    revision = model.get("revision", model.get("version"))
    plan_hash = json_sha256(proposals) if proposals is not None else None
    packet = {"schemaVersion": 1, "evidencePolicy": EVIDENCE_POLICY, "source": source, "sourceSha256": source_hash,
              "sourceImagesAvailable": pdf is not None, "sourceImageStatus": "original-pdf-rendered" if pdf is not None else "sourceimage-unavailable",
              "revision": revision, "planSha256": plan_hash, "dpi": dpi,
              "geometry": {"status": "joined" if geometry_join else "unresolved", "error": geometry_error,
                           "modelMeasures": len(measures), "omrStacks": len(geometry["measures"]),
                           "policy": "count/page/system/aspect checked ordinal stacks; exact source symbol join remains separately checked"},
              "pages": [dict(page, path=f"pages/{page['path']}") for page in renders["pages"]],
              "measures": packet_measures, "candidates": candidate_records, "unlocatedIssues": unlocated_issues,
              "summary": {"measures": len(measures), "locatedMeasures": sum(m["locatorStatus"] != "unresolved" for m in packet_measures),
                          "sourceImagesAvailable": pdf is not None,
                          "pages": len(pages), "sourceObjects": len(events), "exactSymbolPositions": sum(bool(e.get("source", {}).get("omr")) for e in events),
                          "candidates": len(candidate_records), "sourceReviewedMeasures": 0},
              "confidence": "Crops locate evidence; no candidate is accepted and no bar is source-verified by generating this packet."}
    clean = {"schemaVersion": 1, "sourceSha256": source_hash, "revision": revision,
             "sourceImagesAvailable": pdf is not None, "musicxmlSha256": model.get("provenance", {}).get("sourceSha256"),
             "scope": "every-model-measure, including structurally clean bars", "engineEditsVisible": False,
             "policy": "Independent source transcription before reading proposed edits; locator coordinates are OMR-derived, images are original PDF.",
             "measures": clean_measures}
    template_source = proposals.get("sourceSha256") if isinstance(proposals, dict) else None
    template_source = template_source or model.get("provenance", {}).get("sourceSha256")
    template = {"schemaVersion": 1, "sourceSha256": template_source, "sourcePdfSha256": source_hash,
                "revision": revision, "planSha256": plan_hash,
                "decisions": [{"candidateId": record["id"], "decision": "uncertain", "reason": "Pending independent original-PDF review",
                               "evidence": [dict(reference, path=str(output / reference["path"])) for reference in record["sourceEvidence"]]}
                              for record in candidate_records]}
    _write_json(output / "packet.json", packet)
    _write_json(output / "source-review-queue.json", clean)
    _write_json(output / "decisions.template.json", template)
    _html(packet, output)
    return packet
