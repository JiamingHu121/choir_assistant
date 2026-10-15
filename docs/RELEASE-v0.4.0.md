# Choir Assistant v0.4.0

A shared library can keep its homepage public while requiring a separate session
for each score. Website branding, project links, password verifiers, cookie keys
and feedback settings live in private deployment configuration outside the
plugin and frozen release. The generator prompts without echo or reads a private
password file; it never takes a literal password argument or prints credentials.

Final score endpoints enforce access for interactive models, SVGs, original pages,
PDFs, audio, symbol queries and generated selections. Authorizing one work does not
authorize another; whole-library SQLite downloads are disabled when access control
is active. Signed cookies are HttpOnly, Secure, SameSite=Strict and expire. Login
and feedback use browser-bound CSRF tokens, allowed Origins and bounded request
rates. Public health stays minimal; detailed local health uses an independent
private credential.

Users can submit feedback from the homepage or a score, including editable measure
and playback context, and receive a receipt ID. The inbox is a private local SQLite
file with entry, text, frequency and storage limits. Feedback is plain data; the
website does not publish it, execute HTML, email it or open GitHub Issues.

Interactive scores add measure numbers, current-measure highlighting and a
playhead based on the canonical measure timeline. Following continues through
rests and tied tails and relocates after paused seeks or returning from PDF view.
Source-reviewed lyric sidecars add measure/part text, page-level lyric lists and
current-measure words without modifying frozen MusicXML, notes or audio. Sidecars
bind to the exact revision and source PDF, with original-page evidence. They do
not claim guessed syllable-to-note alignment or improve unreviewed OMR accuracy.

Publishing now distinguishes two host arrangements. Existing owned user
LaunchAgents retain atomic activation and recovery. With `publisherMode:
"isolated-manual"`, the plugin verifies root-owned public deployment state,
transfers changed files and prepares a complete release only under the deployment
account's incoming directory. It reports `pendingAdminActivation: true` and a
quoted administrator command. A separately reviewed host helper performs the final
activation; the plugin does not run sudo, change service accounts or sandbox rules,
read service secrets, modify DNS, or create a privileged web endpoint. Migration
rollback is explicitly not treated as score-release rollback.

The authoring plugin and exported website remain independent. No production
hostname, service identity, password, private score, SoundFont or engine binary is
included. Existing deployments must explicitly export/sync and configure any new
access settings. Check gateway caching rules and remove previously cached public
score assets when enabling access control. Previously downloaded files cannot be
revoked by the website.

Focused validation covers real HTTP access/CSRF/cookie/feedback boundaries,
private credential provisioning, incremental isolated staging, exact file sets,
compare-and-swap behavior and legacy user-service recovery. See
[package validation](VALIDATION.md) for the recorded scope. Tests do not establish
physical microphone/speaker accuracy, immunity to dependency vulnerabilities, or
universal score recognition accuracy. Account and sandbox provisioning must be
verified on the destination operating system.
