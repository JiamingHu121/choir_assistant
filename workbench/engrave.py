"""Render canonical MusicXML to local SVG while checking note identity coverage."""
from pathlib import Path
import xml.etree.ElementTree as ET
import verovio
from workbench.normalize_engraving import normalize_with_report

def engrave(source,output):
    toolkit=verovio.toolkit()
    toolkit.setOptions({'inputFrom':'musicxml','pageWidth':2200,'pageHeight':2800,
                        'scale':38,'adjustPageHeight':True,'breaks':'auto',
                        'svgViewBox':True,'svgBoundingBoxes':True,'footer':'none','header':'none'})
    xml,report=normalize_with_report(source)
    (Path(output)/'engraving-normalization.json').write_text(__import__('json').dumps(report,ensure_ascii=False,indent=2))
    if not toolkit.loadData(xml):raise ValueError('Verovio 无法载入规范曲谱')
    pages=[];ids={}
    for page in range(1,toolkit.getPageCount()+1):
        svg=toolkit.renderToSVG(page)
        root=ET.fromstring(svg)
        for element in root.iter():
            if 'note' in element.get('class','').split():
                note_id=element.get('id')
                if note_id:ids[note_id]=page
        filename=f'notation-{page}.svg';(Path(output)/filename).write_text(svg)
        pages.append({'page':page,'svg':f'/assets/{filename}'})
    (Path(output)/'canonical.mei').write_text(toolkit.getMEI())
    return pages,ids
