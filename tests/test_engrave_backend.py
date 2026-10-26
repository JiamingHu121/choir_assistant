"""The optional local WASM renderer keeps the native engraving contract."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from workbench import engrave


class EngravingBackendTests(unittest.TestCase):
    def test_missing_fallback_configuration_fails_without_searching_or_downloading(self):
        with mock.patch.dict('os.environ', {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'Configure both'):
                engrave._node_render('<score-partwise/>')

    def test_wasm_output_maps_note_ids_and_writes_mei(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg"><g class="note" id="n-P1-m1-o1"/></svg>'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.musicxml'
            source.write_text('<score-partwise/>')
            with mock.patch.object(engrave, 'verovio', None), \
                    mock.patch.object(engrave, 'normalize_with_report', return_value=('<canonical/>', {})), \
                    mock.patch.object(engrave, '_node_render', return_value={'pages': [svg], 'mei': '<mei/>', 'version': '6.3.0'}):
                pages, ids = engrave.engrave(source, root)
            self.assertEqual(pages, [{'page': 1, 'svg': '/assets/notation-1.svg'}])
            self.assertEqual(ids, {'n-P1-m1-o1': 1})
            self.assertEqual((root / 'canonical.mei').read_text(), '<mei/>')
            self.assertEqual(json.loads((root / 'engraving-normalization.json').read_text())['renderer']['backend'], 'node-wasm')

    def test_native_remains_default_when_available(self):
        toolkit = mock.Mock()
        toolkit.loadData.return_value = True
        toolkit.getPageCount.return_value = 1
        toolkit.renderToSVG.return_value = '<svg/>'
        toolkit.getMEI.return_value = '<mei/>'
        toolkit.getVersion.return_value = '6.3.0'
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(engrave, 'verovio', mock.Mock(toolkit=lambda: toolkit)), \
                    mock.patch.object(engrave, 'normalize_with_report', return_value=('<canonical/>', {})), \
                    mock.patch.object(engrave, '_node_render') as fallback:
                engrave.engrave(Path(directory) / 'source.musicxml', directory)
                fallback.assert_not_called()
                toolkit.setOptions.assert_called_once_with(engrave.OPTIONS)


if __name__ == '__main__':
    unittest.main()
