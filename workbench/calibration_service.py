"""Responsive loopback calibration jobs; keep mutations under the server lock."""
from copy import deepcopy
import json
from pathlib import Path
import threading
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

from workbench.calibrate import apply_review, content_hash, scan
from workbench.workflow import ROOT, file_hash, load_config, write_json

WEB = ROOT / "workbench"


def artifact_url(path, generated=None):
    path = Path(path).resolve()
    if generated is None:
        raise ValueError("A project generated directory is required")
    generated = Path(generated).resolve()
    if not path.is_relative_to(generated):
        raise ValueError("Calibration evidence is outside generated artifacts")
    return "/generated/" + path.relative_to(generated).as_posix()


def describe_run(run, revision, *, generated=None, receipt_path=None):
    url = lambda path: artifact_url(path, generated)
    run = Path(run).resolve()
    record = json.loads((run / "run.json").read_text())
    plan = json.loads((run / "plan.json").read_text())
    packet_path = run / "evidence/packet.json"
    packet = json.loads(packet_path.read_text()) if packet_path.exists() else {}
    crops = {}
    for measure in packet.get("measures", []):
        refs = [measure.get("crop"), measure.get("systemContext")]
        refs.extend(item.get("crop") for item in measure.get("staffContexts", []))
        crops[measure["index"]] = [run / "evidence" / item["path"] for item in refs if item]
    application_path = run / "application.json"
    application = json.loads(application_path.read_text()) if application_path.exists() else None
    reviewed = {}
    if (run / "review.json").is_file():
        reviewed = {item["candidateId"]: item["decision"] for item in json.loads((run / "review.json").read_text())["decisions"]}
    candidates = []
    for item in plan["candidates"]:
        paths = {path for index in item.get("measureIndices", []) for path in crops.get(index, [])}
        evidence_urls = [url(path) for path in sorted(paths) if path.is_file()]
        candidates.append({**item, "current": [{"objectId": edit["objectId"], "field": edit["field"], "value": edit["before"]} for edit in item["edits"]],
                           "proposed": [{"objectId": edit["objectId"], "field": edit["field"], "value": edit["after"]} for edit in item["edits"]],
                           "status": {"accept": "accepted", "reject": "rejected", "uncertain": "pending"}.get(reviewed.get(item["id"]), item["status"]),
                           "evidenceUrls": evidence_urls})
    summary = {key: value for key, value in plan["summary"].items() if isinstance(value, (str, int, float))}
    before = json.loads((run / "model-before.json").read_text())
    summary.update(blocking=len(before["rhythmValidation"]["blockingIssueIds"]),
                   reviewRequired=len(plan["summary"].get("reviewMeasures", [])))
    if application:
        summary.update(appliedEdits=application["delta"]["appliedEdits"],
                       blocking=application["delta"]["afterBlockingIssues"])
    receipt_path = Path(receipt_path or WEB / "calibration-current.json")
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text())
        if receipt.get("revision") == revision:
            summary.update(appliedEdits=receipt["delta"]["appliedEdits"],
                           appliedSymbols=len({edit["objectId"] for edit in receipt.get("edits", [])}))
    return {"status": "ready" if record["status"] == "ready" else record["status"],
            "revision": revision, "scannedRevision": record["revision"], "runId": run.name,
            "summary": summary, "candidates": candidates, "canApply": not application and record["revision"] == revision,
            "reportUrl": url(run / "evidence/index.html"), "packetUrl": url(packet_path),
            "reload": bool(application and application.get("status") in ("published", "published-preview")), "error": record.get("error")}


class CalibrationManager:
    def __init__(self, read_model, mutation_lock, *, read_config=None, generated=None, receipt_path=None, publish=None):
        self.read_model = read_model
        self.mutation_lock = mutation_lock
        self.state_lock = threading.Lock()
        self.state = {"status": "idle"}
        self.run = None
        if read_config is None or generated is None:
            raise ValueError("Calibration requires an isolated project config and generated directory")
        self.read_config = read_config
        self.generated = Path(generated).resolve()
        self.calibration_root = self.generated / "calibration"
        self.latest = self.calibration_root / "latest.json"
        self.receipt_path = Path(receipt_path or self.generated / "calibration-current.json")
        self.publish = publish

    def snapshot(self):
        revision = self.read_model()["revision"]
        with self.state_lock:
            state, run = deepcopy(self.state), self.run
        if state["status"] in ("running", "failed"):
            return {**state, "revision": revision}
        if run is None and self.latest.is_file():
            candidate = Path(json.loads(self.latest.read_text())["run"]).resolve()
            if candidate.is_relative_to(self.calibration_root) and (candidate / "run.json").is_file():
                run = candidate
        return describe_run(run, revision, generated=self.generated, receipt_path=self.receipt_path) if run else {"status": "idle", "revision": revision}

    def _review(self, data, run):
        plan = json.loads((run / "plan.json").read_text())
        packet = json.loads((run / "evidence/packet.json").read_text())
        allowed = {}
        # Only hash-bound rendered original-PDF artifacts count as local evidence.
        for item in packet.get("pages", []):
            path = run / "evidence" / item["path"]
            allowed[artifact_url(path, self.generated)] = {**item, "path": str(path)}
        for measure in packet.get("measures", []):
            for item in [measure.get("crop"), measure.get("systemContext"),
                         *[staff.get("crop") for staff in measure.get("staffContexts", [])]]:
                if item:
                    path = run / "evidence" / item["path"]
                    allowed[artifact_url(path, self.generated)] = {**item, "path": str(path)}
        decisions = deepcopy(data.get("decisions"))
        if not isinstance(decisions, list) or not decisions:
            raise ValueError("请提供至少一项校对决策")
        for decision in decisions:
            evidence = decision.get("evidence", [])
            if not isinstance(evidence, list):
                raise ValueError("证据必须是本次原谱审核包中的链接列表")
            local = []
            for url in evidence:
                if not isinstance(url, str):
                    raise ValueError("证据必须是本次原谱审核包中的链接")
                parsed = urlsplit(url)
                key = unquote(parsed.path)
                if parsed.scheme or parsed.netloc or parsed.query or key not in allowed:
                    raise ValueError("只接受本次扫描已绑定哈希的原谱图像链接")
                local.append(allowed[key])
            decision["evidence"] = local
        return {"schemaVersion": 1, "sourceSha256": plan["sourceSha256"], "revision": plan["revision"],
                "planSha256": content_hash(plan), "reviewer": "workbench-source-review", "decisions": decisions}

    def start(self, data):
        if data.get("revision") != self.read_model()["revision"]:
            raise ValueError("曲谱版本已经改变，请刷新并重新扫描")
        action = data.get("action")
        if action not in ("scan", "apply"):
            raise ValueError("action必须为scan或apply")
        snapshot = self.snapshot()
        run = self.calibration_root / str(data.get("runId", ""))
        review = None
        if action == "apply":
            if self.publish is None:
                raise ValueError("Applying requires an isolated preview publisher")
            if (snapshot.get("runId") != data.get("runId") or not snapshot.get("canApply")
                    or not run.is_dir() or run.resolve().parent != self.calibration_root.resolve()):
                raise ValueError("此校准记录已过期或已应用，请重新扫描")
            review = self._review(data, run)
        if not self.mutation_lock.acquire(blocking=False):
            raise RuntimeError("正在生成或校准，请稍后再试")
        with self.state_lock:
            self.state = {"status": "running", "revision": data["revision"], "action": action}

        def execute():
            try:
                if action == "scan":
                    self.calibration_root.mkdir(parents=True, exist_ok=True)
                    output = self.calibration_root / datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S%fZ")
                    result = scan(self.read_config(), output, expected_revision=data["revision"])
                    completed_run = Path(result["output"])
                else:
                    result = apply_review(run, review)
                    if self.publish is not None and result.get("bundle"):
                        self.publish(result, run)
                        result["status"] = "published-preview"
                        write_json(run / "application.json", result)
                    completed_run = run
                write_json(self.latest, {"run": str(completed_run)})
                with self.state_lock:
                    self.run = completed_run
                    self.state = {"status": "ready"}
            except Exception as error:
                with self.state_lock:
                    self.state = {"status": "failed", "error": str(error), "revision": data["revision"]}
            finally:
                self.mutation_lock.release()
        threading.Thread(target=execute, daemon=True, name="score-calibration").start()
        return {"status": "running", "revision": data["revision"], "action": action}
