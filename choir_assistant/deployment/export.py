"""Export an external score library as a standalone, verified rehearsal website.

Recognition, corrections, and engraving never run on the site. Optional original
PDF rendering occurs offline only, using the bundle-frozen PDF.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import urlencode


from . import catalog as library_catalog

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = Path(__file__).with_name("static")
UI_FILES = {
    "index.html", "entry.js", "entry.css", "calibration_ui.js", "calibration_ui.css",
    "app.js", "style.css", "interactive.js", "interactive.css", "console.js",
    "console.css", "metronome.js", "pitch_core.js", "pitch.js", "pitch.css",
    "pitch-worklet.js", "pitch-worker.js", "pitch-reference-worklet.js",
}
REQUIRED_UI = UI_FILES - {"pitch-worklet.js", "pitch-reference-worklet.js"}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    result = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def source_path(value, library):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else library / path).resolve()


def safe_relative(value):
    if not isinstance(value, str) or not value:
        raise ValueError("An indexed artifact needs a relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe artifact path: {value}")
    return path


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ValueError(f"Unsafe work or movement ID: {value!r}")
    return value


def copy_verified(source, destination, expected=None):
    source, destination = Path(source), Path(destination)
    if not source.is_file() or source.is_symlink():
        raise ValueError(f"Source is missing or is a symlink: {source}")
    actual = digest(source)
    if expected is not None and actual != expected:
        raise ValueError(f"Source SHA-256 mismatch: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if digest(destination) != actual:
        raise ValueError(f"Copied SHA-256 mismatch: {destination}")
    return actual


def copy_bundle(source, destination):
    manifest = read_json(source / "manifest.json")
    if manifest.get("status") not in {"complete", "complete-with-open-issues"}:
        raise ValueError(f"Bundle is incomplete: {source}")
    indexed = {}
    for item in manifest.get("artifacts", []):
        relative = safe_relative(item.get("path"))
        name = relative.as_posix()
        if name in indexed:
            raise ValueError(f"Duplicate indexed artifact: {name}")
        file = source / relative
        if not file.resolve().is_relative_to(source.resolve()) or not file.is_file():
            raise ValueError(f"Indexed artifact escapes or is missing: {name}")
        if digest(file) != item.get("sha256"):
            raise ValueError(f"Bundle artifact SHA-256 mismatch: {name}")
        indexed[name] = item["sha256"]
    for file in (source / "assets").rglob("*"):
        if file.is_file() and file.relative_to(source).as_posix() not in indexed:
            raise ValueError(f"Unindexed bundle asset: {file}")
    model = read_json(source / "assets/interactive.json")
    if model.get("revision") != manifest.get("revision"):
        raise ValueError(f"Bundle model/manifest revision mismatch: {source}")
    # Copy only declared immutable artifacts and the manifest. Scratch outputs,
    # local configs and recognition caches do not become website resources.
    for name, expected in sorted(indexed.items()):
        copy_verified(source / name, destination / name, expected)
    copy_verified(source / "manifest.json", destination / "manifest.json")
    return manifest, model, {"indexedArtifacts": len(indexed), "copiedFiles": len(indexed) + 1}


def copy_original(project, source_hash, destination, model, pdf=None, prepare=False, preparation_root=None):
    candidates = sorted((project.parent / "preview/original").glob("*/render-manifest.json"))
    matching = [(path, read_json(path)) for path in candidates
                if read_json(path).get("sourceSha256") == source_hash]
    if not matching and prepare:
        # Rendering is an explicit offline preparation step; authoring bundles
        # and the source project cache remain unchanged.
        from workbench.calibration_evidence import render_pdf
        if not pdf:
            raise ValueError("Original PDF is required for offline page preparation")
        temporary = Path(preparation_root) / source_hash
        rendered = render_pdf(pdf, temporary)
        matching = [(temporary / "render-manifest.json", rendered)]
    if not matching:
        raise ValueError("No verified original-PDF page cache; rerun site export with --prepare-original (requires pdftoppm)")
    last_error = None
    for manifest_path, rendered in matching:
        try:
            pages = rendered.get("pages", [])
            if not pages:
                raise ValueError("Original-PDF cache has no pages")
            # Validate the entire candidate before publishing any of its pages.
            for index, page in enumerate(pages, 1):
                relative = safe_relative(page["path"])
                file = manifest_path.parent / relative
                if page.get("page") != index or not file.resolve().is_relative_to(manifest_path.parent.resolve()):
                    raise ValueError("Original-PDF page numbering/path is invalid")
                if not file.is_file() or file.is_symlink() or digest(file) != page.get("sha256"):
                    raise ValueError(f"Original-PDF PNG SHA-256 mismatch: {file}")
                if not all(isinstance(page.get(key), (int, float)) and page[key] > 0 for key in ("width", "height")):
                    raise ValueError("Original-PDF geometry is invalid")
            break
        except (KeyError, OSError, ValueError) as error:
            last_error = error
    else:
        raise ValueError(f"No intact original-PDF cache: {last_error}")
    geometry = model.get("pages", [])
    aligned = len(pages) == len(geometry) and all(
        abs(page["width"] / page["height"] - locator["width"] / locator["height"]) <= .025
        for page, locator in zip(pages, geometry))
    projected, exported = [], []
    for index, page in enumerate(pages):
        filename = f"page-{page['page']:03d}.png"
        copy_verified(manifest_path.parent / page["path"], destination / filename, page["sha256"])
        locator = geometry[index] if aligned else page
        projected.append({"page": page["page"], "width": locator["width"], "height": locator["height"],
                          "image": "/original/" + filename})
        exported.append({**page, "path": filename})
    write_json(destination / "render-manifest.json", {
        "schemaVersion": 1, "sourceSha256": source_hash, "dpi": rendered.get("dpi"),
        "renderer": rendered.get("renderer"), "pages": exported,
    })
    return projected, "OMR-page-geometry" if aligned else "unavailable"


def score_projection(model, manifest, pages, alignment, source_hash):
    groups = list(dict.fromkeys(part["part"] for part in model["parts"]))
    labels = {"T": "男高音", "B": "男低音", "S": "女高音", "A": "女低音", "Piano": "钢琴"}
    return {
        "title": model["title"], "subtitle": " · ".join(labels.get(group, group) for group in groups),
        "pages": pages, "measures": model["measures"], "duration": model["duration"], "groups": groups,
        "audio": model["audio"], "revision": model["revision"], "interactive": True,
        "sourceAlignment": alignment,
        "sourcePdfBinding": {"status": "release-sha256-verified", "sha256": source_hash},
        "playbackSettings": model.get("playbackSettings", manifest["config"]["playback"]),
        "rhythmValidation": model["rhythmValidation"],
    }


def download_url(work, movement, kind):
    return "/api/download?" + urlencode({"work": work, "movement": movement, "type": kind})


def public_report_text(text):
    """Redact local locations in prose while retaining external HTTPS sources."""
    # Reports are a separate public view, not the canonical review evidence.
    # Absolute paths may contain spaces: quoted/inline-code paths are matched as
    # a unit; unquoted paths stop at whitespace to avoid deleting adjacent prose.
    text = re.sub(r"([`\"'])(?:file://)?/(?:Users|home|private|tmp|Volumes|var|opt)/[^`\"'\n]*\1",
                  "[authoring-path-redacted]", text)
    text = re.sub(r"(?<![A-Za-z0-9:/])(?:file://)?/(?:Users|home|private|tmp|Volumes|var|opt)/[^\s<>`\"']+",
                  "[authoring-path-redacted]", text)
    return re.sub(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/](?!/)[^\s<>`\"']+", "[authoring-path-redacted]", text)


def export_site(output, library_root, plugin_root=None, prepare_original=False):
    """Freeze a complete site, atomically publish locally, and verify every file.

    Repeated exports of unchanged inputs are byte-identical. The input library
    only supplies external entry pointers; neither plugin nor site owns it.
    """
    library = Path(library_root).expanduser().resolve()
    plugin = Path(plugin_root or PLUGIN_ROOT).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    if output.exists():
        raise ValueError("Export destination must be a new directory")
    for forbidden in (plugin, library / "works", library / "作品"):
        try:
            output.relative_to(forbidden)
        except ValueError:
            pass
        else:
            raise ValueError("Export destination must stay outside frozen scores and plugin code")
    records = library_catalog.entries(library)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".site-export-", dir=output.parent))
    preparation = Path(tempfile.mkdtemp(prefix=".pdf-prepare-", dir=output.parent))
    try:
        for name in ("index.html", "app.js", "app.css"):
            copy_verified(STATIC_ROOT / name, staging / name)
        copy_verified(Path(__file__).with_name("site_server.py"), staging / "site_server.py")
        (staging / "requirements.txt").write_text("mido==1.3.3\npackaging==24.2\n", encoding="utf-8")
        missing = sorted(name for name in REQUIRED_UI if not (plugin / "workbench" / name).is_file())
        if missing:
            raise ValueError("Shared workbench UI is incomplete: " + ", ".join(missing))
        for name in sorted(UI_FILES):
            if (plugin / "workbench" / name).is_file():
                copy_verified(plugin / "workbench" / name, staging / "ui/workbench" / name)
        for name in ("playback.py", "validation.py"):
            copy_verified(plugin / "workbench" / name, staging / "runtime_modules" / name)
        release = {"schemaVersion": 1, "works": {}}
        catalog = {"schemaVersion": 1, "works": []}
        evidence = {"status": "passed", "works": {}, "frozenBundlesUnmodified": True,
                    "publicModel": "authoring paths redacted by HTTP projection; frozen source bytes preserved"}
        models, expected_revisions = {}, {}
        for wid, entry in sorted(records.items()):
            wid = safe_id(wid)
            base = Path("data") / wid
            released_work = {"id": wid, "title": entry["title"], "sourcePdf": (base / "source.pdf").as_posix(),
                             "reviewReport": None, "movements": {}}
            release["works"][wid] = released_work
            work = {"id": wid, "title": entry["title"], "composer": entry.get("composer", ""),
                    "status": "ready", "reviewStatus": entry.get("status", "draft-needs-review"),
                    "movements": [], "parts": [], "notes": 0, "rests": 0, "measures": 0,
                    "duration": 0, "blockingMeasures": 0, "openIssues": 0}
            catalog["works"].append(work)
            selected_report = entry.get("reviewReport") or next((m.get("reviewReport") for m in entry["movements"] if m.get("reviewReport")), None)
            report_path = None
            if selected_report:
                report = source_path(selected_report, library)
                canonical_report = base / ("review-source" + (report.suffix.lower() or ".txt"))
                report_hash = copy_verified(report, staging / canonical_report)
                if report.suffix.lower() in (".md", ".txt", ".json", ".log", ".rst"):
                    report_path = base / "review-public.txt"
                    text = report.read_text(encoding="utf-8")
                    (staging / report_path).write_text(
                        "Public review projection; local authoring paths are redacted.\n"
                        + "Canonical review SHA-256: " + report_hash + "\n\n" + public_report_text(text), encoding="utf-8")
                    released_work["reviewReport"] = report_path.as_posix()
            evidence["works"][wid] = {"movements": {}}
            pdf_hash = None
            for source_movement in entry["movements"]:
                mid = safe_id(source_movement["id"])
                source_bundle, project = library_catalog.active_bundle(source_movement, library)
                # Export cannot be nested in any selected project or bundle:
                # this also avoids recursive traversal of its own staging data.
                for frozen in (source_bundle, project.parent):
                    try:
                        output.relative_to(frozen)
                    except ValueError:
                        pass
                    else:
                        raise ValueError("Export must stay outside authoring project directories")
                destination = base / mid
                manifest, model, checks = copy_bundle(source_bundle, staging / destination / "bundle")
                pdf, source_hash = library_catalog.source_pdf(source_bundle, project, entry, library)
                if pdf_hash is not None and source_hash != pdf_hash:
                    raise ValueError("Movements in one work must share the same original PDF")
                if pdf_hash is None:
                    copy_verified(pdf, staging / base / "source.pdf", source_hash)
                    pdf_hash = source_hash
                    released_work["sourceSha256"] = source_hash
                    evidence["works"][wid]["sourceSha256"] = source_hash
                pages, alignment = copy_original(project, source_hash, staging / destination / "original", model,
                                                 pdf=pdf, prepare=prepare_original, preparation_root=preparation)
                write_json(staging / destination / "score.json", score_projection(model, manifest, pages, alignment, source_hash))
                released_work["movements"][mid] = {
                    "id": mid, "title": source_movement.get("title", "全曲"), "revision": model["revision"],
                    "bundle": (destination / "bundle").as_posix(),
                    "projection": (destination / "score.json").as_posix(),
                    "original": (destination / "original").as_posix(),
                }
                movement = library_catalog.movement_summary(source_movement, manifest, model)
                movement["reviewReport"] = download_url(wid, mid, "review") if report_path else None
                work["movements"].append(movement)
                for key in ("notes", "rests", "measures", "duration", "blockingMeasures", "openIssues"):
                    work[key] += movement[key]
                work["parts"] = list(dict.fromkeys(work["parts"] + movement["parts"]))
                evidence["works"][wid]["movements"][mid] = {
                    **checks, "revision": model["revision"], "sourcePages": len(pages),
                    "sourceAlignment": alignment, "notes": len(model["notes"]), "rests": len(model["rests"]),
                }
                models[(wid, mid)] = (model, manifest)
                expected_revisions[(wid, mid)] = model["revision"]
            first_mid = work["movements"][0]["id"]
            work["sourcePdf"] = download_url(wid, first_mid, "pdf")
            work["sourceSha256"] = pdf_hash
            work["reviewReport"] = download_url(wid, first_mid, "review") if report_path else None
            work["sourceReviewed"] = all(m["sourceReviewed"] for m in work["movements"])
        catalog["totals"] = {"works": len(catalog["works"]), "ready": len(catalog["works"]),
                             **{key: sum(w[key] for w in catalog["works"]) for key in ("notes", "rests", "measures")}}
        library_catalog.write_index(staging / "library.sqlite3", catalog, release, models)
        write_json(staging / "catalog.json", catalog)
        write_json(staging / "release.json", release)
        evidence["totals"] = catalog["totals"]
        write_json(staging / "export-verification.json", evidence)
        hashes = [{"path": file.relative_to(staging).as_posix(), "bytes": file.stat().st_size,
                   "sha256": digest(file)} for file in sorted(staging.rglob("*")) if file.is_file()]
        write_json(staging / "file-manifest.json", {"schemaVersion": 1, "files": hashes})
        staging.rename(output)
        return {"output": str(output), "works": len(release["works"]), "files": len(hashes) + 1,
                "bytes": sum(file.stat().st_size for file in output.rglob("*") if file.is_file()),
                "revisions": {wid + "/" + mid: revision for (wid, mid), revision in expected_revisions.items()},
                "hashValidation": "passed", "fileManifestSha256": digest(output / "file-manifest.json")}
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(preparation, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New output directory (must not exist)")
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--plugin", type=Path)
    parser.add_argument("--prepare-original", action="store_true", help="Offline-render missing original pages using pdftoppm")
    args = parser.parse_args(argv)
    print(json.dumps(export_site(args.output, args.library, args.plugin, args.prepare_original), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
