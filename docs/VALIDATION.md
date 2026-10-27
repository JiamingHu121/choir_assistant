# Plugin package validation

## v0.6.1 — publication completion state

Regressions follow queued preview, ready preview, a new queued publication job
and the final published state. They check the complete draft binding, mutation
freeze, correct success summary, reload action and preservation of a separate
draft when opening another saved publication. See the
[patch receipt](release-validation-v0.6.1.json).

## v0.6.0 — batch publication and community interface

Regressions cover three separate selections in one publication, rejection of an
outdated preview, natural-language conflict handling, independent cards,
first-use guidance and manual reopening, anonymous messages, text-only rendering,
CSRF/origin boundaries, bounded storage, and private-inbox separation. See the
[structured receipt](release-validation-v0.6.0.json) for completed checks and their
scope; release history below retains earlier acceptance evidence.

## v0.5.1 — portable transfer regression

The Linux runner exposed a kernel-dependent slow-socket fixture in v0.5.0.
The corrected regression explicitly rejects the legacy whole-body write and
accepts the complete chunked transfer through a deterministic timed writer;
a separate real HTTP test verifies complete bytes and SHA. Production code is
unchanged. See [patch notes](RELEASE-v0.5.1.md) and the
[patch receipt](release-validation-v0.5.1.json).

## v0.5.0 — regression and deployment acceptance

The final repository suites passed **319 Python tests** and **91 JavaScript
tests**. A regular wheel was built with the declared build backend, installed
outside the source checkout, and checked for its authoring and web resources.
The package scan excludes private scores, operator tools, runtime binaries,
passwords and machine-specific paths.

The new selection editor passes the JavaScript suite, including draft isolation,
semitone bounds, six note values/dots, preview endpoints, undo/redo, typed
requests, explicit natural-language confirmation, stale previews and reload
context checks. Filesystem and HTTP tests cover source-only module export,
manifest integrity, content-head protection, authenticated snapshot export/import,
actual local bundle replacement, immutable original PDFs, backups, private
password files, no redirects, bounded extraction, commit/ledger/artifact hashes
and check-only behavior. Cross-module validation consumes the real editor export
with the independent pull verifier.

Target acceptance also exercised the real isolated worker: source-bound pitch
correction, repeated semitone respelling, rhythm/dot rebuilding, SVG/MIDI/WAV/MP3
generation and real preview delivery. A configured local model produced a typed
proposal; explicit confirmation generated a separate unpublished preview.
Temporary music publication survived a website release update; the original
pinned baseline remained available and was restored as a new history head.

Large snapshots exposed two real transport failures: a single large socket write
could truncate despite download progress, and long public transfers could end
early. Bounded server writes and fixed-head HTTP range downloads now have real
HTTP regressions for slow readers, premature EOF, bounded retry, rate limits,
changed totals and publication conflicts. Download completion and all artifact
hashes remain prerequisites for updating local bundles and sync watermarks.

Final public synchronization and zero-change repeat checks passed. Original
music, notation, timing and text matched the pre-test model after restoration.
The source plugin retains compatible reviewed lyric sidecars through verified
copied bindings; mismatched musical addresses fail before switching library
entries or watermarks. Original sidecar bytes remain untouched.
See the [structured release receipt](release-validation-v0.5.0.json).
These checks do not certify general OMR accuracy, unsupported
natural-language edits or physical microphone/speaker performance.

## v0.4.0 — package regression

On 2026-10-16, the final full suites passed: **191 Python tests** and
**71 JavaScript tests**. They include 8 real HTTP authorization/feedback cases,
4 private-configuration provisioning cases, 9 isolated staging/inspection cases,
20 legacy publishing cases, 13 export/runtime cases and the canonical-index
regression for pickups and repeated printed measure numbers.

Local browser acceptance opened a password-protected score, played the original
full audio from a late measure across page boundaries into the tied tail, and
returned from PDF view to the correctly highlighted final measure. Branding,
project link, measure labels, lyrics and feedback context were inspected.
These observations do not establish that OS account migration or public CDN
authorization has been completed; those are separate deployment acceptance steps.

HTTP tests exercise direct resource denials, per-work isolation, wrong/tampered/
expired credentials, browser-bound CSRF, Origin checks, Secure/HttpOnly/SameSite
cookies, login limits, private feedback receipts/plain-text storage and quotas,
internal health authentication, and protected lyric sidecars. Filesystem tests
verify full incoming releases from incremental transfers, unchanged active files,
root-owned state requirements, manifest identity, compare-and-swap checks, locks,
corrupt transfers, private incoming directories, mode dispatch and unsupported
migration rollback. The staging fixture never starts/stops a real service.

Source package checks scan public code, docs and examples for private machine
paths and unexpected score/audio/binary assets. All four package/plugin versions
must agree. Versioned ZIP creation packages only repository-owned code and the
original public Practice Study fixture. Public-site deployment and isolated OS
provisioning remain separate acceptance steps; they are not proved by a package
ZIP, unit tests or a changed plugin version.

## v0.2.0 — historical acceptance

Validated on 2026-10-15: **137 Python tests** and **65 JavaScript tests** passed.
The updated skill passed its frontmatter validator; actual Codex discovery
reported version `0.2.0` as available, without installing or enabling it.
Four metadata versions agree, and the source package scan excludes private
scores, recordings, generated runs, binaries and machine-specific paths.

A regular wheel was built from a temporary source copy, installed into a separate
writable target, and invoked from outside the repository using the existing
pinned Python runtime dependencies. `pip check` passed. The original public
Practice Study MusicXML and its PDF generated 29 notes, eight measures, 29/29
SVG note coverage, and 20 seconds of MIDI/WAV with zero clipped samples. This
acceptance did not rerun Audiveris recognition.

The installed CLI passed independent artifact checks, followed the active
workbench bundle pointer, and matched four staff-bar facts frozen from the
public author MusicXML before generating. It rejected an incorrect octave,
stale evidence and an output path overwriting its source facts. Replacing the
external PDF did not change verification of the frozen original. Separate real
Poppler acceptance also rendered the frozen PDF after the external path became
invalid. Deletion/tampering of declared assets, ambiguous grace/octave endpoints,
and sequential XML units are covered by synthetic regressions.

Three local, private score bundles passed the independent parser over 6,723
structured objects. Their old PDF binding is correctly reported as not frozen;
no historical bundle was rewritten, and no private material is in this release.

[Structured v0.2.0 results](release-validation-v0.2.0.json) include the wheel hash
and validation boundary. Source-fact acceptance is limited to the supplied
staff-bars; these checks do not measure universal OMR accuracy or physical
singing feedback. See [the reusable review contract](SOURCE_REVIEW.md).

## v0.1.0 — historical acceptance

The v0.1.0 metadata and skill were checked on 2026-10-13 with the installed Codex CLI. The test used process-local configuration overrides and temporary validation dependencies; the user's persistent Codex configuration remained unchanged.

| Check | Observed result |
| --- | --- |
| Root `plugin.json` | Passed the official Agent Plugins 1.0.0 JSON Schema with a Draft 2020-12 validator |
| Root manifest plus repo marketplace | Actual Codex discovery returned `choir-assistant@choir-assistant`, version `0.1.0`, available for installation |
| Codex compatibility fallback | An isolated copy containing `.codex-plugin/`, `.agents/`, and `skills/`, with no root manifest, was discovered as the same plugin/version |
| Skill frontmatter | Passed the skill-creator `quick_validate.py` validator |
| Skill UI metadata | Generated `agents/openai.yaml`; its default prompt explicitly invokes `$choir-workbench` |
| Persistent configuration | Before/after SHA256 of the user's Codex configuration matched |

Official schema: [Agent Plugins 1.0.0 plugin manifest](https://agent-plugins.org/schemas/1.0.0/plugin.schema.json). The fetched schema SHA256 was `0a4aad95ce337878ad38802ebf0daa3fde76abe3f65400c86bcbb1ec0b3ab883`.

To inspect a checkout without adding it to persistent configuration, replace the example path below with the absolute checkout root:

```sh
codex \
  -c 'marketplaces.choir-assistant.source_type="local"' \
  -c 'marketplaces.choir-assistant.source="/absolute/plugin-root"' \
  plugin list --marketplace choir-assistant --available --json
```

The expected available entry is `choir-assistant@choir-assistant`, version `0.1.0`, with `installPolicy: AVAILABLE`. For this release test, it reported `installed: false` and `enabled: false`; discovery did not install or enable a plugin. The repo catalog's `source.path: "./"` resolved to the plugin root as intended.

The portable JSON Schema checks portable manifest fields. OpenAI-specific extension data is additionally exercised by the actual host's discovery path. Public directory approval and per-score musical accuracy have separate validation requirements; the source-review and output-checking process is documented in [WORKFLOW.md](WORKFLOW.md).

## Runtime acceptance

The release was installed as a regular wheel in a new Python 3.12 environment,
then used from outside the plugin directory. `pip check`, **103 Python tests**
and **62 JavaScript tests** passed. The PDF tests used a real Poppler renderer.

Fresh Audiveris recognition of the original eight-bar public fixture produced
29 notes. Every pitch, duration and tie matched its source MusicXML. The whole
score review queue covered all eight measures; SVG covered 29/29 notes. MIDI
and WAV were both 20 seconds at 96 quarter-note beats/minute, with zero clipped
samples. A subsequent import reused the verified recognition cache and mapped
the explicit `Pno` source label without manual mapping.

An independent reviewer reproduced a page-evidence acceptance failure with no
OMR geometry. After repair, a copy of the public fixture containing one
deliberately changed pitch was corrected through CLI scan/apply and rebuilt
into actual SVG/MIDI/WAV, preserving all 29 source note IDs. This engineered
fixture tests the correction transaction; it is not an OMR accuracy measurement.

Browser checks exercised note audition, four-note selection and real 2.5-second
audio synthesis, panel switching, metronome start/stop at 96, and original-PDF
view. [Structured results](release-validation.json) and the [public example
screenshot](workbench-example.png) record the acceptance scope. Physical
microphone/speaker performance remains untested.
