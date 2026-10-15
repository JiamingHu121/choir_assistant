# Choir Assistant v0.3.1

Includes the independent library website and incremental publishing workflow
from [v0.3.0](RELEASE-v0.3.0.md), with a portable test-fixture correction.

The source-PDF identity tests already used a synthesized PCM fixture and mocked
MP3 encoding, but still called the host's external FFmpeg audio fitter. They now
isolate that remaining encoder call; the fixture already has the exact MIDI
duration. All five tests pass with FFmpeg absent from PATH. Production audio,
music semantics, website runtime and deployment code are unchanged.

Setup instructions now distinguish FFmpeg's authoring role from the standalone
website's dependency-free PCM fitting. Source review remains required before
claiming transcription accuracy; no microphone accuracy claim is added.
