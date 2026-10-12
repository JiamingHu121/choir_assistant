"""Regenerate the original MIT-licensed practice fixture (optional: svglib/reportlab)."""
from pathlib import Path
from fractions import Fraction
from music21 import stream, note, meter, tempo, metadata, instrument, tie, spanner, clef
import verovio
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent


def generate():
    score = stream.Score()
    score.metadata = metadata.Metadata(title="Practice Study", composer="Choir Assistant")
    part = stream.Part(id="Piano")
    part.partName = "Piano"
    part.insert(0, instrument.Piano())
    bars = [
        [("C4", 1), ("D4", 1), ("E4", 1), ("G4", 1)],
        [("F4", 1), ("E4", 1), ("D4", 1), ("C4", 1)],
        [("E4", 2), ("G4", 1), ("A4", 1)],
        [("G4", 1.5), ("F4", .5), ("E4", 2)],
        [("D4", 2), ("D4", 1), ("E4", 1)],
        [("F4", .5), ("G4", .5), ("A4", .5), ("G4", .5), ("F4", 1), ("E4", 1)],
        [("C4", Fraction(1, 3)), ("D4", Fraction(1, 3)), ("E4", Fraction(1, 3)), ("F4", 1), ("G4", 2)],
        [("C4", 4)],
    ]
    for number, entries in enumerate(bars, 1):
        bar = stream.Measure(number=number)
        if number == 1:
            bar.insert(0, clef.TrebleClef())
            bar.insert(0, meter.TimeSignature("4/4"))
            bar.insert(0, tempo.MetronomeMark(number=96))
        for pitch, duration in entries:
            bar.append(note.Note(pitch, quarterLength=duration))
        if number == 5:
            bar.notes[0].tie = tie.Tie("start")
            bar.notes[1].tie = tie.Tie("stop")
        if number == 6:
            part.insert(0, spanner.Slur(bar.notes[0], bar.notes[3]))
        part.append(bar)
    score.append(part)
    xml_path = ROOT / "practice-study.musicxml"
    score.write("musicxml", fp=str(xml_path))
    toolkit = verovio.toolkit()
    toolkit.setOptions({"inputFrom": "musicxml", "pageWidth": 2100,
                        "pageHeight": 2970, "scale": 50, "adjustPageHeight": False})
    if not toolkit.loadFile(str(xml_path)):
        raise RuntimeError("Example engraving failed")
    svg_path = ROOT / "practice-study.svg"
    svg = ET.fromstring(toolkit.renderToSVG(1))
    ns = "{http://www.w3.org/2000/svg}"
    # Cairo does not read the embedded Leipzig WOFF font. Draw the quarter
    # metronome symbol explicitly so this authoring export remains printable.
    for group in svg.iter():
        if group.get("class") != "tempo":
            continue
        text = group.find(ns + "text")
        x, y = int(text.get("x")), int(text.get("y"))
        group.remove(text)
        ET.SubElement(group, ns + "ellipse", {"cx": str(x + 80), "cy": str(y - 70),
            "rx": "80", "ry": "52", "transform": f"rotate(-20 {x + 80} {y - 70})"})
        ET.SubElement(group, ns + "path", {"d": f"M {x + 155} {y - 70} V {y - 470}",
            "stroke": "black", "stroke-width": "24"})
        label = ET.SubElement(group, ns + "text", {"x": str(x + 235), "y": str(y),
            "font-size": "405px", "font-family": "Times, serif"})
        label.text = "= 96"
    svg_path.write_text(ET.tostring(svg, encoding="unicode"), encoding="utf-8")
    return svg_path


if __name__ == "__main__":
    generate()
