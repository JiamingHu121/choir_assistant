# v0.2.0 — reusable source verification and resilient workbenches

This release turns whole-score proofreading lessons into portable code,
synthetic regressions, and an executable review contract. Private scores,
recordings, correction ledgers, and generated libraries are not distributed.

- Added `verify --project/--bundle [--facts ...]`: independent sequential XML
  parsing, manifest integrity, and optional revision/hash-bound original-score
  facts. It reports artifact consistency separately from selected source review.
- Corrected sequential `divisions` changes, including backup/forward, chord
  members, inherited units, and reviewed chord insertions. Musical policy is now
  `music-semantics-v2.4`, so rebuilt models receive a new semantic identity.
- Kept legitimate sequential clef changes. Duplicate clefs within one attributes
  block remain review candidates.
- Made exact octave-direction anchors render correctly in Verovio without
  changing sounding pitches or musical times. Ambiguous grace boundaries and
  unrepresentable chord endpoints remain explicit unresolved cases.
- Froze the original PDF inside new bundles and checked its hash before preview.
  Missing declared SVG/audio/input files now fail bundle validation; existing
  files alone are insufficient proof of completeness. Old bundles remain usable
  with an explicit unknown PDF binding.
- Fixed the collapsed PDF-view toolbar, isolated SVG load failures by page, added
  visible retry and a 15-second fetch timeout, and preserved the venv interpreter
  path when launching the workbench.
- Updated `$choir-workbench` and [source review instructions](SOURCE_REVIEW.md)
  with source-first facts, identity migration, complete downstream rebuilding,
  and actual notation/audio/browser acceptance.

Engineering tests and a public synthetic fixture verify these behaviors. Three
local score bundles also exercised the independent parser over 6,723 structured
objects; their private source material is excluded. These checks do not establish
universal OMR accuracy, complete expressive engraving, or physical
microphone/speaker performance. Repeat routes and unsupported transpositions
retain the existing documented boundaries.
