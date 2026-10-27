# Manual score correction and online synchronization

The score remains a versioned musical document. Selecting or auditioning a note
changes no public music. Publishing a correction creates a new immutable content
head with its parent, musical revision, typed operations and reason. Passing
structural checks does not certify the transcription against the original PDF.

## From selection to publication

Open **Review / 校对**, then select notes in the score. Selection updates the
calibration panel without taking over the pitch-practice tab. Semitone buttons
shift every selected pitched note by exactly one semitone, preserving intervals.
The six note-value buttons set each selected note to a 32nd, 16th, eighth,
quarter, half or whole note; the dotted switch multiplies that value by 1.5.
This sets individual values, rather than stretching a passage as one object.
Repeated semitone changes retain the written letter within double accidentals;
beyond that range they use a standard enharmonic spelling in the edit direction
(for example, F#4 raised twice becomes G#4). The receipt records that respelling.

The four independent cards cover the current batch, selected-note controls,
natural-language requests and saved tasks/history. Switching selections retains
earlier edits in the current page's draft. For example, three edits to three
different notes become one complete preview and one explicit publication. The
current-batch card shows all changed notes; a newer edit invalidates an older
preview, including when that old task is reopened. Closing or reloading the page
does not reconstruct an unsaved draft from historical jobs.

The panel shows original values beside the browser draft and supports undo,
redo and reset. Quick WebAudio audition uses a simple tone and draft pitch and
duration while retaining original onsets. It is a local sketch, not the final
reflowed rhythm or chosen instrument. The original PDF remains the reference.

**Generate a preview of all current changes** starts a durable server task. An independent
worker checks the selected relation groups and affected voices, rebuilds the
score and synthesizes preview audio. Partial tie, beam, tuplet or chord-dependent
changes can require a complete explicit selection. New structural problems block
publication; existing unresolved issues remain visible rather than being fixed
by guessing. Preview SVG pages and audio are separate from the public version.
An empty optional correction explanation receives a default audit reason and
does not block preview generation.

A ready preview needs a separate **Publish** click. Every session authorized with
the corresponding work password can view and continue that work's calibration
tasks, publish its ready corrections, and restore its history. No administrator
approval is required for those musical actions. A shared password identifies an
authorized session, not a named person; optional attribution does not establish
identity. The first online publication preserves the original musical baseline
as an immutable snapshot descriptor inherited by later heads. History can still
restore that original baseline after library/software synchronization; it is not
redefined as whichever bundle the latest website release happens to contain.
Restoring music creates a new head and does not roll back website code.
Other works remain separately authorized. The old general feedback
inbox remains private and is not a calibration queue.

Natural-language requests are interpreted against the current published score
and selected note IDs. Supported requests become a typed proposal. After explicit
confirmation, a proposal created in the current page joins its cumulative draft
and generates a preview of the complete batch. A proposal that changes the same
field of an already edited note is rejected before either change is applied;
the user resolves that overlap explicitly. Saved jobs from other sessions are
never silently merged. The user must inspect and confirm the difference,
then explicitly publish the completed preview. Ambiguous or unsupported complex requests remain in `needs_input`; they must
not be guessed into a broader edit. A configured model may propose only the supported musical patch;
it cannot supply a shell command or alter the website implementation.

Tasks report actual worker stages and outcomes rather than invented percentages.
If the worker heartbeat is missing or stale, the UI reports that the backend has
not responded, retains the submitted task and continues polling it. Local draft
editing remains available; avoid creating duplicate submissions to recover it.
Reopening the workbench restores the work's recent tasks. A preview whose base
head has changed is stale and cannot be published over a newer version. The
server enforces this compare-and-swap even if the browser is old. History restore
creates a new head and requires an explicit confirmation in the UI. After
publication the browser verifies the work, movement, revision and current head
before reloading. Score, audio and subsequent selection requests are pinned to
the head already loaded, so publication in another session cannot mix versions.

## Pull online music before pushing a library

A release contains `editor-protocol.json` with schema version 1 and
`contentHeads`, a map from `work/movement` to its incorporated online edit-head
SHA-256. The local source is `.online-editor-heads.json` in the library. Do not
write that watermark by hand to bypass synchronization: it describes imported
music, not an approval flag.
The private watermark also binds each head to the imported model revision and
bundle-manifest SHA-256. Export checks the actual active bundle before emitting
a head. Re-registering an old project or replacing/rebuilding that bundle cannot
silently inherit its watermark: export fails until the online ledger has been
deliberately merged/rebased and incorporated. Do not advance either a head or its
bundle binding by hand to conceal an old musical source.

An optional private sync-profile field enables automatic authenticated pull:

```json
{
  "editorSync": {
    "enabled": true,
    "passwordFile": "/absolute/private/work-password.txt"
  }
}
```

The file must be a bounded regular file owned by the syncing account, with mode
`0600`, and contain one password. The password is read only when a changed head
needs downloading. It is sent only in a same-origin HTTPS login POST body with
CSRF protection. Normal TLS verification stays enabled and redirects are refused.
A single configured password must authorize every changed work being pulled;
otherwise the pull fails before switching entry pointers. Passwords, cookies and
request bodies are not logged or exported.
Large snapshot responses are sent in bounded chunks. The sync client reads a
fixed musical head in bounded HTTP byte ranges, verifies each range and the
unchanged total length, and respects export rate limits. It then verifies the
complete snapshot and every indexed artifact. Interrupted transfers do not
replace a local bundle or advance its synchronization watermark.
Existing measure-bound lyric sidecars are copied and rebound only when the PDF,
base bundle and complete measure/part/symbol addresses still match. Their text
and page evidence remain unchanged, and the original file remains untouched.
Changed addresses or conflicting sidecars stop synchronization for review;
preserving display annotations does not renew source-review certification.

For a normal `site sync`, the client inspects online heads before export, downloads
changed authenticated snapshots, validates their descriptor, canonical commit
hash, ledger hash, bundle manifest, actual musical revision and original-PDF
identity, and checks every indexed file. Bounded extraction rejects links,
duplicates, escaping paths and unindexed assets. The imported immutable bundle
becomes the movement's real `bundle` pointer and revision; source-review status
is reset to require review. Old revision-bound lyric/review sidecars are not
silently attached to the new music. The original PDF bytes are preserved.

Original entries and an existing cached catalog are backed up under
`.online-edits/backups/` before switching pointers. The content watermark is
written only after the score entries and cached catalog. Backups are retained
for review; sync does not automatically roll music back. An embedding library
application should rebuild any additional derived SQLite/UI index after pull.
Unchanged heads do not download again. A second remote inspection after export
catches publication that raced the pull. A separately provisioned publisher must
enforce the same content-head check at activation to close the final race.

`--check-only` never pulls or changes the library; it compares watermarks and
blocks a stale export. Without `editorSync`, a stale or absent local watermark
also blocks synchronization. Missing credentials are not a reason to overwrite
online music. Local import/export, SSH staging, website activation and musical
head publication are separate operations with separate receipts.

## Optional independent worker

Serving and authoring remain separate processes. The HTTP process serves music
and queues bounded work-scoped requests. It does not recognize PDFs, execute
natural-language instructions as code, or start an authoring build. An operator
may configure `editor.enabled` and an external editor root in private website
settings, then independently provision a persistent low-privilege worker. The
editor root, queue, job database and preview storage stay outside frozen releases.
The website requires per-work password access before enabling online editing.

Every export includes `runtime_modules/editor.py` and source-only
`authoring_modules/`: top-level workbench Python/CJS files, `synth/__init__.py`,
`synth/render.py` and `runtime.py`. Every file is covered by the release's full
SHA-256 manifest. These modules contain no Python environment, engine binary,
Node/WASM distribution, SoundFont, credentials or private scores.

A fixed, separately reviewed supervisor may select the actual active release
from root-owned public state and start a fresh unprivileged worker with
`PYTHONPATH` pointing at that release's `authoring_modules`. Using `--once` per
fresh process lets routine release updates use the new musical core without
executing release code as root or retaining old imports in a daemon. Provisioning
that supervisor, accounts and permissions is an operator task, not a side effect
of plugin sync. Do not point the worker at an unactivated incoming release.

For example, an already configured unprivileged worker can run the frozen entry:

```sh
PYTHONPATH=/absolute/active-release/authoring_modules \
  /absolute/worker-python /absolute/active-release/runtime_modules/editor.py worker \
  --root /absolute/editor-runtime --release /absolute/active-release --once
```

The full authoring CLI continues to require Python 3.12+. A separately tested
narrow worker path can use Python 3.9 with compatible dependencies. The operator
must verify actual rebuilds on that target runtime. Native Verovio is preferred;
when unavailable, both explicit paths `CHOIR_ASSISTANT_VEROVIO_NODE` and
`CHOIR_ASSISTANT_VEROVIO_MODULE` enable the local Node/Verovio WASM engraving
fallback. FluidSynth, FFmpeg and a licensed SoundFont are still independent
runtime dependencies. Neither native nor WASM engraving is a substitute for
source review, and a queued task is not evidence of a successful real build.

Optional natural-language execution is operator-configured through a bounded
JSON executor or a loopback local-model endpoint. Unconfigured or ambiguous
requests ask for clarification. This setup grants no access to email, GitHub,
credentials, other scores or the operating system's privileged publisher.


## Operator acceptance gates

Before a privileged build probe or service activation, test the exact sandbox
profile on the target OS as an ordinary user. A syntactically invalid profile is
a separate failure from an expected denial of a harmless, non-allowlisted
executable. Record that distinction instead of treating any nonzero exit as a
successful sandbox test. Some older macOS SBPL versions accept the symbolic
`localhost` form for a configured loopback port where a numeric-address form
fails to parse. Keep the rule restricted to the operator's selected local port;
never broaden it to a wildcard just to make parsing succeed.

After the syntax gate, run the fixed scoped preflight with the actual dedicated
worker identity, native target runtime and engines. Exercise a complete fixture
rebuild, selected-note change, stable source identity, SVG note coverage,
MIDI/WAV timing, and full MP3 decoding. Verify denied secret/outside-file access,
denied unrelated network access, the queue boundary and inherited preview-file
groups. The preflight must restore configuration and refrain from starting or
activating a production service. Keep raw child diagnostics bounded, redacted
and owner-only; return only a fixed summary.

Only after those checks pass should the operator activate the independently
provisioned worker and validate real authenticated preview/publication/restore
flows. Syntax checks, offline tests, a queue receipt or a passing fixture do not
by themselves establish live deployment acceptance.
