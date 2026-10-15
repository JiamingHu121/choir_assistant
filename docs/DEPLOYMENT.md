# Optional website publishing

The authoring plugin and hosted website have separate lifecycles. Import, review,
calibration and engraving run locally. Publishing consumes completed, hash-bound
bundles and creates an immutable website snapshot. A host runs the exported
`site_server.py`, its frozen playback modules and assets; it does not install or
call the authoring plugin, Audiveris, Java, or a hosted agent. Updating the plugin
does not update a website until `site sync` is explicitly run.

Keep the library, export directories, deployment profile, SSH keys, SoundFont,
runtime cache and operational receipts outside the installed plugin and public
Git repository. There is no built-in domain, SSH account or production library.
An HTTPS gateway is configured independently; synchronization never changes DNS,
Cloudflare configuration, gateway SSH keys, or other applications.

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

The serving runtime needs Python 3.8+, `mido`, a working native FluidSynth and a
licensed SF2 file. Its requirements differ from the Python 3.12+ authoring
environment. Install and test these tools on the target OS; do not copy a binary
from a newer macOS onto an older Mac. The server can serve existing audio without
a synthesis engine, but a synthesis-enabled deployment must pass a real short
selection render before acceptance.

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
