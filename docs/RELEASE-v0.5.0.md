# Choir Assistant v0.5.0

Adds version-bound manual correction in the shared score workbench and an
optional independently provisioned authoring worker.

- Select notes, shift them by semitones, set six note values and dots, inspect
  original-to-draft differences, undo/redo and audition simple tones immediately.
- Generate durable score/audio previews before a separate publication action.
  All corresponding work-password holders can publish, continue work-scoped
  tasks and explicitly restore a historical version as a new head. The original
  musical baseline remains restorable across later software/library updates.
- Natural-language requests first produce a typed proposal. Confirmation,
  preview generation and publication remain distinct actions. Unsupported complex
  requests remain `needs_input`.
- Pinned musical heads, base-value checks and publication compare-and-swap
  prevent old sessions from silently overwriting newer music.
- Optional HTTPS pull validates complete immutable online snapshots and switches
  the actual local score bundle before sync. Missing/stale watermarks fail closed;
  `--check-only` never downloads. Private revision/manifest bindings prevent an
  old project rebuild from inheriting an incorporated-head watermark. Original
  PDF bytes and pre-pull backups remain.
- Range-checked snapshot downloads tolerate interrupted public transfers while
  retaining head, length and artifact checks. Existing measure-bound lyric
  sidecars survive compatible edits through verified copied bindings; changed
  musical addresses require review rather than silently discarding lyrics.
- Exports include fully hash-indexed source modules for a separate unprivileged
  worker. Explicit local Node/Verovio WASM paths support engraving where the
  native module is unavailable. Runtime binaries, private configuration and
  user data remain external.

See [manual correction and synchronization](MANUAL_CORRECTION.md) for semantics,
worker boundaries and private profile setup, and [validation](VALIDATION.md) for
actual target acceptance. Existing full-score scan/source review remains available.
These checks do not establish whole-score OMR accuracy or physical microphone
performance. Operator provisioning remains separate from plugin installation.
