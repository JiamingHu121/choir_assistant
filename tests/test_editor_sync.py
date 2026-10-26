"""Authenticated pull imports music, not just watermarks, without contacting a host."""
from copy import deepcopy
from hashlib import sha256
import io
from email.message import Message
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
from unittest import TestCase, mock
from urllib.error import HTTPError
import test_site_export as fixtures
from choir_assistant.deployment import catalog, editor_sync, sync


def snapshot(bundle, root, source_hash, work='study', movement='main', revision='online-v2', corrupt_ledger=False, model_transform=None):
    folder=root/'snapshot';shutil.copytree(bundle,folder/'bundle')
    model_path=folder/'bundle/assets/interactive.json';model=json.loads(model_path.read_text());model['revision']=revision;model['notes'][0]['pitch']=61
    if model_transform:model_transform(model)
    model_path.write_bytes(editor_sync.encoded(model))
    manifest_path=folder/'bundle/manifest.json';manifest=json.loads(manifest_path.read_text());manifest['revision']=revision
    for item in manifest['artifacts']:
        path=folder/'bundle'/item['path'];item['sha256']=catalog.digest(path)
        if 'bytes' in item:item['bytes']=path.stat().st_size
    manifest_path.write_bytes(editor_sync.encoded(manifest))
    ledger=[{'origin':'user-edit','reason':'Raise one semitone','typedOperations':[{'op':'pitch.delta','noteIds':['n1'],'semitones':1}]}]
    commit={'work':work,'movement':movement,'revision':revision,'sourceSha256':source_hash,'bundleManifestSha256':catalog.digest(manifest_path),'ledger':ledger,'ledgerSha256':('b'*64 if corrupt_ledger else sha256(editor_sync.encoded(ledger)).hexdigest()),'parent':'baseline','reason':'Raise one semitone'}
    head=sha256(editor_sync.encoded(commit)).hexdigest();commit['editHead']=head
    for name,value in (('commit.json',commit),('head.json',{k:v for k,v in commit.items() if k!='ledger'}),('ledger.json',ledger)):(folder/name).write_bytes(editor_sync.encoded(value))
    descriptor={'schemaVersion':1,'work':work,'movement':movement,'editHead':head,'revision':revision,'sourceSha256':source_hash,'bundleManifestSha256':catalog.digest(manifest_path),'files':[{'path':p.relative_to(folder).as_posix(),'bytes':p.stat().st_size,'sha256':catalog.digest(p)} for p in sorted(folder.rglob('*')) if p.is_file()]}
    (folder/'editor-export.json').write_bytes(editor_sync.encoded(descriptor))
    archive=root/'snapshot.tar'
    with tarfile.open(archive,'w') as tar:
        for path in sorted(folder.rglob('*')):
            if path.is_file():tar.add(path,arcname=path.relative_to(folder).as_posix(),recursive=False)
    return archive,head,folder


class EditorPullTests(TestCase):
    def setUp(self):
        self.fixture=fixtures.SiteExportTests('runTest');self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root;self.library=self.fixture.library
        entry=catalog.entries(self.library)['study'];self.entry_path=self.library/'works/study/entry.json';self.entry=deepcopy(entry)
        movement=entry['movements'][0];bundle,project=catalog.active_bundle(movement,self.library)
        self.pdf,self.source_hash=catalog.source_pdf(bundle,project,entry,self.library)
        self.original_pdf=self.pdf.read_bytes();self.archive,self.head,self.folder=snapshot(bundle,self.root,self.source_hash)
        self.password=self.root/'password';self.password.write_text('secret-for-test\n');self.password.chmod(0o600)
        self.profile={'libraryRoot':str(self.library),'publicUrl':'https://scores.example.org','editorSync':{'enabled':True,'passwordFile':str(self.password)}}

    def test_full_pull_changes_actual_bundle_and_backs_up_entry_and_catalog(self):
        cached={'schemaVersion':1,'works':[{'id':'study','title':'Study','movements':[{'id':'main','revision':'fixture-v1'}]}]};catalog.write_json(self.library/'catalog.json',cached)
        calls=[]
        def download(work,movement,head,destination):calls.append((work,movement,head));shutil.copyfile(self.archive,destination)
        result=editor_sync.pull_online_edits(self.profile,{'study/main':self.head},mock.Mock(download=download))
        entry=catalog.entries(self.library)['study'];movement=entry['movements'][0]
        self.assertNotEqual(movement['bundle'],self.entry['movements'][0].get('bundle'))
        imported=json.loads((Path(movement['bundle'])/'assets/interactive.json').read_text());self.assertEqual(imported['revision'],'online-v2');self.assertEqual(imported['notes'][0]['pitch'],61)
        self.assertFalse(movement['sourceReviewed']);self.assertEqual(movement['onlineEditHead'],self.head)
        self.assertNotIn('notationOverlay',movement)
        self.assertEqual(self.pdf.read_bytes(),self.original_pdf)
        self.assertEqual(json.loads((Path(result['backup'])/'works/study/entry.json').read_text()),self.entry)
        self.assertTrue((Path(result['backup'])/'catalog.json').is_file())
        self.assertEqual(catalog.read_json(self.library/'catalog.json')['works'][0]['movements'][0]['revision'],'online-v2')
        self.assertEqual(editor_sync.local_content_heads(self.library),{'study/main':self.head})
        result2=editor_sync.pull_online_edits(self.profile,{'study/main':self.head},mock.Mock(download=download));self.assertEqual(result2,{'pulled':[]});self.assertEqual(len(calls),1)

    def test_bad_source_and_ledger_fail_before_switching_entry_or_watermark(self):
        def download(w,m,h,d):shutil.copyfile(self.archive,d)
        original=self.entry_path.read_bytes()
        with self.assertRaises(ValueError):editor_sync.pull_online_edits(self.profile,{'study/main':'c'*64},mock.Mock(download=download))
        self.assertEqual(self.entry_path.read_bytes(),original);self.assertFalse((self.library/'.online-editor-heads.json').exists())
        with self.assertRaisesRegex(ValueError,'source PDF'):editor_sync.unpack_snapshot(self.archive,self.root/'wrong-source','study','main',self.head,'0'*64)
        alt=self.root/'alt';alt.mkdir();bad,head,_=snapshot(self.fixture.bundle,alt,self.source_hash,corrupt_ledger=True)
        with self.assertRaisesRegex(ValueError,'ledger'):editor_sync.unpack_snapshot(bad,self.root/'bad-ledger','study','main',head,self.source_hash)

    def test_archive_duplicate_links_and_escape_are_rejected(self):
        for index,(name,kind) in enumerate((('../escape','regular'),('bundle/link','symlink'),('head.json','duplicate'))):
            bad=self.root/f'bad-{index}.tar'
            with tarfile.open(bad,'w') as tar:
                item=tarfile.TarInfo(name);item.size=1
                if kind=='symlink':item.type=tarfile.SYMTYPE;item.linkname='/tmp/elsewhere';item.size=0
                tar.addfile(item,io.BytesIO(b'x') if item.size else None)
                if kind=='duplicate':tar.addfile(item,io.BytesIO(b'x'))
            with self.assertRaises((ValueError,tarfile.TarError)):editor_sync.unpack_snapshot(bad,self.root/f'extract-{index}','study','main',self.head,self.source_hash)
        self.assertFalse((self.root/'escape').exists())

    def test_snapshot_file_hash_tamper_is_rejected(self):
        (self.folder/'bundle/assets/interactive.json').write_bytes(b'{}')
        bad=self.root/'tampered.tar'
        with tarfile.open(bad,'w') as tar:
            for path in self.folder.rglob('*'):
                if path.is_file():tar.add(path,arcname=path.relative_to(self.folder).as_posix(),recursive=False)
        with self.assertRaisesRegex(ValueError,'SHA-256'):editor_sync.unpack_snapshot(bad,self.root/'tampered','study','main',self.head,self.source_hash)

    def test_password_owner_only_and_no_symlink(self):
        self.assertEqual(editor_sync.password_from_file(self.password),'secret-for-test')
        self.password.chmod(0o644)
        with self.assertRaises(ValueError):editor_sync.password_from_file(self.password)
        self.password.chmod(0o600);link=self.root/'password-link';link.symlink_to(self.password)
        with self.assertRaises(ValueError):editor_sync.password_from_file(link)


class NetworkContractTests(TestCase):
    def test_https_login_password_only_in_post_body_and_snapshot_is_head_bound(self):
        calls=[]
        class Response:
            def __init__(self,url,body):
                self.url=url;self.body=io.BytesIO(body);self.status=200;self.headers=Message();self.headers['Content-Length']=str(len(body))
                if 'api/editor/export?' in url:
                    self.status=206;self.headers['Content-Range']='bytes 0-%s/%s'%(len(body)-1,len(body))
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def geturl(self):return self.url
            def read(self,limit):return self.body.read(limit)
        class Opener:
            def open(self,request,timeout):
                calls.append(request)
                body=b'{"csrfToken":"token"}' if request.full_url.endswith('site-config') else b'{"authenticated":true}' if request.full_url.endswith('login') else b'tar-fixture'
                return Response(request.full_url,body)
        with tempfile.TemporaryDirectory() as temp:
            client=editor_sync.HTTPSPullClient('https://scores.example.org','test-secret',opener=Opener());path=Path(temp)/'snapshot'
            client.download('work','main','a'*64,path)
            self.assertEqual(path.read_bytes(),b'tar-fixture');self.assertTrue(calls[-1].full_url.endswith('export?head='+'a'*64))
            self.assertNotIn('test-secret',str([r.full_url for r in calls]));self.assertEqual(json.loads(calls[1].data)['password'],'test-secret');self.assertEqual(calls[1].get_method(),'POST')
        with self.assertRaises(ValueError):editor_sync.HTTPSPullClient('http://scores.example.org','password')
        with self.assertRaises(HTTPError) as caught:editor_sync.NoRedirect().redirect_request(calls[0],None,302,'redirect',{},'https://elsewhere.example.org')
        caught.exception.close()

class SyncPullOrderingTests(TestCase):
    def setUp(self):
        import test_site_sync as fixtures_sync
        self.fixture=fixtures_sync.SiteSyncTests('runTest');self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.password=self.fixture.root/'password';self.password.write_text('example');self.password.chmod(0o600)
        self.profile=dict(self.fixture.profile,editorSync={'enabled':True,'passwordFile':str(self.password)})
        self.fixture.profile_path.write_text(json.dumps(self.profile))

    def test_profile_rejects_nonprivate_password_file(self):
        self.password.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'0600'):sync.load_profile(self.fixture.profile_path)

    def test_noncheck_pull_precedes_export(self):
        events=[]
        def pull(profile,heads):events.append('pull');return {'pulled':['fixture/main']}
        def export(*args):events.append('export');raise RuntimeError('export-stop')
        with mock.patch.object(sync,'_call',return_value={'editorHeads':{'fixture/main':'a'*64}}) as call,mock.patch.object(editor_sync,'pull_online_edits',side_effect=pull),mock.patch.object(sync,'_export',side_effect=export):
            with self.assertRaisesRegex(RuntimeError,'export-stop'):sync.sync_site(self.fixture.profile_path)
        self.assertEqual(events,['pull','export']);self.assertEqual(call.call_args.args[1],'inspect')

    def test_check_only_never_downloads_and_still_blocks_stale_music(self):
        from choir_assistant.deployment import remote
        release,entries,plan,archive=self.fixture.planned();state=remote.inspect(self.profile,self.fixture.service);state['editorHeads']={'fixture/main':'a'*64}
        with mock.patch.object(sync,'_export',return_value=(release,entries,plan['revisions'],{})),mock.patch.object(sync,'_call',return_value=state),mock.patch.object(editor_sync,'pull_online_edits') as pull:
            with self.assertRaisesRegex(RuntimeError,'pull and review'):sync.sync_site(self.fixture.profile_path,check_only=True)
        pull.assert_not_called()

class ImportedBundleBindingTests(EditorPullTests):
    def setUp(self):
        super().setUp()
        def download(work,movement,head,destination):shutil.copyfile(self.archive,destination)
        editor_sync.pull_online_edits(self.profile,{'study/main':self.head},mock.Mock(download=download))

    def test_export_accepts_only_actual_incorporated_bundle(self):
        from choir_assistant.deployment.export import local_content_heads
        self.assertEqual(local_content_heads(self.library,verify_bindings=True),{'study/main':self.head})
        metadata=catalog.read_json(self.library/'.online-editor-heads.json')
        self.assertEqual(metadata['bindings']['study/main']['revision'],'online-v2')
        self.assertEqual(metadata['bindings']['study/main']['bundleManifestSha256'],catalog.digest(Path(catalog.entries(self.library)['study']['movements'][0]['bundle'])/'manifest.json'))

    def test_re_register_old_project_cannot_inherit_online_watermark(self):
        from choir_assistant.deployment.export import export_site
        catalog.register_project(self.library,self.fixture.project,'study',overwrite=True)
        with self.assertRaisesRegex(ValueError,'merge/rebase'):export_site(self.root/'stale-rebuild-export',self.library)
        self.assertFalse((self.root/'stale-rebuild-export').exists())

    def test_export_checks_copied_bundle_not_a_later_entry_snapshot(self):
        from choir_assistant.deployment.export import export_site
        # An export selected the old entry just before another process imported
        # a new head. Checking a freshly reread entry would wrongly bless it.
        with mock.patch.object(catalog,'entries',return_value={'study':deepcopy(self.entry)}):
            with self.assertRaisesRegex(ValueError,'merge/rebase'):
                export_site(self.root/'raced-export',self.library)
        self.assertFalse((self.root/'raced-export').exists())

    def test_changed_imported_manifest_cannot_keep_old_binding(self):
        from choir_assistant.deployment.export import local_content_heads
        movement=catalog.entries(self.library)['study']['movements'][0]
        path=Path(movement['bundle'])/'manifest.json';value=catalog.read_json(path);value['revision']='offline-rebuilt';catalog.write_json(path,value)
        with self.assertRaisesRegex(ValueError,'merge/rebase'):local_content_heads(self.library,verify_bindings=True)

    # Parent scenarios are covered once by EditorPullTests before the binding setup.
    test_full_pull_changes_actual_bundle_and_backs_up_entry_and_catalog=None
    test_bad_source_and_ledger_fail_before_switching_entry_or_watermark=None
    test_archive_duplicate_links_and_escape_are_rejected=None
    test_snapshot_file_hash_tamper_is_rejected=None
    test_password_owner_only_and_no_symlink=None
