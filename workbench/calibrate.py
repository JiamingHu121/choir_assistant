"""Version-bound, whole-score calibration: propose, inspect evidence, apply, rebuild.

No candidate is approved by an issue count, an OMR grade, or a meter fit.
The source files stay immutable; every run and decision remains inspectable.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil

from workbench.score_model import public_model
from workbench.workflow import build, file_hash, load_config, prepare_model, read_corrections, write_json

POLICY_VERSION = "calibration-transaction-v1"
EDIT_FIELDS = {"duration", "pitch", "tieTypes", "slurs", "tuplets", "articulations", "notation"}


def content_hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode()).hexdigest()


def resolve_inputs(config):
    """Reuse a hash-bound OMR import for PDF inputs instead of recognizing twice."""
    if config["input"].get("musicxml"):
        return config["input"]["musicxml"], config["input"].get("omr"), config.get("sourcePdf")
    pdf = Path(config["input"]["pdf"])
    from runtime import cache_dir
    cache = cache_dir() / "omr" / file_hash(pdf)
    manifest = cache / "input.json"
    if not manifest.exists():
        from omr.audiveris_runner import run_omr
        cache.mkdir(parents=True, exist_ok=True)
        snapshot = cache / pdf.name
        shutil.copy2(pdf, snapshot)
        exports = [Path(path).resolve() for path in run_omr(str(snapshot), str(cache))]
        record = {"pdfSha256": file_hash(pdf), "exports": [str(path) for path in exports],
                  "exportHashes": {str(path): file_hash(path) for path in exports},
                  "omrHashes": {str(path.with_suffix(".omr")): file_hash(path.with_suffix(".omr"))
                                for path in exports if path.with_suffix(".omr").is_file()}}
        write_json(manifest, record)
    record = json.loads(manifest.read_text())
    if record["pdfSha256"] != file_hash(pdf):
        raise ValueError("Cached OMR PDF identity changed")
    exports = record["exports"]
    index = config["input"].get("scoreIndex")
    if index is None and len(exports) != 1:
        raise ValueError("PDF import has multiple scores; set input.scoreIndex")
    index = 0 if index is None else index
    if index >= len(exports):
        raise ValueError("input.scoreIndex exceeds cached OMR exports")
    source = Path(exports[index])
    if not source.is_file() or file_hash(source) != record["exportHashes"][str(source)]:
        raise ValueError("Cached OMR export changed")
    omr = source.with_suffix(".omr")
    if omr.is_file() and file_hash(omr) != record.get("omrHashes", {}).get(str(omr)):
        raise ValueError("Cached OMR project changed or lacks a bound hash; use a fresh cache")
    return str(source), str(omr) if omr.is_file() else None, str(pdf)


def model_from_config(config, ledger=None):
    source, omr, _ = resolve_inputs(config)
    ledger = read_corrections(config.get("corrections")) if ledger is None else ledger
    return prepare_model(config, source, omr, ledger)


def scan(config, output=None, *, source_pdf=None, evidence=None, expected_revision=None):
    from workbench.calibration import plan_calibration, read_omr_features
    from workbench.calibration_evidence import build_evidence_packet
    config = deepcopy(config)
    if source_pdf:
        config["sourcePdf"] = str(Path(source_pdf).resolve())
    source, omr, pdf = resolve_inputs(config)
    # Freeze resolved OMR paths. Later apply never silently runs a new recognition.
    config["input"] = {"musicxml": source, "omr": omr, "scoreIndex": None}
    ledger = read_corrections(config.get("corrections"))
    model = model_from_config(config, ledger)
    if expected_revision and expected_revision != model["revision"]:
        raise ValueError("曲谱版本已经改变，请刷新并重新扫描")
    from runtime import data_dir, cache_dir
    output = Path(output or data_dir() / "calibration" /
                  datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S%fZ")).resolve()
    from workbench.workflow import ROOT
    if output.is_relative_to(ROOT):
        raise ValueError("Calibration outputs must stay outside the installed plugin")
    if output.exists():
        raise ValueError(f"Calibration output already exists: {output}")
    output.mkdir(parents=True)
    if evidence is not None and (not isinstance(evidence, dict) or "fields" not in evidence):
        raise ValueError("Source review requires a source-bound fields record, not an unbound evidence map")
    if isinstance(evidence, dict) and "fields" in evidence:
        if evidence.get("sourceSha256") != model["provenance"]["sourceSha256"]:
            raise ValueError("Source review findings belong to a different score")
        findings = evidence
        if not pdf or findings.get("sourcePdfSha256") != file_hash(pdf):
            raise ValueError("Source review findings belong to a different original PDF")
        symbols = {item["id"]: item for item in model["notes"] + model["rests"]}
        for ident, item in findings["fields"].items():
            if ident not in symbols:
                raise ValueError(f"Source finding refers to unknown symbol: {ident}")
            if "beforeDuration" in item and item["beforeDuration"] != symbols[ident]["duration"]:
                raise ValueError(f"Source finding before-duration is stale: {ident}")
            reference = item.get("reference", {})
            if (item.get("sourcePdfSha256") != findings["sourcePdfSha256"]
                    or reference.get("sourceSha256") != findings["sourcePdfSha256"]
                    or reference.get("source") != "original-pdf"
                    or not Path(reference.get("path", "")).is_file()
                    or file_hash(reference["path"]) != reference.get("sha256")):
                raise ValueError(f"Source finding original-PDF evidence changed or mismatched: {ident}")
        write_json(output / "source-findings.json", findings)
        evidence = deepcopy(findings["fields"])
    sources = {label: {"path": str(path), "sha256": file_hash(path)}
               for label, path in (("musicxml", source), ("omr", omr), ("pdf", pdf)) if path}
    plan = plan_calibration(model, evidence=evidence, omr_features=read_omr_features(model))
    write_json(output / "plan.json", plan)
    write_json(output / "model-before.json", public_model(model))
    write_json(output / "corrections-before.json", ledger)
    write_json(output / "config.json", config)
    record = {"schemaVersion": 1, "policyVersion": POLICY_VERSION, "status": "preparing-evidence",
              "createdAt": datetime.now(timezone.utc).isoformat(), "revision": model["revision"],
              "sourceSha256": model["provenance"]["sourceSha256"], "planSha256": content_hash(plan),
              "ledgerSha256": content_hash(ledger), "sources": sources, "summary": plan["summary"]}
    record["configSha256"] = content_hash(config)
    if (output / "source-findings.json").is_file():
        record["sourceFindingsSha256"] = content_hash(json.loads((output / "source-findings.json").read_text()))
    write_json(output / "run.json", record)
    try:
        if pdf:
            cache = cache_dir() / "source-pages" / file_hash(pdf) / "pages"
            if cache.is_dir():
                (output / "evidence").mkdir()
                shutil.copytree(cache, output / "evidence/pages")
        packet = build_evidence_packet(model, pdf, omr, output / "evidence", proposals=plan)
        if pdf and not cache.is_dir():
            cache.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(output / "evidence/pages", cache)
        record.update(status="ready", evidence="evidence/packet.json", packetSummary=packet.get("summary", {}),
                      packetSha256=file_hash(output / "evidence/packet.json"))
    except Exception as error:
        record.update(status="failed", error=str(error))
        write_json(output / "run.json", record)
        raise
    write_json(output / "run.json", record)
    return {**record, "output": str(output), "candidates": plan["candidates"]}


def _confirmed_source_page(candidate, record, packet, run):
    """Locate only the explicit original page frozen in a verified finding.

    No OMR geometry is needed: the reviewer supplies the page. Its reference
    must be the bound original PDF or a hash-identical full-page render; an
    arbitrary file claiming that PDF's identity is never promoted to evidence.
    """
    if (candidate.get("ruleId") != "source-verified" or candidate.get("status") != "auto-eligible"
            or not candidate.get("edits")):
        return None
    finding = candidate.get("evidence", {})
    reference = finding.get("reference", {})
    original = record.get("sources", {}).get("pdf")
    if (not original or finding.get("status") != "source-verified"
            or finding.get("sourceSha256") != record.get("sourceSha256")
            or reference.get("source") != "original-pdf"
            or reference.get("sourceSha256") != original["sha256"]
            or packet.get("sourceSha256") != original["sha256"]
            or type(reference.get("page")) is not int):
        return None
    page = next((item for item in packet.get("pages", []) if item.get("page") == reference["page"]), None)
    if page is None or not isinstance(reference.get("path"), str) or not reference.get("sha256"):
        return None
    frozen_path = Path(reference["path"]).resolve()
    original_path = Path(original["path"]).resolve()
    try:
        if not frozen_path.is_file() or file_hash(frozen_path) != reference["sha256"]:
            return None
        if frozen_path == original_path:
            if reference["sha256"] != original["sha256"]:
                return None
        elif reference["sha256"] != page["sha256"]:
            return None
        if not original_path.is_file() or file_hash(original_path) != original["sha256"]:
            return None
    except OSError:
        return None
    return {**page, "path": str(run / "evidence" / page["path"]),
            "source": "original-pdf", "sourceSha256": original["sha256"]}


def source_review_decisions(run):
    """Turn a verified agent's concrete findings into one atomic reviewed batch.

    Structural hypotheses, including unique meter solutions, remain unaccepted.
    This removes the need to hand-author dozens of identical JSON decisions.
    """
    run = Path(run).resolve()
    record = json.loads((run / "run.json").read_text())
    findings_path = run / "source-findings.json"
    if not findings_path.is_file() or content_hash(json.loads(findings_path.read_text())) != record.get("sourceFindingsSha256"):
        raise ValueError("This run has no frozen source-review findings")
    plan = json.loads((run / "plan.json").read_text())
    if content_hash(plan) != record["planSha256"]:
        raise ValueError("Calibration plan changed")
    packet_path = run / "evidence/packet.json"
    if file_hash(packet_path) != record["packetSha256"]:
        raise ValueError("Original-score evidence packet changed")
    packet = json.loads(packet_path.read_text())
    measures = {item["index"]: item for item in packet["measures"]}
    decisions = []
    for candidate in plan["candidates"]:
        if candidate["ruleId"] != "source-verified" or candidate["status"] != "auto-eligible" or not candidate["edits"]:
            continue
        # Prefer the reviewer's frozen original-page reference when valid.
        page = _confirmed_source_page(candidate, record, packet, run)
        references = [page] if page else []
        if not references:
            for index in candidate["measureIndices"]:
                crop = measures[index].get("crop")
                if not crop:
                    raise ValueError("Reviewed finding lacks verified original-page evidence")
                references.append({**crop, "path": str(run / "evidence" / crop["path"])})
        references = verify_evidence(references, run, candidate["measureIndices"])
        decisions.append({"candidateId": candidate["id"], "decision": "accept",
                          "reason": candidate["reason"], "evidence": references})
    if not decisions:
        raise ValueError("No concrete source-verified changes; all structural hypotheses remain pending")
    return {"schemaVersion": 1, "sourceSha256": plan["sourceSha256"], "revision": plan["revision"],
            "planSha256": record["planSha256"], "reviewer": "frozen-source-findings-batch",
            "decisions": decisions}


def current_value(symbol, field):
    if field in ("tuplets", "articulations"):
        return symbol.get("notation", {}).get(field, [])
    return symbol.get(field)


def matches_reviewed_value(actual, expected):
    """Model adds derived notation metadata; compare every explicit reviewed field."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and matches_reviewed_value(actual[key], value)
                                               for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            matches_reviewed_value(left, right) for left, right in zip(actual, expected))
    return actual == expected


def verify_evidence(entries, run, required_measures=None):
    if not isinstance(entries, list) or not entries:
        raise ValueError("接受修正必须附原谱证据文件及哈希")
    record = json.loads((run / "run.json").read_text())
    packet_path = run / "evidence/packet.json"
    if not packet_path.is_file() or file_hash(packet_path) != record.get("packetSha256"):
        raise ValueError("Original-score evidence packet changed")
    packet = json.loads(packet_path.read_text())
    allowed = {}
    coverage = {}
    def add(item, indices):
        if isinstance(item, dict) and item.get("path") and item.get("sha256"):
            path = (run / "evidence" / item["path"]).resolve()
            allowed[path] = item["sha256"]
            coverage.setdefault(path, set()).update(indices)
    for page in packet.get("pages", []):
        add(page, [measure["index"] for measure in packet.get("measures", []) if measure.get("page") == page["page"]])
    # With unavailable geometry, only an explicit frozen source-confirmed
    # page establishes a candidate's bar/page relationship. No structural
    # hypothesis or merely available PDF page supplies that relationship.
    plan_path = run / "plan.json"
    if plan_path.is_file():
        plan = json.loads(plan_path.read_text())
        if content_hash(plan) != record.get("planSha256"):
            raise ValueError("Calibration plan changed")
        for candidate in plan.get("candidates", []):
            page = _confirmed_source_page(candidate, record, packet, run)
            if page:
                path = Path(page["path"]).resolve()
                if allowed.get(path) == page["sha256"]:
                    coverage.setdefault(path, set()).update(candidate["measureIndices"])
    for measure in packet.get("measures", []):
        indices = [measure.get("index", measure.get("measureIndex"))]
        add(measure.get("crop"), indices)
        add(measure.get("context"), indices)
        add(measure.get("systemCrop"), indices)
        add(measure.get("systemContext"), indices)
        for staff in measure.get("staffContexts", []):
            add(staff.get("crop"), indices)
    if record.get("sources", {}).get("pdf"):
        source_pdf = record["sources"]["pdf"]
        allowed[Path(source_pdf["path"]).resolve()] = source_pdf["sha256"]
        coverage[Path(source_pdf["path"]).resolve()] = {measure.get("index", measure.get("measureIndex")) for measure in packet.get("measures", [])}
    verified = []
    covered = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry.get("sha256"):
            raise ValueError("Evidence requires path and sha256")
        path = Path(entry["path"])
        path = (run / path if not path.is_absolute() else path).resolve()
        if (allowed.get(path) != entry["sha256"] or not path.is_file()
                or file_hash(path) != entry["sha256"]):
            raise ValueError(f"Source evidence missing or changed: {entry['path']}")
        verified.append({**entry, "path": str(path)})
        covered.update(coverage.get(path, set()))
    if required_measures and not set(required_measures).issubset(covered):
        raise ValueError("Source evidence does not cover the proposed correction's measure")
    return verified


def guarded_ledger(plan, model, ledger, review, run):
    """Reject stale, duplicated or overlapping decisions before making any write."""
    if review.get("schemaVersion") != 1:
        raise ValueError("Unsupported calibration review schema")
    for key, expected in (("sourceSha256", plan["sourceSha256"]), ("revision", plan["revision"]),
                          ("planSha256", content_hash(plan))):
        if review.get(key) != expected:
            raise ValueError(f"Calibration review {key} is stale or mismatched")
    decisions = review.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("Review decisions cannot be empty")
    candidates = {candidate["id"]: candidate for candidate in plan["candidates"]}
    symbols = {symbol["id"]: symbol for symbol in model["notes"] + model["rests"]}
    result, applied, seen, fields = deepcopy(ledger), [], set(), set()
    for decision in decisions:
        ident = decision.get("candidateId")
        if ident not in candidates or ident in seen:
            raise ValueError("Unknown or duplicate calibration candidate")
        seen.add(ident)
        if decision.get("decision") not in ("accept", "reject", "uncertain"):
            raise ValueError("Decision must be accept, reject or uncertain")
        if not isinstance(decision.get("reason"), str) or not decision["reason"].strip():
            raise ValueError("Every review decision needs a reason")
        if decision["decision"] != "accept":
            continue
        candidate = candidates[ident]
        if not candidate.get("edits"):
            raise ValueError("Candidate has no concrete correction; request a new source-backed proposal")
        evidence = verify_evidence(decision.get("evidence"), Path(run), candidate.get("measureIndices"))
        for edit in candidate["edits"]:
            target, field = edit["objectId"], edit["field"]
            if target not in symbols or field not in EDIT_FIELDS:
                raise ValueError("Unknown calibration target or unsupported field")
            if (target, field) in fields:
                raise ValueError("Accepted proposals have conflicting field edits")
            fields.add((target, field))
            if current_value(symbols[target], field) != edit["before"]:
                raise ValueError(f"Calibration before-value changed: {target}/{field}")
            change = result.setdefault(target, {})
            # Preserve previous independently reviewed fields, evidence and reason.
            previous_reason = change.get("reason", "")
            change.update({field: deepcopy(edit["after"]),
                           "reason": (previous_reason + "；" if previous_reason else "") + decision["reason"],
                           "calibration": {"candidateId": ident, "ruleId": candidate["ruleId"],
                               "sourceSha256": plan["sourceSha256"], "revision": plan["revision"],
                               "evidence": evidence, "reviewer": review.get("reviewer", "source-review"),
                               "status": "source-reviewed"}})
            applied.append({**edit, "candidateId": ident, "ruleId": candidate["ruleId"], "evidence": evidence})
    return result, applied


def validate_delta(before, after, applied):
    old = {symbol["id"]: symbol for symbol in before["notes"] + before["rests"]}
    new = {symbol["id"]: symbol for symbol in after["notes"] + after["rests"]}
    if set(old) != set(new):
        raise ValueError("Calibration changed source symbol IDs")
    for ident in old:
        raw_before = {key: value for key, value in old[ident]["source"].items() if key not in ("reviewed", "calibration")}
        raw_after = {key: value for key, value in new[ident]["source"].items() if key not in ("reviewed", "calibration")}
        if raw_before != raw_after:
            raise ValueError(f"Calibration changed original source provenance: {ident}")
    for edit in applied:
        field, symbol = edit["field"], new[edit["objectId"]]
        actual = current_value(symbol, field)
        # Ledger articulation values are names; model entries carry XML attributes.
        if field == "articulations":
            actual = [item["type"] for item in actual]
        if not matches_reviewed_value(actual, edit["after"]):
            raise ValueError(f"Calibration edit was not applied: {edit['objectId']}/{field}")
    before_bad = set(before["rhythmValidation"]["blockingMeasureIndices"])
    after_bad = set(after["rhythmValidation"]["blockingMeasureIndices"])
    if after_bad - before_bad:
        raise ValueError(f"Calibration introduced newly invalid bars: {sorted(after_bad-before_bad)}")
    before_count = sum(bool(issue.get("rhythmBlocking")) for issue in before["audit"]["issues"])
    after_count = sum(bool(issue.get("rhythmBlocking")) for issue in after["audit"]["issues"])
    if after_count > before_count:
        raise ValueError("Calibration increased blocking structural issues")
    def issue_key(issue):
        evidence = issue.get("evidence", {})
        return (issue["kind"], issue.get("measure"), evidence.get("sourcePart"),
                evidence.get("staff"), evidence.get("voice"), tuple(sorted(evidence.get("noteIds", []))),
                tuple(evidence.get("measureIndices", [])))
    old_issues = {issue_key(item) for item in before["audit"]["issues"] if item.get("rhythmBlocking")}
    new_issues = {issue_key(item) for item in after["audit"]["issues"] if item.get("rhythmBlocking")}
    if new_issues - old_issues:
        raise ValueError("Calibration introduced a new structural contradiction inside an existing invalid bar")
    return {"beforeBlockingIssues": before_count, "afterBlockingIssues": after_count,
            "beforeBlockingMeasures": sorted(before_bad), "afterBlockingMeasures": sorted(after_bad),
            "resolvedMeasures": sorted(before_bad-after_bad), "sourceIdsPreserved": len(old),
            "originalProvenancePreserved": True, "appliedEdits": len(applied)}


def apply_review(run, review, *, publish_current=False, build_bundle=True):
    if publish_current:
        raise ValueError("publish_current is unsupported; serve the isolated rebuilt bundle")
    run = Path(run).resolve()
    record = json.loads((run / "run.json").read_text())
    if record.get("status") != "ready":
        raise ValueError("Calibration evidence preparation has not completed successfully")
    if (run / "application.json").exists():
        raise ValueError("This calibration run was already applied; rescan the new revision")
    plan = json.loads((run / "plan.json").read_text())
    if content_hash(plan) != record["planSha256"]:
        raise ValueError("Calibration plan changed after evidence generation")
    config = json.loads((run / "config.json").read_text())
    if content_hash(config) != record.get("configSha256"):
        raise ValueError("Frozen calibration configuration changed")
    for source in record["sources"].values():
        if not Path(source["path"]).is_file() or file_hash(source["path"]) != source["sha256"]:
            raise ValueError("Original score source changed; rescan")
    ledger = read_corrections(config.get("corrections"))
    if content_hash(ledger) != record["ledgerSha256"]:
        raise ValueError("Correction ledger changed; rescan")
    before = model_from_config(config, ledger)
    if before["revision"] != record["revision"]:
        raise ValueError("Model policy or revision changed; rescan")
    review = json.loads(Path(review).read_text()) if isinstance(review, (str, Path)) else review
    corrected, applied = guarded_ledger(plan, before, ledger, review, run)
    after = model_from_config(config, corrected)
    delta = validate_delta(before, after, applied)
    write_json(run / "review.json", review)
    write_json(run / "corrections-after.json", corrected)
    write_json(run / "model-after.json", public_model(after))
    result = {"schemaVersion": 1, "policyVersion": POLICY_VERSION, "status": "review-recorded",
              "beforeRevision": before["revision"], "revision": after["revision"],
              "delta": delta, "edits": applied,
              "decisions": Counter(item["decision"] for item in review["decisions"]),
              "sourceSha256": record["sourceSha256"], "planSha256": record["planSha256"]}
    if not applied:
        write_json(run / "application.json", result)
        return result
    if build_bundle:
        bundle_config = {**config, "corrections": str(run / "corrections-after.json"), "reviewFile": None}
        bundle = build(bundle_config, run / "rebuilt")
        if bundle["revision"] != after["revision"]:
            raise ValueError("Rebuilt bundle differs from the reviewed configured model")
        result["bundle"] = str(run / "rebuilt")
        result["bundleStatus"] = bundle["status"]
    result["status"] = "rebuilt" if build_bundle else "validated-proposal"
    write_json(run / "application.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Explicit project configuration; required for a new scan")
    parser.add_argument("--source-pdf")
    parser.add_argument("--source-review", help="Hash-bound per-symbol source findings to add concrete reviewed hypotheses")
    parser.add_argument("--output")
    parser.add_argument("--run", help="Existing immutable calibration run")
    parser.add_argument("--review", help="Hash-bound review decisions JSON")
    parser.add_argument("--accept-source-reviewed", action="store_true", help="Apply only the run's frozen source-verified agent findings as a batch")
    parser.add_argument("--no-bundle", action="store_true", help="Validate ledger edits without rendering audio")
    args = parser.parse_args()
    if args.run or args.review or args.accept_source_reviewed:
        if not args.run or not (args.review or args.accept_source_reviewed) or args.review and args.accept_source_reviewed:
            parser.error("--run requires either --review or --accept-source-reviewed")
        review = source_review_decisions(args.run) if args.accept_source_reviewed else args.review
        result = apply_review(args.run, review,
                              build_bundle=not args.no_bundle)
    else:
        if not args.config:
            parser.error("--config is required for a new scan")
        evidence = json.loads(Path(args.source_review).read_text()) if args.source_review else None
        result = scan(load_config(args.config), args.output, source_pdf=args.source_pdf, evidence=evidence)
    print(json.dumps({key: value for key, value in result.items() if key not in ("candidates", "edits")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
