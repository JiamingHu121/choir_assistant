---
name: choir-workbench
description: Import and source-review a PDF choral score, generate an interactive rehearsal workbench, or publish completed score projects to an independently configured library website. Use for score-to-rehearsal or choir-library synchronization requests; ordinary PDF reading or audio transcription does not need this skill.
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

Music theory locates conflicts; it does not manufacture truth. A unique beat-filling solution, repeated pattern, OMR beam graph, or smooth harmony is a proposal until verified against the original. Preserve legitimate pickups, sparse voices, forward gaps, polymeter, and chords. When a field ledger cannot express a source-confirmed repair, use a separately traced derived-source retranscription: materialize accepted corrections first, keep the original and old ledgers, publish old-to-new identities, and rebuild all outputs. Until that source reading is confirmed, keep the unresolved issue and draft status; do not shorten/delete by proxy or replay old ordinals against reordered symbols.

Save original-PDF hash-bound findings using the review contract. Then create a new frozen scan and accept its source-verified edits:

```sh
choir-assistant scan --project /writable/score-run/project.json --source-review /absolute/findings.json
choir-assistant apply --run /absolute/new-frozen-calibration-run --accept-source-reviewed --project /writable/score-run/project.json
```

For candidate-by-candidate decisions use `apply --run ... --review /absolute/decisions.json --project ...` and the generated template. Accepted corrections rebuild the complete downstream bundle. Continue unresolved parts in draft; do not silently certify them or stop after one illustrative passage.

## Verify and open

Check the selected bundle manifest and actual assets: input hashes, stable source IDs, complete expected SVG note coverage, canonical/performance events, MIDI/WAV timing, and clipping report. For reviewed tuplets, check actual engraving timing and displayed ratios as well as MIDI; correct audio alone does not prove a correctly spaced score. Spot-check representative repaired and unchanged passages against the source.

For octave directions and mid-measure clef changes, check the actual MEI printed
and sounding octaves and the exact scope endpoints, including the following
out-of-scope note. Check `engraving-normalization.json` for unresolved octave
offsets. Source-frozen pitch samples must agree with the input, canonical XML,
model, and rendered score; complete beat totals are not a source pitch check.

Run the independent bundle check, then compare source-first frozen facts for the
reviewed staff-bars. Read [the fact contract](../../docs/SOURCE_REVIEW.md) when
creating those records; bind the final revision, XML/model hashes, and frozen PDF
hash. A selected-region match does not certify unselected measures. New bundles
include the original PDF snapshot; older bundles can have unknown PDF binding.

```sh
choir-assistant verify --project /writable/score-run/project.json --output /writable/consistency.json
choir-assistant verify --project /writable/score-run/project.json --facts /writable/source-facts.json --output /writable/source-check.json
```

```sh
choir-assistant serve --project /writable/score-run/project.json
```

Open the returned loopback URL with the available browser tool. Verify all SVG pages, selection/voice filtering, a real short playback selection, and the metronome/control tabs. Switch from interactive score to PDF and back repeatedly; confirm the toolbar still receives clicks. A failed SVG must show a retry while successful pages remain visible. Keep the source-PDF view available for draft scores. If browser tools are unavailable, provide the URL and clearly state which UI behavior was not exercised.

Pitch practice compares a selected part or single note with microphone/uploaded singing. The antiphonal mode plays other chosen parts as a background and uses interference-aware scoring. These are experimental: no physical microphone/speaker accuracy claim follows from fixture tests, and ambiguous unison/harmonic interference may be unscored. Do not request microphone capture unless the user's practice task requires it.

Deliver the workbench URL, run/project path, source-review coverage, remaining issues, and validation actually performed. v0.2 still requires source review for false extra symbols, octave-direction scopes, mixed-meter relations, and repeat routes; see [known limits](../../docs/WORKFLOW.md#known-limits).

## Optional library website

When the user asks to publish a library or synchronize new scores, read
[deployment](../../docs/DEPLOYMENT.md). Register the completed project with
`library add --library ... --project ... --id ...`, then run
`site sync --profile /absolute/private/site-profile.json` against the authorized
destination. Read `publisherMode` before proceeding. A user LaunchAgent sync
exports and verifies the library, transfers changed files, activates only its
owned service, and restores the old deployment on activation failure.
`isolated-manual` only stages a complete verified release under the destination's
incoming directory and returns `pendingAdminActivation`; report the pending state
and administrator command rather than claiming activation. Never invoke the
isolation migration rollback as a score-release rollback, add sudoers access or
read service secrets to bypass that boundary. Review the receipt and actual
public browser flow before claiming delivery. Use `--check-only` for a plan with
no remote mutation. Honor a host's existing readiness rule; `--ready` records an
already satisfied confirmation, not a new authorization.

Keep the deployment profile, keys, site data and runtime outside the plugin.
The website runs a frozen standalone serving package and never calls this skill.
Optional online correction queues typed tasks for a separately provisioned
unprivileged worker; the HTTP process does not run recognition or authoring. Domain and gateway setup are separate
from sync; don't silently repoint DNS or reuse a personal research SSH account.
Report which machines remain in the traffic/credential path when discussing
isolation. A successful deploy does not establish full-score recognition or
physical microphone accuracy.

For optional website passwords, branding and feedback, keep `site-config.json`
private and outside exports; use the deployment credential generator's getpass
prompt or private password file, never a literal password argument. Verify final
score/PDF/audio endpoints reject unauthenticated direct access, not just that the
homepage opens a password dialog. Feedback stays in the private server inbox;
creating email or GitHub Issues is a separate user-authorized action.

If printed lyrics are absent or garbled in recognized MusicXML, read the original
pages and save a revision/PDF-bound notation sidecar as described in deployment.
Bind text to canonical measures, parts and original-page evidence; do not guess
syllable-to-note alignment or rewrite accepted musical durations to add lyrics.
Check later playback, rests, tied tails, paused seeks and PDF-to-score returns.


## Selected-note correction and online edits

For manual workbench corrections, natural-language requests, task recovery or
history restoration, read [manual correction](../../docs/MANUAL_CORRECTION.md).
Semitone/value controls first change a browser draft. Quick simple-tone audition
keeps original onsets; the server preview performs the real rhythm rebuild.
Preserve the distinct draft, preview, ready-to-publish and published states.
On a stale worker heartbeat, retain and poll the existing durable task rather
than treating queue time as progress or submitting duplicates.
Require the submitter's explicit difference confirmation for a natural-language
proposal and their separate publish action. Every corresponding work-password
holder may publish and restore music; do not introduce administrator approval.
A shared session is an authorization/audit handle, not a verified personal name.

Before pushing a library with online edits, let configured `editorSync` pull and
validate complete score snapshots and update actual bundle pointers. Never
manually advance `.online-editor-heads.json` over old music or bypass a stale-head
failure. `--check-only` compares without downloading. Keep the password in an
owner-only private file, not chat text, a URL, a shell argument or the repository. Report
the pull backup and publication receipts separately. Optional isolated worker,
Node/WASM engine and supervisor provisioning are independent operator setup;
routine sync must not execute release modules as root or change that setup.
