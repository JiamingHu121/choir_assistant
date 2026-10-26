"""Online content watermarks stop stale exports overwriting musical changes."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock
from choir_assistant.deployment.export import local_content_heads, validate_content_heads
from choir_assistant.deployment.sync import verify_editor_heads
from choir_assistant.deployment import isolated

class EditorHeadTests(TestCase):
    def test_strict_scopes_and_digest_values(self):
        for bad in ({'../work/main':'a'*64},{'work/main':'bad'},{'work/main':{'path':'private'}},{'work/a/b':'a'*64}):
            with self.assertRaises(ValueError): validate_content_heads(bad)
        self.assertEqual(validate_content_heads({'work/main':'a'*64}),{'work/main':'a'*64})

    def test_missing_mismatched_heads_fail_closed(self):
        self.assertTrue(verify_editor_heads({},{}))
        self.assertTrue(verify_editor_heads({'w/m':'a'*64},{'w/m':'a'*64}))
        for local in ({},{'w/m':'b'*64}):
            with self.assertRaisesRegex(RuntimeError,'pull and review'): verify_editor_heads(local,{'w/m':'a'*64})

    def test_local_watermark_file_is_bounded_and_rejects_links(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.assertEqual(local_content_heads(root),{})
            path=root/'.online-editor-heads.json'
            path.write_text(json.dumps({'schemaVersion':1,'contentHeads':{'w/m':'a'*64}}))
            self.assertEqual(local_content_heads(root),{'w/m':'a'*64})
            path.rename(root/'real');path.symlink_to(root/'real')
            with self.assertRaises(ValueError): local_content_heads(root)

    def test_isolated_reads_only_digest_map_using_root_owned_protocol(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(isolated,'SYSTEM_OWNER',os.getuid()):
            root=Path(temp).resolve();editing=root/'editing';public=editing/'public';public.mkdir(parents=True)
            protocol=editing/'protocol.json';protocol.write_text(json.dumps({'schemaVersion':1,'builderUid':403,'editorProtocol':1}));protocol.chmod(0o644)
            heads=public/'heads.json';heads.write_text(json.dumps({'schemaVersion':1,'heads':{'w/m':{'editHead':'a'*64,'bundle':'private/not-returned'}}}));heads.chmod(0o644)
            site=object.__new__(isolated.IsolatedSite);site.root=root
            self.assertEqual(site.editor_heads(),{'w/m':'a'*64})
            heads.chmod(0o666)
            with self.assertRaises(ValueError): site.editor_heads()
            heads.chmod(0o644);heads.rename(public/'real');heads.symlink_to(public/'real')
            with self.assertRaises(ValueError): site.editor_heads()

    def test_absent_metadata_keeps_legacy_sites_compatible(self):
        with tempfile.TemporaryDirectory() as temp:
            site=object.__new__(isolated.IsolatedSite);site.root=Path(temp)
            self.assertIsNone(site.editor_heads())
