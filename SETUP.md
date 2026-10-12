# Install the runtime

Choir Assistant requires Python **3.12 or later**, Audiveris, FluidSynth, a General
MIDI SoundFont, and Poppler (`pdftoppm`). FFmpeg is optional for MP3 export.
The plugin contains Python code and the browser workbench; it does not contain
these applications, Java, SoundFonts, or a user's scores.

## 1. Install system applications

On macOS, install [Homebrew](https://brew.sh/) if you use it, then run:

```sh
brew install python@3.13 fluidsynth poppler ffmpeg
```

Download Audiveris's matching macOS installer from its
[official releases](https://github.com/Audiveris/audiveris/releases), and install
the application. Its official installer includes the matching Java runtime.
Use an Audiveris, Java, and native-library build matching your CPU architecture.

On Debian/Ubuntu with Python 3.12 or later available:

```sh
sudo apt install python3-venv fluidsynth poppler-utils ffmpeg timgm6mb-soundfont
```

Install the matching Audiveris `.deb` from the same official releases. On other
Linux distributions, install the corresponding applications using the system's
package manager. Older distributions with Python 3.11 need a separate Python
3.12+ interpreter before the next step. An Audiveris launcher must be callable
from the terminal; a sandboxed desktop launcher may need an explicit wrapper.

Choose a licensed General MIDI `.sf2`/`.sf3` SoundFont. On Linux the standard
`FluidR3_GM.sf2`, `TimGM6mb.sf2`, and `default.sf2` installation paths are detected.
On macOS, or with a different font, set its path:

```sh
export CHOIR_ASSISTANT_SOUNDFONT="/absolute/path/to/General-MIDI.sf2"
```

SoundFonts have their own licenses. Do not treat the plugin's MIT license as a
license for the sound bank or imported scores.

## 2. Install the Python package

From a checkout or installed plugin directory, run:

```sh
python3 scripts/bootstrap.py --venv "$HOME/.local/share/choir-assistant/venv"
```

Use the actual Python 3.12+ command if `python3` points to an older interpreter.
This creates a virtual environment, installs the pinned dependency closure in
`requirements.txt`, builds a temporary copy of the plugin, installs a regular
wheel, and runs `pip check` and dependency diagnostics. It does not modify the
plugin installation or install global Python packages. Omitting `--venv` uses
the platform's user data directory. An existing non-venv directory is rejected.

For a disconnected installation, prepare wheels for the target OS, CPU, and
Python version, including `pip==26.2.1` and `setuptools==84.0.0`, then use:

```sh
python3 scripts/bootstrap.py --venv /writable/choir-venv --wheelhouse /path/to/wheels
```

The system applications must be installed separately. `--skip-doctor` installs
Python first when those applications will be configured later.

## 3. Check and use it

Activate the environment (macOS/Linux):

```sh
source "$HOME/.local/share/choir-assistant/venv/bin/activate"
choir-assistant doctor --json
choir-assistant import "/path/to/score.pdf" --output "/path/to/rehearsal-project"
```

Doctor reports missing Python packages, unavailable executables, invalid
overrides, and a malformed SoundFont. Its Audiveris check verifies configured
launcher/Java availability; the first PDF import actually runs the engine and
keeps its log. The full import never silently substitutes an existing score if
recognition fails. A successful import opens a local workbench. The source review
queue retains uncertain notation for human/agent review before corrected audio
can be presented as verified.

## Explicit configuration

Set these in the terminal that launches Codex or the CLI. Each executable setting
is **one executable path**, not a command with arguments.

| Variable | Meaning |
| --- | --- |
| `CHOIR_ASSISTANT_DATA_DIR` | Writable data directory; shared recognition cache is inside `cache/` |
| `CHOIR_ASSISTANT_AUDIVERIS` | Official Audiveris executable/launcher |
| `CHOIR_ASSISTANT_AUDIVERIS_JAR` | Audiveris jar; overrides automatic launcher discovery |
| `CHOIR_ASSISTANT_JAVA` | Java executable for a jar installation |
| `CHOIR_ASSISTANT_AUDIVERIS_CLASSPATH` | Full Java classpath including the application, dependencies, and platform-native jars |
| `CHOIR_ASSISTANT_TESSDATA` | Optional external OCR language-data directory |
| `CHOIR_ASSISTANT_FLUIDSYNTH` | FluidSynth executable |
| `CHOIR_ASSISTANT_SOUNDFONT` | General MIDI `.sf2`/`.sf3` file |
| `CHOIR_ASSISTANT_PDFTOPPM` | Poppler PDF renderer executable |
| `CHOIR_ASSISTANT_FFMPEG` | Optional FFmpeg executable |

For a custom jar installation on macOS/Linux:

```sh
export CHOIR_ASSISTANT_AUDIVERIS_JAR="/path/to/audiveris/lib/audiveris.jar"
export CHOIR_ASSISTANT_JAVA="/path/to/matching-jdk/bin/java"
export CHOIR_ASSISTANT_AUDIVERIS_CLASSPATH="/path/to/audiveris/lib/*:/path/to/platform-native-jars/*"
export CHOIR_ASSISTANT_TESSDATA="/path/to/tessdata"
```

Prefer the official launcher for ordinary installations; it supplies matching
Java and native dependencies. The application runs in headless batch mode.
Standard dynamic-library environment variables are inherited for custom
FluidSynth builds; no development-machine library locations are injected.

The default user data location is `~/Library/Application Support/choir-assistant`
on macOS and `${XDG_DATA_HOME:-~/.local/share}/choir-assistant` on Linux. A project
output directory is always explicit. Windows discovery helpers exist, but the
release acceptance workflow currently targets macOS/Linux.
