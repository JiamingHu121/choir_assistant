"""Import a score, audit every measure, and serve an isolated rehearsal workbench."""
from __future__ import annotations
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import webbrowser

INSTALL_ROOT = Path(__file__).resolve().parents[1]


def stamp(prefix):
    return datetime.now(timezone.utc).strftime(prefix + "-%Y%m%dT%H%M%S%fZ")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    from workbench.workflow import write_json as save
    save(path, value)


def external_path(path):
    path = Path(path).expanduser().resolve()
    if path.is_relative_to(INSTALL_ROOT):
        raise ValueError("Outputs must stay outside the installed plugin; use a project workspace directory")
    return path


def project_paths(project):
    project = external_path(project)
    if project.is_dir():
        project = project / "project.json"
    return project, project.parent / "run-state.json", project.parent / "preview"


def inspect_source(source):
    from workbench.score_model import _read_source
    from workbench.workflow import recognized_part_role, file_hash
    root, xml = _read_source(Path(source))
    from hashlib import sha256
    parts = []
    for part in root.findall("part-list/score-part"):
        ident, name = part.get("id"), part.findtext("part-name", part.get("id"))
        notes = root.find(f"part[@id='{ident}']")
        parts.append({"sourcePart": ident, "name": name, "recognizedRole": recognized_part_role(name),
                      "instrumentNames": [item.text for item in part.findall("score-instrument/instrument-name")],
                      "measures": len(notes.findall("measure")) if notes is not None else 0})
    return {"schemaVersion": 1, "musicxml": str(Path(source).resolve()), "sourceFileSha256": file_hash(source),
            "sourceSha256": sha256(xml).hexdigest(), "title": root.findtext("work/work-title") or root.findtext("movement-title"),
            "parts": parts, "mappingPolicy": "Explicit recognized source names only; source IDs never imply voice roles"}


def load_part_map(path):
    if not path:
        return {}
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValueError("--part-map must contain a source-ID to S/A/T/B/Piano JSON object")
    from workbench.workflow import GROUPS
    if any(not isinstance(key, str) or item not in GROUPS for key, item in value.items()):
        raise ValueError("Every part-map value must be S, A, T, B or Piano")
    return value


def active_project(project):
    from workbench.workflow import load_config
    project, state_path, generated = project_paths(project)
    state = read_json(state_path)
    config = load_config(project)
    if state.get("latestBundle"):
        from workbench.preview import BundlePreview
        preview = BundlePreview(state["latestBundle"], project, generated)
        config = preview.read_config()
    return project, state_path, generated, state, config


def initialize(args):
    from workbench.workflow import load_config, file_hash
    from workbench.calibrate import resolve_inputs
    from runtime import data_dir
    pdf = Path(args.pdf).expanduser().resolve()
    if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
        raise ValueError("import requires an existing PDF score")
    output = external_path(args.output or data_dir() / "projects" / stamp("score"))
    if output.exists():
        raise ValueError(f"Output already exists: {output}; resume with run --project {output / 'project.json'}")
    output.mkdir(parents=True)
    inputs = output / "inputs"
    inputs.mkdir()
    snapshot = inputs / "original.pdf"
    shutil.copy2(pdf, snapshot)
    config_path = output / "project.json"
    raw = {"schemaVersion": 1, "input": {"pdf": str(snapshot)}, "rhythmPolicy": "draft", "mergeLogicalParts": False,
           "sourcePdf": str(snapshot), "partMap": load_part_map(args.part_map),
           "playback": {"transpose": 0, "program": 52, "gains": {}, "a4": 440}}
    if args.score_index is not None:
        raw["input"]["scoreIndex"] = args.score_index
    if args.soundfont:
        raw["soundfont"] = str(Path(args.soundfont).expanduser().resolve())
    if args.title:
        raw["title"] = args.title
    write_json(config_path, raw)
    state = {"schemaVersion": 1, "status": "recognizing", "project": str(config_path), "inputPdf": str(pdf),
             "inputPdfSha256": file_hash(pdf), "createdAt": datetime.now(timezone.utc).isoformat()}
    write_json(output / "run-state.json", state)
    try:
        config = load_config(config_path)
        source, omr, _ = resolve_inputs(config)
        copied_source = inputs / ("source" + Path(source).suffix.lower())
        shutil.copy2(source, copied_source)
        copied_omr = None
        if omr:
            copied_omr = inputs / "source.omr"
            shutil.copy2(omr, copied_omr)
        config["input"] = {"musicxml": str(copied_source), "omr": str(copied_omr) if copied_omr else None, "scoreIndex": None}
        if not config.get("title"):
            config["title"] = inspect_source(copied_source).get("title") or pdf.stem
        write_json(config_path, config)
        state.update(status="recognized", sourceMusicxml=str(copied_source), sourceOmr=str(copied_omr) if copied_omr else None)
        write_json(output / "run-state.json", state)
    except Exception as error:
        state.update(status="failed", failedStage="recognition", error=str(error))
        write_json(output / "run-state.json", state)
        raise
    args.project = str(config_path)
    return execute(args)


def execute(args):
    from workbench.workflow import build, load_config
    from workbench.calibrate import resolve_inputs, scan
    project, state_path, generated = project_paths(args.project)
    state = read_json(state_path)
    config = load_config(project)
    if args.part_map:
        config["partMap"] = load_part_map(args.part_map)
        write_json(project, config)
        state["status"] = "recognized"
        state.pop("latestBundle", None)
    if config["input"].get("pdf"):
        # Recognition failures resume from the immutable hash cache.
        source, omr, pdf = resolve_inputs(config)
        inputs = project.parent / "inputs"
        copied_source = inputs / ("source" + Path(source).suffix.lower())
        shutil.copy2(source, copied_source)
        copied_omr = inputs / "source.omr" if omr else None
        if omr:
            shutil.copy2(omr, copied_omr)
        config["input"] = {"musicxml": str(copied_source), "omr": str(copied_omr) if omr else None, "scoreIndex": None}
        config["sourcePdf"] = pdf
        if not config.get("title"):
            config["title"] = inspect_source(copied_source).get("title") or Path(state["inputPdf"]).stem
        write_json(project, config)
    inspection = inspect_source(config["input"]["musicxml"])
    write_json(project.parent / "source-inspection.json", inspection)
    known = {part["sourcePart"] for part in inspection["parts"]}
    if set(config["partMap"]) - known:
        raise ValueError("part-map contains source IDs absent from source-inspection.json")
    unknown = [part for part in inspection["parts"] if not config["partMap"].get(part["sourcePart"]) and not part["recognizedRole"]]
    if unknown:
        template = {part["sourcePart"]: config["partMap"].get(part["sourcePart"]) or part["recognizedRole"] for part in inspection["parts"]}
        write_json(project.parent / "part-map.template.json", template)
        state.update(status="needs-part-map", unknownParts=unknown,
                     nextStep=f"Inspect original.pdf and source-inspection.json; fill part-map.template.json, then run --project {project} --part-map MAPPING.json")
        write_json(state_path, state)
        return state
    state.pop("unknownParts", None)
    state.pop("nextStep", None)
    try:
        if state.get("status") != "ready" or not state.get("latestBundle"):
            generated.mkdir(parents=True, exist_ok=True)
            calibration = generated / "calibration" / stamp("run")
            state.update(status="scanning")
            write_json(state_path, state)
            scanned = scan(config, calibration)
            write_json(generated / "calibration/latest.json", {"run": str(calibration)})
            state.update(status="building", latestCalibration=str(calibration), calibrationSummary=scanned["summary"])
            write_json(state_path, state)
            bundle = project.parent / "bundles" / stamp("bundle")
            manifest = build(config, bundle)
            state.update(status="ready", latestBundle=str(bundle), revision=manifest["revision"], bundleStatus=manifest["status"],
                         validation=manifest["validation"], scoreStatus=manifest["validation"]["scoreStatus"])
            state.pop("error", None)
            state.pop("failedStage", None)
            write_json(state_path, state)
        if not args.no_serve:
            serve_project(project, args.port, args.no_open)
            state["url"] = f"http://127.0.0.1:{args.port}/"
        return state
    except Exception as error:
        state.update(status="failed", failedStage=state["status"], error=str(error))
        write_json(state_path, state)
        raise


def serve_project(project, port=8765, no_open=False):
    from workbench.start import ensure_server
    project, state_path, generated = project_paths(project)
    state = read_json(state_path)
    if not state.get("latestBundle"):
        raise ValueError("The project has no completed bundle; run --project first")
    ensure_server(port, INSTALL_ROOT, timeout=60, bundle=state["latestBundle"], config=project, generated=generated)
    url = f"http://127.0.0.1:{port}/"
    if not no_open:
        webbrowser.open(url)
    return {"status": "ready", "url": url, "project": str(project)}


def scan_project(args):
    from workbench.calibrate import scan
    project, state_path, generated, state, config = active_project(args.project)
    run = generated / "calibration" / stamp("run")
    evidence = read_json(args.source_review) if args.source_review else None
    result = scan(config, run, evidence=evidence)
    write_json(generated / "calibration/latest.json", {"run": str(run)})
    state.update(latestCalibration=str(run), calibrationSummary=result["summary"])
    write_json(state_path, state)
    return {key: value for key, value in result.items() if key != "candidates"}


def apply_project(args):
    from workbench.calibrate import apply_review, source_review_decisions, model_from_config, resolve_inputs
    from workbench.workflow import file_hash
    run = external_path(args.run)
    if args.project:
        project, state_path, generated = project_paths(args.project)
        if not run.is_relative_to(generated / "calibration"):
            raise ValueError("The calibration run is outside this project's preview/calibration directory")
        # Use the active preview's ledger after UI reviews, rather than the
        # project's older base configuration. Reject stale or foreign runs
        # before apply_review writes decisions, ledgers, models or audio.
        project, state_path, generated, state, config = active_project(project)
        record = read_json(run / "run.json")
        plan = read_json(run / "plan.json")
        before = read_json(run / "model-before.json")
        current = model_from_config(config)
        if any(item.get("revision") != current["revision"] for item in (record, plan, before)):
            raise ValueError("Calibration run differs from the active project source, configuration or ledger; rescan")
        source, omr, pdf = resolve_inputs(config)
        active_sources = {label: {"path": str(Path(path).resolve()), "sha256": file_hash(path)}
                          for label, path in (("musicxml", source), ("omr", omr), ("pdf", pdf)) if path}
        if active_sources != record.get("sources"):
            raise ValueError("Calibration run source files differ from the active project; rescan")
    review = source_review_decisions(run) if args.accept_source_reviewed else args.review
    result = apply_review(run, review)
    if args.project and result.get("bundle"):
        state = read_json(state_path)
        manifest = read_json(Path(result["bundle"]) / "manifest.json")
        write_json(project, manifest["config"])
        state.update(status="ready", latestBundle=result["bundle"], revision=result["revision"], latestCalibration=str(run),
                     validation=manifest["validation"], bundleStatus=manifest["status"])
        write_json(state_path, state)
        write_json(generated / "calibration-current.json", {**result, "run": str(run)})
    return {key: value for key, value in result.items() if key != "edits"}


def rebuild_project(args):
    from workbench.workflow import build
    project, state_path, generated, state, config = active_project(args.project)
    bundle = project.parent / "bundles" / stamp("bundle")
    manifest = build(config, bundle)
    write_json(project, config)
    state.update(status="ready", latestBundle=str(bundle), revision=manifest["revision"], bundleStatus=manifest["status"], validation=manifest["validation"])
    write_json(state_path, state)
    return {"status": "ready", "bundle": str(bundle), "revision": manifest["revision"], "validation": manifest["validation"]}


def verify_project(args):
    from workbench.source_verification import verify_bundle
    if args.bundle:
        bundle = Path(args.bundle).expanduser().resolve()
    else:
        project, state_path, generated = project_paths(args.project)
        bundle_path = read_json(state_path).get("latestBundle")
        if not bundle_path:
            raise ValueError("The project has no completed bundle to verify")
        bundle = Path(bundle_path).resolve()
        # A workbench review may have selected a newer immutable bundle without
        # changing the CLI's base pointer. Match the preview's identity guards.
        pointer_path = generated / "current.json"
        if pointer_path.is_file():
            pointer = read_json(pointer_path)
            if pointer.get("baseBundle") == str(bundle) and pointer.get("configPath") == str(project):
                bundle = Path(pointer["bundle"]).resolve()
    facts = Path(args.facts).expanduser().resolve() if args.facts else None
    report = verify_bundle(bundle, facts)
    if args.output:
        output = external_path(args.output)
        if output.is_relative_to(bundle) or output == facts:
            raise ValueError("Verification output must not overwrite the frozen bundle or source facts")
        write_json(output, report)
    return report


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    initial = commands.add_parser("import", help="PDF → OMR → whole-score evidence → draft audio/score workbench")
    initial.add_argument("pdf")
    initial.add_argument("--output")
    initial.add_argument("--soundfont")
    initial.add_argument("--title")
    initial.add_argument("--score-index", type=int)
    initial.add_argument("--part-map")
    resume = commands.add_parser("run", help="Resume recognition/mapping/build stages in an existing project")
    resume.add_argument("--project", required=True)
    resume.add_argument("--part-map")
    for command in (initial, resume):
        command.add_argument("--port", type=int, default=8765)
        command.add_argument("--no-open", action="store_true")
        command.add_argument("--no-serve", action="store_true", help="Build artifacts without starting a server")
    server = commands.add_parser("serve", help="Start/reuse the project's loopback server")
    server.add_argument("--project", required=True)
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--no-open", action="store_true")
    scanner = commands.add_parser("scan", help="Create a fresh hash-bound whole-score source review run")
    scanner.add_argument("--project", required=True)
    scanner.add_argument("--source-review", help="Original-PDF-bound per-symbol agent findings JSON")
    apply = commands.add_parser("apply", help="Validate accepted decisions and rebuild an isolated bundle")
    apply.add_argument("--run", required=True)
    decisions = apply.add_mutually_exclusive_group(required=True)
    decisions.add_argument("--review")
    decisions.add_argument("--accept-source-reviewed", action="store_true")
    apply.add_argument("--project", help="Switch this project's bundle pointer to the reviewed result")
    rebuild = commands.add_parser("rebuild")
    rebuild.add_argument("--project", required=True)
    verifier = commands.add_parser("verify", help="Independently verify bundle semantics and optional source-frozen staff-bar facts")
    target = verifier.add_mutually_exclusive_group(required=True)
    target.add_argument("--bundle")
    target.add_argument("--project")
    verifier.add_argument("--facts", help="Revision/hash-bound facts transcribed from original score images")
    verifier.add_argument("--output", help="Save the JSON report outside the frozen bundle")
    doctor = commands.add_parser("doctor", help="Check external runtime tools and soundfont")
    doctor.add_argument("--json", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("--project", required=True)
    library = commands.add_parser("library", help="Register completed projects in an external score library")
    library_commands = library.add_subparsers(dest="library_command", required=True)
    add = library_commands.add_parser("add", help="Register a project without changing its score or review status")
    add.add_argument("--library", required=True)
    add.add_argument("--project", required=True)
    add.add_argument("--id", required=True)
    add.add_argument("--movement", default="main")
    add.add_argument("--title")
    add.add_argument("--composer", default="")
    add.add_argument("--overwrite", action="store_true")
    site = commands.add_parser("site", help="Export, serve or synchronize a standalone score-library website")
    site_commands = site.add_subparsers(dest="site_command", required=True)
    export = site_commands.add_parser("export", help="Verify and freeze a website locally; no network operation")
    export.add_argument("--library", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--prepare-original", action="store_true", help="Render missing original PDF page caches offline")
    hosted = site_commands.add_parser("serve", help="Run an exported website without authoring or OMR services")
    hosted.add_argument("--root", required=True)
    hosted.add_argument("--runtime", required=True)
    hosted.add_argument("--bind", default="127.0.0.1")
    hosted.add_argument("--port", type=int, default=8780)
    hosted.add_argument("--fluidsynth")
    hosted.add_argument("--soundfont")
    hosted.add_argument("--origin", action="append", default=[])
    for name in ("sync", "check", "rollback"):
        remote = site_commands.add_parser(name, help={"sync": "Export and atomically synchronize an independently configured website", "check": "Read-only verification of the configured website", "rollback": "Restore the previous website release"}[name])
        remote.add_argument("--profile", required=True, help="External deployment profile; never store credentials in the plugin")
        remote.add_argument("--ready", action="store_true", help="Acknowledge an already confirmed host readiness requirement")
        if name == "sync":
            remote.add_argument("--check-only", action="store_true", help="Plan and verify without remote writes or restart")
    return result


def deployment_command(args):
    """Load optional publishing code only when explicitly requested."""
    if args.command == "library":
        from choir_assistant.deployment.catalog import register_project
        return register_project(external_path(args.library), external_path(args.project), args.id,
                                movement_id=args.movement, title=args.title,
                                composer=args.composer, overwrite=args.overwrite)
    if args.site_command == "export":
        from choir_assistant.deployment.export import export_site
        return export_site(external_path(args.output), external_path(args.library),
                           prepare_original=args.prepare_original)
    if args.site_command == "serve":
        from choir_assistant.deployment.site_server import main as serve_site
        options = ["--root", str(external_path(args.root)), "--runtime", str(external_path(args.runtime)),
                   "--bind", args.bind, "--port", str(args.port)]
        for flag in ("fluidsynth", "soundfont"):
            if getattr(args, flag):
                options += ["--" + flag, getattr(args, flag)]
        for origin in args.origin:
            options += ["--origin", origin]
        return serve_site(options)
    from choir_assistant.deployment.sync import sync_site, check_site, rollback_site
    profile = external_path(args.profile)
    if args.site_command == "sync":
        return sync_site(profile, check_only=args.check_only, ready=args.ready)
    if args.site_command == "check":
        return check_site(profile, ready=args.ready)
    return rollback_site(profile, ready=args.ready)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            from runtime import doctor_main
            return doctor_main(["--json"] if args.json else [])
        if hasattr(args, "port") and not 1 <= args.port <= 65535:
            raise ValueError("--port must be between 1 and 65535")
        if args.command == "import": result = initialize(args)
        elif args.command == "run": result = execute(args)
        elif args.command == "serve": result = serve_project(args.project, args.port, args.no_open)
        elif args.command == "scan": result = scan_project(args)
        elif args.command == "apply": result = apply_project(args)
        elif args.command == "rebuild": result = rebuild_project(args)
        elif args.command == "verify": result = verify_project(args)
        elif args.command in ("library", "site"): result = deployment_command(args)
        else:
            project, state_path, generated, state, config = active_project(args.project)
            result = {**state, "activeCorrectionLedger": config.get("corrections")}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.command == "verify" and not result["passed"] else 0
    except Exception as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
