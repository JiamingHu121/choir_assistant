"""Serve an isolated workflow bundle using the same interactive workbench.

The immutable bundle stays unchanged. Source-page renders, selection audio,
calibration runs, and the active bundle pointer belong to the preview project.
"""
from copy import deepcopy
import json
from pathlib import Path

from workbench.workflow import file_hash, load_config, write_json


class BundlePreview:
    def __init__(self, bundle, config_path, generated=None):
        self.config_path = Path(config_path).resolve()
        requested = load_config(self.config_path)
        self.generated = Path(generated or self.config_path.parent / "preview").resolve()
        from workbench.workflow import ROOT
        if self.generated.is_relative_to(ROOT):
            raise ValueError("Preview generated files must stay outside the installed plugin")
        self.generated.mkdir(parents=True, exist_ok=True)
        self.base_bundle = Path(bundle).resolve()
        self.pointer = self.generated / "current.json"
        self.receipt = self.generated / "calibration-current.json"
        selected = self.base_bundle
        if self.pointer.is_file():
            pointer = json.loads(self.pointer.read_text())
            if pointer.get("baseBundle") == str(self.base_bundle) and pointer.get("configPath") == str(self.config_path):
                selected = Path(pointer["bundle"]).resolve()
        self._load(selected, requested)
        self._render_source_pages()

    def _load(self, bundle, requested=None):
        manifest = json.loads((bundle / "manifest.json").read_text())
        if manifest.get("status") not in ("complete", "complete-with-open-issues"):
            raise ValueError("Preview requires a completed workflow bundle")
        assets = bundle / "assets"
        model_path = assets / "interactive.json"
        indexed = {}
        for item in manifest.get("artifacts", []):
            name = item.get("path") if isinstance(item, dict) else None
            if not isinstance(name, str) or not name:
                raise ValueError("Bundle artifact requires a relative path")
            relative = Path(name)
            path = bundle / relative
            if relative.is_absolute() or ".." in relative.parts or not path.resolve().is_relative_to(bundle.resolve()):
                raise ValueError(f"Bundle artifact path is outside its bundle: {name}")
            key = relative.as_posix()
            if key in indexed:
                raise ValueError(f"Duplicate bundle artifact path: {name}")
            if not path.is_file() or file_hash(path) != item.get("sha256"):
                raise ValueError(f"Preview artifact changed, missing or is unverified: {name}")
            indexed[key] = item
        for path in assets.rglob("*"):
            if path.is_file():
                item = indexed.get(path.relative_to(bundle).as_posix())
                if not item:
                    raise ValueError(f"Preview artifact changed or is unverified: {path.name}")
        frozen_pdf = None
        source_pdf_binding = {"status": "unknown"}
        if "sourcePdf" in manifest:
            pdf = manifest["sourcePdf"]
            name = pdf.get("path") if isinstance(pdf, dict) else None
            item = indexed.get(name) if isinstance(name, str) else None
            if not item or not name.lower().endswith(".pdf") or pdf.get("sha256") != item["sha256"]:
                raise ValueError("Frozen source PDF metadata differs from the verified artifact index")
            frozen_pdf = bundle / name
            source_pdf_binding = {"status": "bundle-sha256-verified", "sha256": pdf["sha256"]}
        model = json.loads(model_path.read_text())
        if model.get("revision") != manifest.get("revision"):
            raise ValueError("Bundle manifest and score revision disagree")
        config = manifest["config"]
        if requested:
            # The bundle may already contain a reviewed ledger. All other
            # project identities must still agree with the selected config.
            for key in ("title", "partMap", "mergeLogicalParts", "tempoEvidence", "sourcePdf", "soundfont", "playback", "rhythmPolicy"):
                if requested.get(key) != config.get(key):
                    raise ValueError(f"Preview config differs from bundle: {key}")
            for key in ("musicxml", "omr", "pdf"):
                if requested["input"].get(key) != config["input"].get(key):
                    raise ValueError(f"Preview source differs from bundle: {key}")
        self.bundle, self.assets, self.config = bundle, assets, deepcopy(config)
        self.source_pdf = frozen_pdf
        self.source_pdf_binding = source_pdf_binding

    def _render_source_pages(self):
        self.pages = []
        self.source_alignment = "unavailable"
        source = self.source_pdf or self.config.get("sourcePdf") or self.config["input"].get("pdf")
        if not source:
            return
        from workbench.calibration_evidence import render_pdf
        directory = self.generated / "original" / file_hash(source)
        rendered = render_pdf(source, directory)
        if (self.source_pdf and rendered.get("sourceSha256") != self.source_pdf_binding["sha256"]):
            raise ValueError("Frozen source PDF changed during rendering")
        model = self.read_model()
        geometry = model.get("pages", [])
        aligned = len(rendered["pages"]) == len(geometry) and all(
            abs(page["width"] / page["height"] - locator["width"] / locator["height"]) <= 0.025
            for page, locator in zip(rendered["pages"], geometry))
        self.source_alignment = "OMR-page-geometry" if aligned else "unavailable"
        # The original PDF is still readable without OMR geometry. Never invent
        # note positions or claim its pages align to an unrelated export.
        for index, page in enumerate(rendered["pages"]):
            locator = geometry[index] if aligned else page
            self.pages.append({"page": page["page"], "width": locator["width"], "height": locator["height"],
                               "image": "/generated/" + (directory / page["path"]).relative_to(self.generated).as_posix()})

    def read_model(self):
        return json.loads((self.assets / "interactive.json").read_text())

    def read_config(self):
        return deepcopy(self.config)

    def score_projection(self):
        model = self.read_model()
        groups = list(dict.fromkeys(part["part"] for part in model["parts"]))
        return {"title": model["title"], "subtitle": " · ".join({"T": "男高音", "B": "男低音", "S": "女高音", "A": "女低音", "Piano": "钢琴"}.get(group, group) for group in groups),
                "pages": self.pages, "measures": model["measures"], "duration": model["duration"], "groups": groups,
                "audio": model["audio"], "revision": model["revision"], "interactive": True,
                "sourceAlignment": self.source_alignment,
                "sourcePdfBinding": deepcopy(self.source_pdf_binding),
                "playbackSettings": model.get("playbackSettings", self.config["playback"]),
                "rhythmValidation": model["rhythmValidation"]}

    def publish(self, result, run=None):
        """Switch only this preview after a successfully validated rebuild."""
        bundle = Path(result["bundle"]).resolve()
        if not bundle.is_relative_to(self.generated):
            raise ValueError("A preview rebuild must stay inside its project output directory")
        proposed = json.loads((bundle / "assets/interactive.json").read_text())
        if proposed.get("revision") != result["revision"]:
            raise ValueError("Reviewed revision and preview bundle disagree")
        self._load(bundle, self.config)
        write_json(self.pointer, {"baseBundle": str(self.base_bundle), "configPath": str(self.config_path),
                                 "bundle": str(bundle), "revision": result["revision"]})
        if run:
            write_json(self.receipt, {**result, "run": str(run)})

    def correct_note(self, data):
        import math
        import uuid
        from workbench.workflow import build, read_corrections
        model = self.read_model()
        lookup = {note["id"]: note for note in model["notes"]}
        ident = data.get("noteId")
        if ident not in lookup:
            raise ValueError("该音符不存在，请刷新")
        pitch, duration = data.get("pitch", lookup[ident]["pitch"]), data.get("duration", lookup[ident]["duration"])
        if type(pitch) is not int or not 0 <= pitch <= 127:
            raise ValueError("MIDI音高应在0–127")
        if type(duration) not in (int, float) or not math.isfinite(duration) or not 0.03125 <= duration <= 16:
            raise ValueError("时值应在0.03125–16个四分拍")
        reason = str(data.get("reason", "")).strip()[:500]
        if not reason:
            raise ValueError("请填写原谱校对依据")
        ledger = read_corrections(self.config.get("corrections"))
        ledger[ident] = {**ledger.get(ident, {}), "pitch": pitch, "duration": duration, "reason": reason}
        output = self.generated / "manual" / uuid.uuid4().hex[:12]
        output.mkdir(parents=True)
        write_json(output / "corrections.json", ledger)
        config = {**self.config, "corrections": str(output / "corrections.json"), "reviewFile": None}
        manifest = build(config, output / "rebuilt")
        self.publish({"bundle": str(output / "rebuilt"), "revision": manifest["revision"]})
        return {"revision": manifest["revision"], "model": "/assets/interactive.json", "reload": True}
