# Choir Assistant v0.6.0

Correction controls, natural-language requests and saved tasks no longer share a
single oversized card. A separate current-batch card shows changed-note counts,
musical differences, preview progress, listening and a prominent publish action.
Technical version identifiers and optional attribution are collapsed.

Changing the selected notes retains earlier edits in the current page's draft.
One preview includes all accumulated operations and one explicit publication
publishes that complete preview. Further edits invalidate an older preview;
opening a saved task cannot silently drop pending draft changes.

Confirmed natural-language proposals created in the current page can join that
batch. Proposals that conflict with the same note's same field are rejected
before changing the draft. Historical tasks remain independent snapshots.
Existing relationship completeness and publication compare-and-swap checks stay
enforced.

A five-step first-use guide introduces score selection, playback, batch
correction, practice and feedback. Completion or skipping is remembered locally;
the header help button reopens the guide. Header actions remain available on
small screens without overlapping the workbench.

An optional anonymous message board accepts choir suggestions and song requests.
Its public feed is distinct from the private issue inbox. Same-origin CSRF
checks, strict text fields, rate limits and bounded persistent storage are
required. Data lives in the existing private runtime, outside immutable website
releases and plugin packages. No account, domain or privileged installer change
is needed for an already configured deployment.
