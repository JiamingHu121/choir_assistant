"""Render canonical MusicXML to local SVG while checking note identity coverage."""
from pathlib import Path
import json
import os
import subprocess
import xml.etree.ElementTree as ET
try:
    import verovio
except ImportError:
    verovio = None
from workbench.normalize_engraving import normalize_with_report

OPTIONS = {'inputFrom':'musicxml','pageWidth':2200,'pageHeight':2800,
           'scale':38,'adjustPageHeight':True,'breaks':'auto',
           'svgViewBox':True,'svgBoundingBoxes':True,'footer':'none','header':'none'}


def _node_render(xml):
    """Use only explicitly configured local Node and Verovio WASM paths."""
    from runtime import find_binary
    node = os.environ.get('CHOIR_ASSISTANT_VEROVIO_NODE')
    module = os.environ.get('CHOIR_ASSISTANT_VEROVIO_MODULE')
    if not node or not module:
        raise RuntimeError('Native Verovio is unavailable. Configure both CHOIR_ASSISTANT_VEROVIO_NODE and CHOIR_ASSISTANT_VEROVIO_MODULE for the local WASM fallback.')
    module = Path(module).expanduser().resolve()
    if not module.exists():
        raise RuntimeError('Configured Verovio WASM module is unavailable')
    executable = find_binary('node', node)
    result = subprocess.run([executable, str(Path(__file__).with_name('engrave_node.cjs')),
                             str(module), json.dumps(OPTIONS)], input=xml,
                            text=True, capture_output=True, timeout=180, check=False)
    if result.returncode:
        raise RuntimeError('Verovio WASM engraving failed: ' + result.stderr[-1600:])
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise RuntimeError('Verovio WASM returned invalid output') from error
    if (not isinstance(payload, dict) or not isinstance(payload.get('pages'), list)
            or not payload['pages'] or any(not isinstance(svg, str) for svg in payload['pages'])
            or not isinstance(payload.get('mei'), str)):
        raise RuntimeError('Verovio WASM output is incomplete')
    return payload


def engrave(source,output):
    xml,report=normalize_with_report(source)
    if verovio is None:
        payload=_node_render(xml)
        report['renderer']={'backend':'node-wasm','version':payload.get('version')}
    else:
        toolkit=verovio.toolkit()
        toolkit.setOptions(OPTIONS)
        if not toolkit.loadData(xml):raise ValueError('Verovio 无法载入规范曲谱')
        payload={'pages':[toolkit.renderToSVG(page) for page in range(1,toolkit.getPageCount()+1)],
                 'mei':toolkit.getMEI()}
        report['renderer']={'backend':'native','version':toolkit.getVersion()}
    (Path(output)/'engraving-normalization.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    pages=[];ids={}
    for page,svg in enumerate(payload['pages'],1):
        root=ET.fromstring(svg)
        for element in root.iter():
            if 'note' in element.get('class','').split():
                note_id=element.get('id')
                if note_id:ids[note_id]=page
        filename=f'notation-{page}.svg';(Path(output)/filename).write_text(svg)
        pages.append({'page':page,'svg':f'/assets/{filename}'})
    (Path(output)/'canonical.mei').write_text(payload['mei'])
    return pages,ids
