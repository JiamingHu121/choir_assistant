"""Register external projects and index immutable bundles without rebuilding them."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile


IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    result = sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(data)
    return result.hexdigest()


def safe_id(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("Work/movement ID must contain 1-128 ASCII letters, digits, underscores or hyphens")
    return value


def resolve(value, base):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else Path(base) / path).resolve()


def active_bundle(movement, library):
    project = resolve(movement["project"], library)
    if project.is_dir():
        project = project / "project.json"
    if movement.get("bundle"):
        base = resolve(movement["bundle"], library)
    else:
        state = read_json(project.parent / "run-state.json")
        if not state.get("latestBundle"):
            raise ValueError("Project has no completed bundle; finish the authoring workflow first")
        base = resolve(state["latestBundle"], project.parent)
    pointer = project.parent / "preview/current.json"
    if pointer.is_file():
        state = read_json(pointer)
        if state.get("baseBundle") == str(base) and state.get("configPath") == str(project):
            return resolve(state["bundle"], project.parent), project
    return base, project


def source_pdf(bundle, project, entry, library):
    manifest = read_json(bundle / "manifest.json")
    frozen = manifest.get("sourcePdf")
    if frozen:
        relative = Path(frozen["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Frozen source PDF path escapes its bundle")
        path = (bundle / relative).resolve()
        try:
            path.relative_to(bundle.resolve())
        except ValueError:
            raise ValueError("Frozen source PDF path escapes its bundle")
        indexed = {item["path"]: item.get("sha256") for item in manifest.get("artifacts", [])}
        if indexed.get(relative.as_posix()) != frozen.get("sha256"):
            raise ValueError("Frozen source PDF hash differs from artifact manifest")
        expected = frozen["sha256"]
    elif entry.get("sourcePdf") and entry.get("sourceSha256"):
        # Explicit hash-bound compatibility for pre-snapshot library entries.
        path = resolve(entry["sourcePdf"], library)
        expected = entry["sourceSha256"]
    else:
        raise ValueError("Bundle needs a frozen sourcePdf, or an explicit hash-bound legacy library entry")
    if path.is_symlink() or not path.is_file() or digest(path) != expected:
        raise ValueError("Original PDF SHA-256 mismatch")
    return path, expected


def entries(library_root):
    library = Path(library_root).expanduser().resolve()
    records = {}
    # `works` is the portable format; the legacy folder remains read compatible.
    for folder in ("works", "作品"):
        for path in sorted((library / folder).glob("*/entry.json")):
            entry = read_json(path)
            ident = safe_id(entry.get("id"))
            if ident != path.parent.name or ident in records:
                raise ValueError("Duplicate work ID or ID differs from entry directory")
            movements = entry.get("movements")
            if not isinstance(movements, list) or not movements:
                raise ValueError("A registered work needs at least one movement")
            mids = [safe_id(m.get("id")) for m in movements]
            if len(set(mids)) != len(mids):
                raise ValueError("Duplicate movement ID")
            records[ident] = entry
    if not records:
        raise ValueError("Library has no entries; use `library add --project PROJECT --id SCORE` first")
    return records


def register_project(library_root, project, work_id, movement_id="main", title=None,
                     composer="", overwrite=False, movement_title=None):
    """Atomically register a ready project pointer outside the plugin installation.

    Registration never copies or rebuilds music. Export resolves its latest
    completed bundle again, so later reviewed revisions synchronize naturally.
    """
    library = Path(library_root).expanduser().resolve()
    plugin_root = Path(__file__).resolve().parents[2]
    try:
        library.relative_to(plugin_root)
    except ValueError:
        pass
    else:
        raise ValueError("Library must stay outside the installed plugin")
    work_id, movement_id = safe_id(work_id), safe_id(movement_id)
    project = Path(project).expanduser().resolve()
    if project.is_dir():
        project = project / "project.json"
    movement = {"id": movement_id, "title": movement_title or "全曲", "project": str(project)}
    bundle, _ = active_bundle(movement, library)
    manifest = read_json(bundle / "manifest.json")
    if manifest.get("status") not in ("complete", "complete-with-open-issues"):
        raise ValueError("Only completed authoring bundles can be registered")
    model = read_json(bundle / "assets/interactive.json")
    if model.get("revision") != manifest.get("revision"):
        raise ValueError("Bundle model/manifest revision mismatch")
    source_pdf(bundle, project, {}, library)
    path = library / "works" / work_id / "entry.json"
    entry = read_json(path) if path.is_file() else {"schemaVersion": 1, "id": work_id,
             "title": title or model.get("title", work_id), "composer": composer, "movements": []}
    if (library / "作品" / work_id / "entry.json").exists():
        raise ValueError("This ID already belongs to a legacy library entry")
    previous = next((m for m in entry["movements"] if m["id"] == movement_id), None)
    if previous and not overwrite:
        raise ValueError("Movement already registered; pass --overwrite to replace its project pointer")
    if title:
        entry["title"] = title
    if composer:
        entry["composer"] = composer
    entry["movements"] = [m for m in entry["movements"] if m["id"] != movement_id] + [movement]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".entry-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(entry, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        Path(temporary).replace(path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
    return {"status": "registered", "entry": str(path), "work": work_id,
            "movement": movement_id, "revision": model["revision"]}


SCHEMA = """
CREATE TABLE works(id TEXT PRIMARY KEY,title TEXT,source_pdf TEXT,source_sha256 TEXT,status TEXT,entry_json TEXT);
CREATE TABLE movements(work_id TEXT,id TEXT,title TEXT,revision TEXT,bundle TEXT,project TEXT,duration_seconds REAL,quarter_bpm REAL,score_status TEXT,validation_json TEXT,PRIMARY KEY(work_id,id));
CREATE TABLE parts(work_id TEXT,movement_id TEXT,source_part TEXT,role TEXT,name TEXT,payload_json TEXT,PRIMARY KEY(work_id,movement_id,source_part));
CREATE TABLE measures(work_id TEXT,movement_id TEXT,measure_index INTEGER,printed_number TEXT,meter TEXT,onset_ql REAL,duration_ql REAL,start_seconds REAL,end_seconds REAL,payload_json TEXT,PRIMARY KEY(work_id,movement_id,measure_index));
CREATE TABLE symbols(work_id TEXT,movement_id TEXT,id TEXT,kind TEXT,measure_index INTEGER,printed_number TEXT,part TEXT,source_part TEXT,staff INTEGER,voice TEXT,pitch_midi INTEGER,pitch_name TEXT,offset_ql REAL,duration_ql REAL,duration_numerator INTEGER,duration_denominator INTEGER,start_seconds REAL,end_seconds REAL,notation_json TEXT,payload_json TEXT,PRIMARY KEY(work_id,movement_id,id));
CREATE TABLE relations(work_id TEXT,movement_id TEXT,kind TEXT,id TEXT,from_id TEXT,to_id TEXT,status TEXT,payload_json TEXT);
CREATE INDEX symbols_lookup ON symbols(work_id,movement_id,part,measure_index);
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
"""


def encode(value):
    return json.dumps(public_projection(value), ensure_ascii=False, separators=(",", ":"))


def public_projection(value):
    """Keep host filesystem paths out of all downloadable index JSON fields."""
    if isinstance(value, dict):
        return {key: public_projection(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_projection(item) for item in value]
    if isinstance(value, str) and (value.startswith("file://") or re.match(r"^[A-Za-z]:[\\/]", value)
                                   or (value.startswith("/") and not value.startswith(("/assets/", "/original/", "/generated/", "/api/")))):
        return "[authoring-path-redacted]"
    return value


def movement_summary(movement, manifest, model):
    validation = manifest.get("validation", {})
    tempo = model.get("tempoMetadata", {})
    return {"id": movement["id"], "title": movement.get("title", "全曲"), "revision": model["revision"],
            "duration": model["duration"], "notes": len(model["notes"]), "rests": len(model["rests"]),
            "measures": len(model["measures"]), "parts": list(dict.fromkeys(p["part"] for p in model["parts"])),
            "quarterBpm": tempo.get("initialQuarterBpm"), "tempoInferred": tempo.get("inferred"),
            "scoreStatus": validation.get("scoreStatus", "unreviewed"),
            "sourceReviewed": movement.get("revision") == model["revision"] and movement.get("sourceReviewStatus") == "whole-score-source-reviewed-corrected",
            "blockingMeasures": len(model.get("rhythmValidation", {}).get("blockingMeasureIndices", [])),
            "openIssues": validation.get("openIssues", len(model.get("audit", {}).get("issues", [])))}


def write_index(destination, catalog, release, models):
    """Write a fresh public index from the same in-memory bundle snapshot.

    Host project paths and authoring configs are deliberately absent. The DB and
    catalog are generated together rather than trusting a stale private index.
    """
    with closing(sqlite3.connect(str(destination))) as db:
        db.executescript(SCHEMA)
        for work in catalog["works"]:
            wid = work["id"]
            public = release["works"][wid]
            db.execute("INSERT INTO works VALUES(?,?,?,?,?,?)", (wid, work["title"], work["sourcePdf"], public["sourceSha256"], work["reviewStatus"], encode(work)))
            for movement in work["movements"]:
                mid = movement["id"]
                model, manifest = models[(wid, mid)]
                released = public["movements"][mid]
                db.execute("INSERT INTO movements VALUES(?,?,?,?,?,?,?,?,?,?)", (wid, mid, movement["title"], model["revision"], released["bundle"], None, model["duration"], movement["quarterBpm"], movement["scoreStatus"], encode(manifest.get("validation", {}))))
                for part in model["parts"]:
                    db.execute("INSERT INTO parts VALUES(?,?,?,?,?,?)", (wid, mid, part["sourcePart"], part["part"], part.get("name"), encode(part)))
                for measure in model["measures"]:
                    db.execute("INSERT INTO measures VALUES(?,?,?,?,?,?,?,?,?,?)", (wid, mid, measure["index"], str(measure["number"]), (measure.get("timeSignature") or {}).get("text"), measure.get("onset"), measure.get("duration"), measure.get("start"), measure.get("end"), encode(measure)))
                for note in model["notes"] + model["rests"]:
                    rational = note.get("rational", {}).get("duration", {})
                    db.execute("INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (wid, mid, note["id"], note.get("kind", "note"), note["measureIndex"], str(note["measure"]), note["part"], note["sourcePart"], note.get("staff"), str(note.get("voice", "")), note.get("pitch"), note.get("pitchName"), note.get("offset"), note["duration"], rational.get("numerator"), rational.get("denominator"), note.get("start"), note.get("end"), encode(note.get("notation", {})), encode(note)))
                for kind, relations in model.get("relations", {}).items():
                    for relation in relations:
                        db.execute("INSERT INTO relations VALUES(?,?,?,?,?,?,?,?)", (wid, mid, kind, relation.get("id"), relation.get("from"), relation.get("to"), relation.get("status"), encode(relation)))
        db.execute("INSERT INTO metadata VALUES(?,?)", ("catalog", encode(catalog)))
        db.commit()
    Path(destination).chmod(0o444)
