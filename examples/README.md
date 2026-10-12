# Original practice score

`practice-study.pdf` is an original eight-measure study composed for this project,
distributed under the repository MIT license. It contains a tempo marking,
quarters, eighths, a dotted note, a same-pitch tie, a phrase slur and a triplet.
`practice-study.musicxml` is the corresponding ground truth; importing the PDF
still runs recognition and does not substitute this XML for OMR output.

Regenerate the MusicXML/SVG with `python examples/generate_demo.py`. To create
the PDF from the SVG, install optional `cairosvg` and a Cairo library and call
`cairosvg.svg2pdf(url="examples/practice-study.svg", write_to="examples/practice-study.pdf", background_color="white")`.
These are fixture authoring dependencies, not workbench runtime dependencies.
