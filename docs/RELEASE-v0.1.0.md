# Choir Assistant v0.1.0

First distributable Codex plugin and standalone CLI for the complete
PDF → Audiveris recognition → structured notes → whole-score scan → original-page
agent review → accepted corrections → synchronized rehearsal workbench workflow.

Includes interactive note/region selection, part mixing, tempo controls,
metronome, source evidence and experimental singing/antiphonal pitch practice.
All user scores, caches and generated audio remain outside the installed plugin.
Engine binaries and SoundFonts are installed separately.

Install:

```sh
codex plugin marketplace add JiamingHu121/choir_assistant
codex plugin add choir-assistant@choir-assistant
```

Then ask: “用 $choir-workbench 导入这份 PDF 曲谱，完成全谱校准并打开工作台。”

Validated with an independent Python 3.12 installation, 103 Python and 62
JavaScript tests, fresh recognition of the original public example, actual
MIDI/WAV generation, source-confirmed batch correction and browser interactions.
See README/SETUP and docs/VALIDATION for dependencies and evidence.

This is an initial preview release. General OMR fidelity still needs source
review; arbitrary false-symbol deletion, octave-direction scopes and repeat
routes remain limited. Physical microphone/speaker accuracy is not certified.
