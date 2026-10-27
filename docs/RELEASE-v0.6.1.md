# Choir Assistant v0.6.1

Includes the [v0.6.0 correction cards, first-use guide and anonymous message
board](RELEASE-v0.6.0.md), with a correction to publication progress and completion.

A publication receives a new server job ID. That job now stays bound to the
complete submitted draft, so a successful result displays the published note
count and the reload action rather than an outdated-preview warning. Publication
and restoration freeze further mutations until their outcome is known; preview
generation still permits accumulating more edits. Viewing another saved
publication preserves a separate local draft.

Server musical state, history, message-board storage and access controls are
unchanged. Deployment remains a separate action through the configured private
management channel.
