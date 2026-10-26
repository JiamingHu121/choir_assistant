"""Real HTTP backpressure, chunked decoding and short-EOF export boundaries."""
from email.message import Message
from hashlib import sha256
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.request import urlopen

from choir_assistant.deployment import editor_sync, site_server


class EditorTransferTests(unittest.TestCase):
    def serve(self, handler, server_class=ThreadingHTTPServer, **options):
        server = server_class(("127.0.0.1", 0), handler, **options)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    @staticmethod
    def loopback_opener(server):
        # Production still requires HTTPS and exact origin. This test transport
        # exercises Python's real HTTP framing without adding a TLS fixture CA.
        class Opener:
            def open(self, request, timeout):
                response = urlopen("http://127.0.0.1:%s/" % server.server_port, timeout=timeout)
                response.url = request.full_url
                return response
        return Opener()

    def test_slow_download_completes_beyond_single_socket_write_timeout(self):
        payload = bytes(range(256)) * 8192
        class Handler(site_server.Handler):
            def do_GET(self):
                self.bytes_response(payload, "application/x-tar", download="fixture.tar")
        class Server(site_server.BoundedHTTPServer):
            def get_request(self):
                stream, address = super().get_request()
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
                return stream, address
        server = self.serve(Handler, Server, read_timeout=1)
        connection = HTTPConnection(*server.server_address, timeout=10)
        self.addCleanup(connection.close)
        connection.connect()
        connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
        connection.request("GET", "/")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(int(response.headers["Content-Length"]), len(payload))
        started, received = time.monotonic(), io.BytesIO()
        while True:
            block = response.read(8192)
            if not block:
                break
            received.write(block)
            time.sleep(.01)
        self.assertGreater(time.monotonic() - started, 1)
        self.assertEqual(len(received.getvalue()), len(payload))
        self.assertEqual(sha256(received.getvalue()).digest(), sha256(payload).digest())

    def test_actual_chunked_response_without_content_length_is_accumulated(self):
        pieces = [b"first", b"x" * 32769, b"last"]
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(200); self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
                for piece in pieces:
                    self.wfile.write(("%x\r\n" % len(piece)).encode() + piece + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                self.close_connection = True
        server = self.serve(Handler)
        client = editor_sync.HTTPSPullClient("https://fixture.example", "not-used", opener=self.loopback_opener(server))
        self.assertEqual(client.request("/snapshot", maximum=40000), b"".join(pieces))

    def test_actual_short_eof_is_rejected_before_download_file_is_written(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.send_response(206); self.send_header("Content-Length", "100"); self.send_header("Content-Range", "bytes 0-99/100"); self.end_headers()
                self.wfile.write(b"partial")
                self.close_connection = True
        server = self.serve(Handler)
        client = editor_sync.HTTPSPullClient("https://fixture.example", "not-used", opener=self.loopback_opener(server))
        client.logged_in.add("study")
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "snapshot.tar"
            destination.write_bytes(b"previous complete snapshot")
            with self.assertRaisesRegex(RuntimeError, "segment failed after bounded retries"):
                client.download("study", "main", "a" * 64, destination)
            self.assertEqual(destination.read_bytes(), b"previous complete snapshot")

    def test_bounded_short_reads_continue_until_eof_and_oversize_is_rejected(self):
        class Response:
            status = 200
            def __init__(self, payload, length):
                self.stream = io.BytesIO(payload); self.headers = Message()
                self.headers["Content-Length"] = str(length)
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return "https://fixture.example/snapshot"
            def read(self, limit): return self.stream.read(min(3, limit))
        class Opener:
            def __init__(self, payload, length): self.payload, self.length = payload, length
            def open(self, *args, **kwargs): return Response(self.payload, self.length)
        client = editor_sync.HTTPSPullClient("https://fixture.example", "not-used", opener=Opener(b"abcdefghij", 10))
        self.assertEqual(client.request("/snapshot", maximum=10), b"abcdefghij")
        client.opener = Opener(b"abcdefghij", 11)
        with self.assertRaisesRegex(ValueError, "size limit"):
            client.request("/snapshot", maximum=10)

    def test_read1_deadline_expiry_rejects_late_body_or_eof(self):
        clock = [0]
        class Response:
            status = 200
            def __init__(self, body):
                self.body = body; self.headers = Message()
                self.headers["Content-Length"] = str(len(body))
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def geturl(self): return "https://fixture.example/snapshot"
            def read(self, limit): raise AssertionError("real transport must prefer read1")
            def read1(self, limit):
                clock[0] = 301
                return self.body
        class Opener:
            def __init__(self, body): self.body = body
            def open(self, *args, **kwargs): return Response(self.body)
        for late in (b"complete", b""):
            clock[0] = 0
            client = editor_sync.HTTPSPullClient("https://fixture.example", "not-used", opener=Opener(late))
            with self.subTest(late_body=bool(late)), mock.patch.object(editor_sync.time, "monotonic", side_effect=lambda: clock[0]):
                with self.assertRaisesRegex(RuntimeError, "HTTPS request failed"):
                    client.request("/snapshot", maximum=editor_sync.MAX_ARCHIVE)
