# Reusable source review and independent verification

`choir-assistant verify` checks a frozen bundle without loading the score through
the generator's parser. This catches internally self-consistent mistakes that
would otherwise be copied from the model into every consumer.

```sh
choir-assistant verify --project /writable/score-run/project.json --output /writable/consistency.json
choir-assistant verify --bundle /writable/frozen-bundle --facts /writable/source-facts.json --output /writable/source-check.json
```

Exit status is 0 for a passed check, 1 for a failed verification (including
malformed facts or missing artifacts), and 2 for invalid CLI arguments or command
errors. An output report must stay outside the frozen
bundle and cannot overwrite the facts. The command does not correct notes,
change a project pointer, or mark a whole score reviewed.

## Two different questions

Without `--facts`, the report checks artifact integrity, effective MusicXML
`divisions` at each XML position, rational durations and offsets, chord members,
source locators, pitches, and relationships against the model. Original source
snapshots and current fields are compared separately; source-to-current changes
are listed transparently rather than treated as corrupt original snapshots.
The frozen `scan`/`apply` transaction validates correction authorization; `verify`
does not replace that transaction or infer that every listed change was approved.
Arbitrary input XML IDs are
provenance; part, measure index, and source note ordinal locate original events.
Canonical IDs refer to current model objects.

With `--facts`, a second check compares selected complete staff-bars against
facts read from the original score. A passing result means those selected facts
match; it does not establish that the reader transcribed the original correctly,
that every measure was independently read, or that audio and engraving work.
Unselected bars retain their existing review status.

## Freeze facts before comparison

Read original page/system images before viewing candidate fixes or model pitches.
Record note heads and rests, exact offsets and durations, and relevant tie types.
Use a second reader for ambiguous regions. Preserve the initial facts and the
reason for any subsequent source-image recheck; do not silently change expected
values to fit the generator. Add revision and hashes after freezing the musical
reading, then run the check against the final rebuilt bundle.

A minimal JSON contract is:

```json
{
  "schemaVersion": 1,
  "revision": "FINAL_MODEL_REVISION",
  "hashes": {
    "sourceMusicxml": "SHA256_OF_FROZEN_INPUT_XML",
    "canonicalMusicxml": "SHA256_OF_CANONICAL_XML",
    "model": "SHA256_OF_INTERACTIVE_JSON",
    "sourcePdf": "SHA256_OF_FROZEN_PDF"
  },
  "scope": "Original page 1, complete P1 staff 1 in measure index 1",
  "staffBars": [
    {
      "sourcePart": "P1",
      "measureIndex": 1,
      "staff": 1,
      "events": [
        {"kind": "note", "pitchName": "C4", "offset": "0", "duration": "1", "voice": "1", "tieTypes": []},
        {"kind": "rest", "pitchName": null, "offset": "1", "duration": "3", "voice": "1"}
      ]
    }
  ]
}
```

Replace all placeholders with actual hashes; they are deliberately not valid
acceptance values. All listed staff-bars are complete: missing or additional
symbols fail. Multiple independent voices can contain identical unison objects;
they are compared with multiplicity, and optional `voice` disambiguates them.
Fraction strings such as `"1/3"` avoid decimal rounding of tuplets. Empty facts
cannot establish source review. `sourcePdf` is required when the bundle includes
a frozen PDF. Older bundles without a PDF snapshot explicitly report that
binding as unavailable; external configuration paths are not treated as frozen
source evidence.

## Rebuild and inspect the actual outputs

Materialize accepted corrections before replacing or reordering a staff-bar.
Publish old-to-new identities separately; do not replay an old ordinal ledger
against newly inserted notes. Rebuild the complete bundle and bind all accepted
reports to its final revision and hashes.

Inspect actual MEI/SVG and MIDI/WAV separately. For tuplets, check performed ratios
and displayed group counts. For octave lines, check printed and sounding octaves,
complete chord endpoints, and the following out-of-scope note. A boundary that
crosses an ambiguous grace-note order stays unresolved. Sparse voices, explicit
forward gaps, and legal mid-measure clef changes are preserved.

Finally open the workbench: verify every SVG page, note/region selection, a real
short synthesis, and repeated PDF-to-interactive switching. A deliberately failed
SVG request should show a retry while other pages remain usable. Structural
validation, source facts, rendered notation, and physical singing recognition
are different acceptance results.
