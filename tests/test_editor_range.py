"""Fixed-head range download, bounded retries and publication-safe failures."""
from contextlib import redirect_stderr
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from choir_assistant.deployment import editor_sync


class Response:
    def __init__(self, url, data, start, last, total, status=206):
        self.url=url; self.status=status; self.body=io.BytesIO(data); self.headers=Message()
        self.headers['Content-Length']=str(last-start+1)
        self.headers['Content-Range']='bytes %s-%s/%s'%(start,last,total)
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def geturl(self): return self.url
    def read(self,limit): return self.body.read(limit)
    read1=read


class EditorRangeTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.target=Path(self.temporary.name)/'snapshot.tar';self.target.write_bytes(b'old complete file')
        self.clock=[0.0];self.calls=[];self.payload=b'abcdefghijklmnopq'
        self.head='a'*64
        for patch in (mock.patch.object(editor_sync,'EXPORT_RANGE_BYTES',4),
                      mock.patch.object(editor_sync.time,'monotonic',side_effect=lambda:self.clock[0]),
                      mock.patch.object(editor_sync.time,'sleep',side_effect=lambda seconds:self.clock.__setitem__(0,self.clock[0]+seconds))):
            patch.start();self.addCleanup(patch.stop)
    def client(self, fault=None):
        case=self
        class Opener:
            def open(self,request,timeout):
                start,last=map(int,re.fullmatch(r'bytes=(\d+)-(\d+)',request.get_header('Range')).groups())
                case.calls.append((start,last,case.clock[0],request.full_url,timeout))
                if fault:
                    result=fault(request,start,last,len(case.calls))
                    if result is not None:return result
                end=min(last,len(case.payload)-1)
                return Response(request.full_url,case.payload[start:end+1],start,end,len(case.payload))
        result=editor_sync.HTTPSPullClient('https://fixture.example','not-used',opener=Opener());result.logged_in.add('study');return result
    def download(self,client):
        with redirect_stderr(io.StringIO()) as progress:client.download('study','main',self.head,self.target)
        self.assertNotIn('not-used',progress.getvalue())
    def unchanged(self):
        self.assertEqual(self.target.read_bytes(),b'old complete file')
        self.assertFalse(list(self.target.parent.glob('.editor-export-*')))
    def test_all_segments_are_pinned_and_rate_limited_to_three_per_window(self):
        self.download(self.client())
        self.assertEqual(self.target.read_bytes(),self.payload)
        self.assertEqual([(a,b) for a,b,*_ in self.calls],[(0,3),(4,7),(8,11),(12,15),(16,16)])
        self.assertEqual([row[2] for row in self.calls],[0,0,0,61,61])
        self.assertTrue(all(row[3].endswith('export?head='+self.head) and row[4]<=120 for row in self.calls))
    def test_short_segment_is_discarded_and_only_that_segment_is_retried(self):
        def fault(request,start,last,count):
            if count==2:return Response(request.full_url,b'x',start,last,len(self.payload))
        self.download(self.client(fault))
        self.assertEqual(self.target.read_bytes(),self.payload)
        self.assertEqual([row[0] for row in self.calls][:4],[0,4,4,8])
    def test_changed_total_after_success_or_failed_first_segment_aborts(self):
        for first_short in (False,True):
            self.calls.clear();self.clock[0]=0
            def fault(request,start,last,count):
                if first_short and count==1:return Response(request.full_url,b'x',start,last,len(self.payload))
                if count==2:return Response(request.full_url,self.payload[start:last+1],start,last,len(self.payload)+1)
            with self.subTest(first_short=first_short), self.assertRaisesRegex(ValueError,'total changed'):
                self.download(self.client(fault))
            self.unchanged();self.assertEqual(len(self.calls),2)
    def test_429_waits_one_window_then_retries_same_range(self):
        self.payload=b'abcd'
        def fault(request,start,last,count):
            if count==1:raise HTTPError(request.full_url,429,'limited',Message(),io.BytesIO())
        self.download(self.client(fault));self.assertEqual(self.target.read_bytes(),self.payload)
        self.assertEqual([row[2] for row in self.calls],[0,61])
    def test_repeated_429_is_bounded_and_cas_409_is_immediate(self):
        for code,expected in ((429,2),(409,1)):
            self.calls.clear();self.clock[0]=0
            def fault(request,*args):raise HTTPError(request.full_url,code,'fixed',Message(),io.BytesIO())
            with self.subTest(status=code),self.assertRaisesRegex(RuntimeError,'HTTP '+str(code)):
                self.download(self.client(fault))
            self.assertEqual(len(self.calls),expected);self.unchanged()
    def test_timeout_retries_are_bounded_and_do_not_replace_destination(self):
        def fault(*args):raise URLError(TimeoutError())
        with self.assertRaisesRegex(RuntimeError,'bounded retries'):self.download(self.client(fault))
        self.assertEqual(len(self.calls),3);self.unchanged()
    def test_ignored_range_and_wrong_content_length_do_not_retry(self):
        for ignored in (False,True):
            self.calls.clear();self.clock[0]=0
            def fault(request,start,last,count):
                result=Response(request.full_url,self.payload[start:last+1],start,last,len(self.payload),200 if ignored else 206)
                if not ignored:result.headers.replace_header('Content-Length','3')
                return result
            with self.subTest(ignored=ignored),self.assertRaises(ValueError):self.download(self.client(fault))
            self.assertEqual(len(self.calls),1);self.unchanged()
    def test_overall_deadline_stops_before_another_request(self):
        def fault(request,start,last,count):self.clock[0]=1201;raise URLError(TimeoutError())
        with self.assertRaisesRegex(RuntimeError,'complete-download deadline'):self.download(self.client(fault))
        self.assertEqual(len(self.calls),1);self.unchanged()


class RealRangeTests(unittest.TestCase):
    def test_real_http_ranges_reassemble_an_identical_archive(self):
        data=bytes(range(256))*33;calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                start,last=map(int,re.fullmatch(r'bytes=(\d+)-(\d+)',self.headers['Range']).groups());last=min(last,len(data)-1);calls.append((start,last))
                body=data[start:last+1];self.send_response(206);self.send_header('Content-Range','bytes %s-%s/%s'%(start,last,len(data)));self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start();self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        class Opener:
            def open(self,request,timeout):
                response=urlopen(__import__('urllib.request',fromlist=['Request']).Request('http://127.0.0.1:%s/'%server.server_port,headers={'Range':request.get_header('Range')}),timeout=timeout);response.url=request.full_url;return response
        client=editor_sync.HTTPSPullClient('https://fixture.example','not-used',opener=Opener());client.logged_in.add('study')
        with tempfile.TemporaryDirectory() as folder,mock.patch.object(editor_sync,'EXPORT_RANGE_BYTES',4096),redirect_stderr(io.StringIO()):
            target=Path(folder)/'snapshot';client.download('study','main','a'*64,target);self.assertEqual(target.read_bytes(),data)
        self.assertEqual(calls,[(0,4095),(4096,8191),(8192,8447)])
