# Optional website publishing

The authoring plugin and hosted website have separate lifecycles. Import, review,
calibration and engraving run locally. Publishing consumes completed, hash-bound
bundles and creates an immutable website snapshot. A host runs the exported
`site_server.py`, its frozen playback modules and assets; the HTTP process does
not install or call the authoring plugin, Audiveris, Java, or a hosted agent.
Optional online correction uses a separately provisioned low-privilege worker
and hash-indexed source modules; see [manual correction and synchronization](MANUAL_CORRECTION.md). Updating the plugin
does not update a website until `site sync` is explicitly run.

Keep the library, export directories, deployment profile, SSH keys, SoundFont,
runtime cache and operational receipts outside the installed plugin and public
Git repository. There is no built-in domain, SSH account or production library.
An HTTPS gateway is configured independently; synchronization never changes DNS,
Cloudflare configuration, gateway SSH keys, or other applications.

The public website and the deployment management connection are independent.
An outbound web tunnel can keep the site available while the profile's private
SSH address is unreachable from an off-site client. Verify the configured SSH
connection from the actual client network before promising remote updates.
Off-site deployment requires an existing authenticated management tunnel, VPN or
equivalent channel. Establishing that channel initially requires access to the
host, either locally or through an already working remote-management session.
Ordinary site synchronization does not expose SSH or turn the public score
password into server-management authority.

## Register a completed score

Complete the source review and verification described in [WORKFLOW.md](WORKFLOW.md)
first. Registration preserves the project's current review state; it does not
certify a draft or silently accept proposed corrections.

```sh
choir-assistant library add --library /absolute/score-library \
  --project /absolute/score-run/project.json --id practice-study \
  --title "Practice Study"
choir-assistant site export --library /absolute/score-library \
  --output /absolute/site-releases/first-preview --prepare-original
```

`--prepare-original` renders a missing original-PDF page cache with Poppler,
offline. It does not rerun OMR or alter a source bundle. The exporter rejects
missing, altered, unindexed or escaping assets. Every exported page and asset is
indexed by SHA-256. The site catalog/SQLite index is generated from registered
projects rather than copied from a private authoring database. Public JSON omits
authoring-machine source paths; the export retains the original frozen model for
internal verification.

Run registrations for the same work serially: publication has a deployment lock,
but separate `library add` operations currently do not share a registration lock.
Public review downloads are redacted text projections; binary review attachments
are not exposed. Canonical reports and models remain inside the frozen release.

To preview without the plugin installed on the receiving host:

```sh
python3 /absolute/site-releases/first-preview/site_server.py \
  --root /absolute/site-releases/first-preview \
  --runtime /absolute/site-runtime --bind 127.0.0.1 --port 8780 \
  --fluidsynth /absolute/bin/fluidsynth --soundfont /absolute/GeneralMIDI.sf2 \
  --origin http://127.0.0.1:8780
```

The standalone serving and narrow editor-worker path is tested with Python
3.9+, `mido`, a working native FluidSynth and a licensed SF2 file. The complete
authoring CLI still requires Python 3.12+. These are separate runtime contracts. Install and test these tools on the target OS; do not copy a binary
from a newer macOS onto an older Mac. The server can serve existing audio without
a synthesis engine, but a synthesis-enabled deployment must pass a real short
selection render before acceptance.

## Private website settings, per-score access and feedback

Website identity and access are deployment settings. They are not part of an
export, a source score, or the installed plugin. Create a private configuration
outside all three:

```sh
python3 -m choir_assistant.deployment.security configure \
  --output /absolute/private/site-config.json \
  --site-title "Example Choir Library" \
  --github-url https://github.com/example/project
```

The command prompts twice without echoing the password. For unattended initial
provisioning, use `--password-file /absolute/private/password.txt` with a mode-0600
single-line file, then remove that temporary file. There is intentionally no
`--password` argument, environment-variable default, bundled production password
or password printed in the result. Existing configuration files and symlinks
are never overwritten. Keep all generated secret fields out of Git and logs.

The resulting JSON has `schemaVersion: 1`, public `siteTitle`/`githubUrl`, and
private `auth` fields: `salt`, `passwordHash`, `iterations`, `cookieSigningKey`
and `sessionSeconds`. Password verification uses PBKDF2-HMAC-SHA256. The separate
`internalHealthToken` authenticates local synchronization checks; it is not a
browser password. Optional `feedback.maxBytes` and `feedback.maxEntries` bound
the local inbox (defaults 10 MiB and 2,000 entries). `site_server.py --site-config`
requires a regular file outside the frozen release with mode 0600, or 0640 when
owned by a trusted administrator and the dedicated service group.

Pass `--site-config /absolute/private/site-config.json` to the independent
runtime, alongside an explicit `--origin https://scores.example.org`. A reverse
proxy must preserve the intended Host/Origin relationship. Secure cookies require
HTTPS; the server's HTTP port remains loopback-only. The homepage and basic
catalog remain public. Each score requires its own signed, expiring HttpOnly,
Secure, SameSite=Strict cookie. Authorizing one score does not authorize another.
Final endpoints independently check access for models, SVGs, original pages,
PDFs, audio, symbols and generated clips; hiding homepage links is not the
access boundary. The full SQLite library download is disabled while access
control is enabled. Passwords are not put in URLs, browser storage or frontend
code. Password attempts have per-client and global limits; sessions default to
12 hours. Rotating `cookieSigningKey` invalidates existing sessions.

`GET /api/health` exposes only minimal health when access control is enabled.
Detailed revisions are available only at loopback `/api/health/internal` with
`Authorization: Bearer <internalHealthToken>` and without proxy headers. Never
send that token to an external/public check. Public configuration returns only
branding, feature flags and a CSRF token bound to a signed, random HttpOnly client
cookie. Password login and feedback require that token and an allowed Origin.

Feedback is a private local inbox, not an automatic email or GitHub Issue. Users
can submit text from the homepage or any score; the score, measure and playback
position are prefilled when available and remain editable. The server returns a
receipt ID. Text and context are stored as plain JSON inside
`RUNTIME/feedback.sqlite3` (0600), without cookies, passwords, client IPs or audio.
The runtime bounds text length, frequency, entry count and database pages. It
serves no endpoint for reading the inbox. A local maintainer can inspect it:

```sh
python3 -m choir_assistant.deployment.security feedback \
  --database /absolute/site-runtime/feedback.sqlite3 --limit 50
```

This outputs JSON text. Treat feedback as untrusted user data; do not render it
as HTML or execute commands copied from it. An installation without the plugin
can use `sqlite3 -readonly /absolute/site-runtime/feedback.sqlite3` and query
`SELECT id, created, payload FROM feedback ORDER BY created DESC LIMIT 50;`.

Protect the entire site hostname from intermediary caching of score routes.
The runtime sends `Cache-Control: private, no-store` for protected resources;
check your gateway/CDN rules and purge any resources cached before access was
enabled. No service worker or offline score cache is installed by this website.
A previously downloaded score remains with its recipient; adding authentication
does not revoke files users already saved.

## Configure a destination once

The first deployment provisions an independent host namespace and user service.
Use a path accessible to the service account. On macOS, a directory under that
account's `Library/Application Support` avoids the Documents/Desktop TCC problem
seen with background LaunchAgents. Do not disable TCC or grant Full Disk Access
to solve a project-path issue.

The current synchronization adapter updates an **existing, owned macOS user
LaunchAgent**. Its arguments must run `REMOTE_ROOT/releases/ID/site_server.py`
and bind `--root` to that same release. Prepare Python, native audio tools, SF2,
the initial exported release and that service first. Keep runtime/cache and logs
under separate shared directories. `RunAtLoad`/`KeepAlive` starts a user service
in its login session; this is not a claim of boot-before-login availability.
The profile must name only this service, never a shared tunnel or unrelated job.

Copy [the profile example](../examples/site-profile.example.json) to a writable
directory outside the repository and replace every destination field. SSH uses
the existing local configuration and strict host-key checking; no private key or
token belongs in the JSON profile. Match the public URL with the service's
explicit `--origin` option. Complete HTTPS routing separately and verify that it
points to this service's loopback port.

If the destination's operating manual requires an explicit readiness statement,
set `requireReady: true`. Supply `--ready` only after that requirement has been
satisfied in the current authorized work; a command-line flag is not itself a
human confirmation.

## One command for subsequent updates

After registering a new score or accepting a new bundle revision:

```sh
choir-assistant site sync --profile /absolute/private/site-profile.json
```

For a host with the readiness requirement above, append `--ready`. This exports
the current library, verifies the complete file set, compares destination hashes,
transfers only changed files using tar over SSH, builds a new release from
verified reused files, and updates only the configured user service. An unchanged
snapshot is a no-op. No rsync or in-place score edits are involved.

```sh
choir-assistant site sync --profile /absolute/private/site-profile.json --check-only
choir-assistant site check --profile /absolute/private/site-profile.json
choir-assistant site rollback --profile /absolute/private/site-profile.json
```

A project lock serializes publication. Release activation is followed by local
health and external HTTPS checks against the expected work/revision set. Failure
restores the previous service configuration and release pointer and checks that
the old site recovers. Old releases are retained. External checks use a
self-identifying User-Agent and strict TLS; DNS or certificate failures are
reported, not bypassed. DNS propagation and a source machine's cached resolver
can differ from an external browser; a private loopback health check alone is not
public acceptance.

Read the returned receipt, then check the public URL in a normal browser: all
interactive pages, original-PDF round trip, an actual short selected playback,
and metronome controls. Preserve the JSON receipt and a screenshot with the
private deployment records. Fixture tests do not establish real microphone or
speaker interference accuracy.

## Staging for a separately administered service account

Set `publisherMode: "isolated-manual"` when the website runs under a dedicated
service account and root owns its releases and LaunchDaemon. This mode never
opens the retired user LaunchAgent, writes its plist, calls launchctl/sudo or
reads service configuration. Copy the [isolated profile example](../examples/site-profile.isolated.example.json)
outside the plugin. `remoteRoot`, `isolatedPublishHelper` and optional
`isolatedAdminPython` are explicit deployment paths; no server name, helper code
or administrator credential is bundled.

The administrator must first provision the dedicated accounts and their sandbox,
a root-owned, non-service-writable deployment root with `releases/`, and a
root-owned `public-state.json`. Its exact contract is:

```json
{
  "schemaVersion": 1,
  "publisherMode": "isolated-manual",
  "release": "release-<first-20-manifest-hash-characters>",
  "manifestSha256": "<complete-64-character-sha256>",
  "incomingRoot": "/absolute/deployment-root/incoming",
  "phase": "committed"
}
```

The deployment SSH account owns only `incoming/` and `incoming/releases/` (0700).
The published release and every included file/directory must remain root-owned
and not writable by services. The plugin verifies the current complete manifest
and exact file set, sends changed files over SSH, copies reusable bytes into a
new full incoming release, and verifies it before writing `incoming/request.json`.
The request contains only schema version, target release/hash and expected current
release. Its target ID is derived from the complete manifest hash.

`site sync` returns `pendingAdminActivation: true`, `activated: false`, the
staged path and a quoted administrator command. **The upload has not activated
the site.** The administrator reviews and runs the configured root-owned helper
on the server. The helper must copy into a root-private staging area, verify the
complete pinned manifest again, run preflight code as the unprivileged service
account, and activate only its own web service with recovery checks. No sudoers
entry or web-accessible privileged endpoint is created. This deliberate final
administrator step preserves the service-account boundary.

`site check` verifies the administrator's public state, frozen release hashes and
minimal external HTTPS health. It explicitly reports that complete internal
runtime verification still requires the administrator. Concurrent activation
causes compare-and-swap checks to fail instead of reporting an unintended release.
For this mode, `site rollback` is unsupported: an isolation installer's migration
rollback may restore original service accounts and is not a score-release rollback.
Do not substitute it. A destination needs its own reviewed release rollback
procedure before offering that operation.

## Routine updates through an independently installed publisher

`publisherMode: "isolated-auto"` uses the same frozen export, exact file set,
changed-file tar transfer and private incoming directory. It requires a publisher
that the server administrator has separately installed and reviewed once. Copy
the [automatic profile example](../examples/site-profile.auto.example.json)
outside the plugin. The profile names no privileged helper, administrator Python,
service label, domain account or credential. This plugin contains the unprivileged
client protocol only; it does not install or invoke the privileged publisher.

The separately installed publisher owns `public-state.json` and
`publisher-status.json`. Both are bounded regular root-owned files and must not
be writable by the web, tunnel or SSH deployment accounts. Automatic public
state retains the manual schema above, changes `publisherMode` to
`"isolated-auto"`, and adds exactly:

```json
"publisher": {"schemaVersion": 1, "generation": 0}
```

`generation` is a nonnegative integer used with the current release for
compare-and-swap checks. The client verifies the full current immutable release
before planning and staging. `incoming/request.json` has exactly these fields:

```json
{
  "schemaVersion": 2,
  "requestId": "<32-lowercase-hex-characters>",
  "releaseId": "release-<first-20-manifest-hash-characters>",
  "manifestSha256": "<complete-64-character-sha256>",
  "expectedCurrentRelease": "release-<current-id>",
  "expectedGeneration": 0
}
```

The request selects only a frozen release under `incoming/releases/`. It cannot
choose a command, executable, account, destination, service or secret config.
The server publisher must copy into its own private snapshot, bound every file,
reject links and unexpected files, recheck the pinned full manifest, preflight
as the service account, and activate only its independently fixed web service.
Runtime code supplied in a release may run only as that service account. A
publisher must hold its own root-owned lock, respect an existing
`incoming/.site-sync.lock` during consumption, and check the generation/current
release again before activation. The client never runs `sudo`, changes a
LaunchDaemon or accesses service secrets.

`publisher-status.json` schema 1 contains exactly `schemaVersion`,
`publisherMode`, `status`, `generation`, `currentRelease`, `manifestSha256`,
`changed`, `requestId`, `releaseId`, `previousRelease`, `errorCode`, and `recent`.
The manifest hash always describes the actual committed `currentRelease`,
including after a failed candidate. `status` is `idle`, `queued`, `publishing`,
`succeeded`, `failed` or `blocked`. Request identity fields are null before any
request. `errorCode` is null except for failures, and is one of:

- `invalid-request`, `stale-generation`, `validation-failed`, `preflight-failed`
- `publication-failed-restored`, `recovery-required`, `request-id-collision`
- `capacity-exceeded`, `internal-error`

`recent` is a bounded list of at most 32 terminal receipts, each containing
exactly `requestId`, `requestSha256`, `status`, `releaseId`, `generation`,
`changed` and nullable `errorCode`. `requestSha256` binds the original request
bytes, including whitespace and newline. Keep the latest terminal request in
this list. Detailed private logs and receipts stay outside this public file.
The initial idle receipt has no request, no error, `changed: false` and an empty
recent list.

The normal command remains:

```sh
choir-assistant site sync --profile /absolute/private/site-profile.json --ready
```

It serializes staging, refuses to overwrite a different unresolved request and
reuses an identical pending target's request ID. An unchanged active target
performs read-only verification with no transfer. After staging, it polls the
data-only status in short SSH calls for at most 90 seconds. Only a matching
successful receipt followed by a fresh current-manifest/public-health check
returns `activated: true`. If still running it returns `pendingActivation: true`;
this is a pending operation, not a deployment success. Inspect it using `site
check` or repeat the same sync to continue observing the existing request.

A terminal `failed` or `blocked` request is not retried automatically. The CLI
returns a nonzero exit code and the fixed public error code. After investigating
the private logs, an operator may explicitly request a new attempt:

```sh
choir-assistant site sync --profile /absolute/private/site-profile.json --ready --retry-failed
```

That creates a new request ID; it does not restart or mutate the failed receipt.
Legacy schema-1 pending requests require a separately reviewed administrator
migration; automatic mode never silently rewrites them. `site rollback` remains
unsupported in isolated modes and cannot invoke an account-migration rollback.

## Network and account boundary

The hosted application provides score viewing and bounded selection synthesis.
It exposes no deployment, SSH, OMR, source-editing or general command endpoint.
Origin checks prevent some browser cross-site requests; they are **not user
authentication**. An anonymous website can still be abused. The runtime bounds
connections, reading time, render duration/rate and generated-cache storage, but
these controls do not guarantee immunity to denial of service or dependency bugs.

If a gateway runs on another machine, that machine remains part of the traffic
path. A reverse SSH connection also creates a credential boundary: filesystem
mode 600 does not hide a key from another process running as its owner. Use a
dedicated gateway account with server-side `AllowTcpForwarding remote`,
`AllowStreamLocalForwarding no`, fixed `PermitListen`, `PermitOpen none`, and
`MaxSessions 0`, plus a forced non-shell command and no agent forwarding. Key
options limiting TCP ports do not alone disable Unix-domain socket forwarding.
See the [OpenSSH configuration manual](https://man.openbsd.org/sshd_config).
Review these settings for that dedicated account rather than changing a shared
research account's global SSH policy.

To remove the gateway machine from both HTTP traffic and the SSH trust path,
host an independent HTTPS connector on the score server itself and retire the
old reverse-forwarding credential. An existing domain can point to that
independent connector. A [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/)
uses outbound connections; it still delivers public requests to the application
and is not an application sandbox.

For this arrangement, create a separate named tunnel from the domain's authorized
management environment, place only its tunnel credentials JSON on the score
server with owner-only permissions, and run a service with a single-host ingress
to `http://127.0.0.1:8780` plus a final `http_status:404` rule. Keep the account
management certificate off the web server. Check the new connector's readiness,
switch only the intended hostname, verify external playback, and then stop and
revoke the old reverse SSH route/key. Preserve a DNS rollback target until the
new route has passed external acceptance. See [Cloudflare tunnel permissions](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/tunnel-permissions/).

## Loading performance

The workbench downloads its dependencies concurrently while preserving execution
order. The first notation page becomes interactive before later pages finish;
at most two later SVGs transfer concurrently. Original-PDF images start loading
when that view is opened. Pages received while hidden bind their click areas and
measure geometry when the notation view becomes visible again.

The standalone server supports negotiated gzip for immutable model, score, SVG,
and interface responses. A bounded, process-local representation cache avoids
repeated projection and compression. Each protected request still checks its
work cookie before reading that cache, and HTTP `no-store` remains in force.
CSRF, login, feedback, and other dynamic API responses are not opted into server
compression. Range requests retain identity bytes; gzip HEAD responses describe
the matching GET representation without sending its body.

Measure both origin and public transfer: a CDN may already compress the final
download while the server still uploads the full model to the CDN. Validate
first-page interaction, a slow or missing later page, PDF/notation round trips
during loading, and playback positioning on the final page. Keep score revisions
and audio hashes separate from interface and transport performance checks.


## v0.5 online music editing

Read [the correction and sync contract](MANUAL_CORRECTION.md) before enabling
`editor.enabled` in private website settings. Password holders publish musical
heads, while an independently provisioned worker creates verified immutable
previews. This grants no website-code or root-publisher authority. Exported
`authoring_modules` are source-only and covered by `file-manifest.json`.

Private `editorSync: {"enabled": true, "passwordFile": "/absolute/private/work-password.txt"}`
enables authenticated HTTPS pull before export. The password file is `0600`;
never put a literal password in a public example or command argument. Pull
updates real score pointers and resets source-review status, preserving the
original PDF and pre-pull backups. Stale content heads fail closed, including
in `--check-only`, which never downloads. A host-side activation guard must
recheck content heads under the editor publication lock to protect against
publication after the client's final inspection.


Before privileged editor provisioning, pass the target OS's sandbox syntax gate
as an ordinary user, then the actual dedicated-UID native full-build preflight.
See [operator acceptance gates](MANUAL_CORRECTION.md#operator-acceptance-gates).
Passing package tests is not live service acceptance. Preserve the durable
original musical baseline through software updates; do not derive history's
original restore target from the newly activated release bundle.
