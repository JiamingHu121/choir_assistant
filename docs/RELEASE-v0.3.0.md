# Choir Assistant v0.3.0

Publish completed score projects as an independent library website, then update
that site with one profile-driven command. Local recognition and calibration do
not depend on a website or a hosting account.

- `library add` registers a completed score project while preserving its review status.
- `site export` produces a deterministic, hash-verified standalone website with original PDF pages, score interaction and selection synthesis.
- `site sync` transfers changed files, stages a verified immutable release, activates only an owned user service, checks local/public health and restores the previous release on activation failure.
- `site check`, `site sync --check-only` and `site rollback` support inspection and recovery. Repeating an unchanged sync transfers no files.
- Public JSON, SQLite and text reports omit authoring paths. The serving runtime bounds connections, reads, render duration/rate and generated cache storage.

Profiles, domain routing, SSH credentials, score data and runtime dependencies are
external. The current activation adapter updates an existing macOS user
LaunchAgent; it does not provision accounts, DNS, tunnels or native audio tools.
See [deployment](DEPLOYMENT.md) for initial setup and security boundaries.

The original public 29-note study was exported, served in an isolated Python
process without the plugin installed, and rendered by real FluidSynth to a
non-silent 2.85-second selection. Export and standalone reports accompany this
release. Recognition accuracy and physical microphone performance were not
retested by this hosting change. The prior source-review limitations remain.

Validation: 169 Python and 65 JavaScript tests passed, as did the skill validator,
package checks, and a fresh wheel installation outside the source directory.
An independent receiver without authoring packages served all 26 checked HTTP
resources. A separate existing five-work deployment passed incremental activation
and unchanged-sync checks; its private content and destination are not shipped.
See [validation evidence](release-validation-v0.3.0.json).
