# Plugin package validation

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
