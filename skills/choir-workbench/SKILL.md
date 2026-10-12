---
name: choir-workbench
description: Import a PDF choral score, calibrate its structured notes against the original pages, and generate or continue an interactive score/audio rehearsal workbench with metronome and pitch practice. Use for score-to-rehearsal requests; ordinary PDF reading or audio transcription does not need this skill.
---

# Choir workbench

Complete the whole-score workflow and open the result. Keep recognition, source review, rendering, and sung-pitch validation as distinct evidence. A generated workbench is usable as a draft even when some musical details remain uncertain.

## Locate and prepare

Resolve the plugin root from this file: it is two directories above this skill folder. Never use the caller's working directory to find plugin resources. Keep the installed plugin read-only; choose a writable run directory in the user's workspace and a separate writable runtime/cache. Reuse an existing run when the user asks to continue it. Do not overwrite another score's project.

Read [setup](../../SETUP.md) when dependencies are missing. Use `scripts/doctor.py --json`, and bootstrap the pinned Python environment with `scripts/bootstrap.py --venv /writable/runtime` when needed. The actual score engines and SoundFont are external dependencies; missing dependencies must be resolved or reported specifically, not replaced with simulated OMR or synthesis.

Use the runtime's `choir-assistant` executable, or its Python interpreter with `/plugin/root/scripts/choir_assistant.py`. Run `--help` for current flags. The commands below use `choir-assistant` as shorthand for that resolved executable.

## Import through the complete pipeline

```sh
choir-assistant import /absolute/score.pdf --output /writable/score-run --no-open
choir-assistant status --project /writable/score-run/project.json
```

The import preserves the source PDF, caches Audiveris recognition by input identity, inspects recognized parts, builds canonical MusicXML/SVG/MIDI/audio, and scans all measures. It creates `project.json`, `run-state.json`, independent bundles, and a calibration run. Resolve paths from those records, not from an assumed directory name.

If mapping is needed, inspect `source-inspection.json`, the original staff labels, and `part-map.template.json`. Fill explicit source IDs with `S`, `A`, `T`, `B`, or `Piano`; never assign generic Voice 1/2 to singing roles by position alone. When the source gives an unambiguous mapping, proceed. Ask only if an essential musical mapping remains ambiguous; finish independent recognition/evidence work while waiting. Resume with `run --project ... --part-map /absolute/mapping.json`. Use `--score-index` only after inspecting multiple exported scores.

Inspect original tempo marks and their beat unit. A textual tempo estimate stays labeled as inferred. Review octave/transpose directions, tuplets, ties versus phrase slurs, meter changes, and repeat/endings. Do not infer a performance route or a metric-modulation ratio without source evidence.

## Source review and calibration

Read [workflow and review contract](../../docs/WORKFLOW.md) for state paths, review JSON, and transaction gates. First inspect the entire `evidence/source-review-queue.json` and original PDF images without candidate edits. Then compare with `plan.json` and `model-before.json`. Cover every flagged measure and record which additional measures were read; a scan of all measures does not certify them all.

Use independent agent review when a dense or ambiguous passage benefits from it. Give the reviewer original crops, source IDs, and the source-blind queue before showing proposed edits. Ask for explicit pitch, written type/dots, performed duration, voice/staff, chord simultaneity, tuplet ratios/display numbers, ties/slurs, and uncertainty. Review in bounded page/system chunks until the entire queue is covered. Without a subagent, follow the same source-first procedure directly.

Music theory locates conflicts; it does not manufacture truth. A unique beat-filling solution, repeated pattern, OMR beam graph, or smooth harmony is a proposal until verified against the original. Preserve legitimate pickups, sparse voices, forward gaps, polymeter, and chords. If an unsupported operation is needed, retain the unresolved issue and draft status rather than shortening/deleting by proxy.

Save original-PDF hash-bound findings using the review contract. Then create a new frozen scan and accept its source-verified edits:

```sh
choir-assistant scan --project /writable/score-run/project.json --source-review /absolute/findings.json
choir-assistant apply --run /absolute/new-frozen-calibration-run --accept-source-reviewed --project /writable/score-run/project.json
```

For candidate-by-candidate decisions use `apply --run ... --review /absolute/decisions.json --project ...` and the generated template. Accepted corrections rebuild the complete downstream bundle. Continue unresolved parts in draft; do not silently certify them or stop after one illustrative passage.

## Verify and open

Check the selected bundle manifest and actual assets: input hashes, stable source IDs, complete expected SVG note coverage, canonical/performance events, MIDI/WAV timing, and clipping report. For reviewed tuplets, check actual engraving timing and displayed ratios as well as MIDI; correct audio alone does not prove a correctly spaced score. Spot-check representative repaired and unchanged passages against the source.

```sh
choir-assistant serve --project /writable/score-run/project.json
```

Open the returned loopback URL with the available browser tool. Verify score visibility, selection/voice filtering, a short playback selection, and the metronome/control tabs. Keep the source-PDF view available for draft scores. If browser tools are unavailable, provide the URL and clearly state which UI behavior was not exercised.

Pitch practice compares a selected part or single note with microphone/uploaded singing. The antiphonal mode plays other chosen parts as a background and uses interference-aware scoring. These are experimental: no physical microphone/speaker accuracy claim follows from fixture tests, and ambiguous unison/harmonic interference may be unscored. Do not request microphone capture unless the user's practice task requires it.

Deliver the workbench URL, run/project path, source-review coverage, remaining issues, and validation actually performed. v0.1 still requires source review for false extra symbols, octave-direction scopes, mixed-meter relations, and repeat routes; see [known limits](../../docs/WORKFLOW.md#known-limits).
