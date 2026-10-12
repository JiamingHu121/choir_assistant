# Third-party components

The MIT license in this repository covers repository-owned code and workflow instructions. External engines, Python wheels and their native libraries, fonts, and SoundFonts keep their upstream licenses. No Audiveris/Java distribution, FluidSynth executable, FFmpeg/Poppler executable, or `.sf2` bank is bundled here.

| Component | Role | Upstream license/source |
| --- | --- | --- |
| Audiveris | PDF optical music recognition; separate executable | [AGPL-3.0](https://github.com/Audiveris/audiveris/blob/master/LICENSE) |
| Verovio | MusicXML/MEI engraving through its Python binding | [LGPL-3.0](https://github.com/rism-digital/verovio/blob/develop/COPYING.LESSER), with [GPL companion text](https://github.com/rism-digital/verovio/blob/develop/COPYING) |
| FluidSynth | MIDI/SoundFont audio rendering; separate executable | [LGPL-2.1-or-later](https://github.com/FluidSynth/fluidsynth/blob/master/LICENSE) |
| Poppler / pdftoppm | Render original PDF evidence | [Upstream project](https://poppler.freedesktop.org/); check the installed package's license and bundled-library notices |
| FFmpeg | Optional MP3 export; separate executable | [LGPL/GPL depending on build options](https://www.ffmpeg.org/legal.html) |
| General MIDI SoundFont | Instrument samples supplied by the user | Bank-specific license; review redistribution and generated-audio terms for the selected bank |

Python dependencies are declared in `pyproject.toml` and pinned in the runtime requirements. Their wheel metadata includes license information and notices; transitive native libraries can carry additional terms. Installing dependencies from upstream is separate from relicensing or bundling them.

When redistributing a built runtime or modified upstream engine, preserve its notices and satisfy its distribution terms, including applicable source provisions. This repository's source package does not grant rights to a user's sheet music or recordings.
