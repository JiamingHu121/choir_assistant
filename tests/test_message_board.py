"""Public anonymous messages stay separate from private score feedback."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection
import json
import os
import sqlite3
import threading
import unittest
from unittest import mock

from choir_assistant.deployment import site_server
import test_site_auth_feedback as fixtures


class MessageBoardHTTPTests(unittest.TestCase):
    setUp = fixtures.SiteAccessHTTPTests.setUp
    refresh_manifest = fixtures.SiteAccessHTTPTests.refresh_manifest
    register = fixtures.SiteAccessHTTPTests.register
    exported = fixtures.SiteAccessHTTPTests.exported
    serve = fixtures.SiteAccessHTTPTests.serve
    request = fixtures.SiteAccessHTTPTests.request
    failure = fixtures.SiteAccessHTTPTests.failure
    config = fixtures.SiteAccessHTTPTests.config
    login = fixtures.SiteAccessHTTPTests.login

    def body(self, message='想唱这首歌', category='song'):
        return {'category': category, 'message': message, 'csrfToken': self.config()['csrfToken']}

    def post(self, body):
        with self.request('/api/message-board', body, headers={'Accept-Encoding': 'gzip'}) as response:
            self.assertEqual(response.status, 201)
            self.assertIn('no-store', response.headers['Cache-Control'])
            self.assertNotIn('Content-Encoding', response.headers)
            return json.load(response)['item']

    def feed(self):
        with self.request('/api/message-board', headers={'Accept-Encoding': 'gzip'}) as response:
            self.assertIn('no-store', response.headers['Cache-Control'])
            self.assertNotIn('Content-Encoding', response.headers)
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
            return json.load(response)

    def test_public_posts_feed_restart_and_minimal_private_storage(self):
        release = self.serve()
        self.assertTrue(self.config()['messageBoardEnabled'])
        self.assertEqual(self.feed(), {'enabled': True, 'items': []})
        text = '<script>alert(1)</script> & 想唱的歌\n第二行'
        items = [self.post(self.body(text, category)) for category in ('suggestion', 'song', 'other')]
        for item in items:
            self.assertEqual(set(item), {'id', 'category', 'message', 'created'})
            self.assertRegex(item['id'], r'^[0-9a-f]{32}$')
            self.assertRegex(item['created'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
            self.assertEqual(item['message'], text)
        self.assertEqual(self.feed()['items'], list(reversed(items)))
        self.assertFalse(any(key.startswith('__Host-choir-work-') for key in self.cookies))
        self.failure(401, '/w/study/main/assets/interactive.json')
        self.failure(401, '/api/download?work=study&type=pdf')
        self.failure(401, '/w/study/main/api/message-board')
        path = self.site.access.message_board_path
        self.assertEqual(path.parent, self.site.access.feedback_path.parent)
        self.assertNotEqual(path, self.site.access.feedback_path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(path)) as db:
            columns = [row[1] for row in db.execute('PRAGMA table_info(messages)')]
            self.assertEqual(columns, ['id', 'category', 'message', 'created', 'bytes'])
            stored = json.dumps(db.execute('SELECT * FROM messages').fetchall())
            self.assertNotIn(self.secret, stored)
            self.assertNotIn('csrfToken', stored)
            self.assertNotIn('127.0.0.1', stored)
        restarted = site_server.SiteAccess(self.config_path, self.site.runtime, release)
        self.assertEqual(restarted.message_board('new-client')['items'], list(reversed(items)))

    def test_private_feedback_and_database_files_are_never_in_public_feed(self):
        self.serve()
        private = {'csrfToken': self.config()['csrfToken'], 'text': 'PRIVATE location and score problem',
                   'work': 'study', 'movement': 'main', 'measure': '48'}
        with self.request('/api/feedback', private) as response:
            receipt = json.load(response)['receipt']
        public = self.post(self.body('Public suggestion', 'suggestion'))
        with self.request('/api/message-board?table=feedback') as response:
            value = json.load(response)
        self.assertEqual(value['items'], [public])
        self.assertNotIn('PRIVATE', json.dumps(value))
        with closing(sqlite3.connect(self.site.access.feedback_path)) as db:
            self.assertIn('PRIVATE', db.execute('SELECT payload FROM feedback WHERE id=?', (receipt,)).fetchone()[0])
        for path in ('/feedback.sqlite3', '/message-board.sqlite3', '/runtime/message-board.sqlite3',
                     '/api/feedback', '/site-config.json'):
            self.failure(404, path)

    def test_invalid_payloads_are_rejected_without_creating_messages(self):
        self.serve()
        valid = self.body()
        invalid = [{'message': '', 'category': 'song'}, {'message': ' \n\t ', 'category': 'song'},
                   {'message': 'x' * 1201, 'category': 'song'}, {'message': None, 'category': 'song'},
                   {'message': ['text'], 'category': 'song'}, {'message': 'text', 'category': 'private'},
                   {'message': 'text', 'category': True}, {'message': 'NUL\0text', 'category': 'other'},
                   {'message': 'bad\x7ftext', 'category': 'other'}, {'message': '\ud800', 'category': 'other'},
                   {'message': 'text', 'category': 'song', 'contact': 'private@example.org'}]
        for body in invalid:
            with self.subTest(body=repr(body)):
                self.site.access.rates.clear()
                self.failure(400, '/api/message-board', {**valid, **body})
        self.assertEqual(self.feed()['items'], [])
        self.site.access.rates.clear()
        accepted = self.post({**valid, 'message': '  ' + '音' * 1200 + '  '})
        self.assertEqual(accepted['message'], '音' * 1200)

    def test_anonymous_post_still_requires_same_origin_and_browser_bound_csrf(self):
        self.serve()
        valid = self.body()
        self.failure(403, '/api/message-board', valid, cookies={})
        self.failure(403, '/api/message-board', {**valid, 'csrfToken': 'invalid'})
        self.failure(403, '/api/message-board', valid, headers={'Origin': 'https://attacker.invalid'})
        self.failure(403, '/api/message-board', valid, headers={'Origin': ''})
        another = {}
        with self.request('/api/site-config', cookies=another): pass
        self.failure(403, '/api/message-board', valid, cookies=another)
        self.assertEqual(self.feed()['items'], [])
        self.post(valid)

    def test_http_type_body_and_framing_guards_apply_to_public_post(self):
        self.serve()
        valid = self.body()
        self.failure(415, '/api/message-board', valid, headers={'Content-Type': 'text/plain'})
        self.failure(400, '/api/message-board', {**valid, 'message': 'x' * 9000})
        for headers in ((('Content-Length', '2'), ('Content-Length', '2')),
                        (('Transfer-Encoding', 'chunked'), ('Content-Length', '2'))):
            with self.subTest(headers=headers):
                connection = HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
                try:
                    connection.putrequest('POST', '/api/message-board')
                    connection.putheader('Origin', self.url)
                    connection.putheader('Content-Type', 'application/json')
                    for key, value in headers: connection.putheader(key, value)
                    connection.endheaders(b'{}')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 400)
                    response.read()
                finally: connection.close()
        self.assertEqual(self.feed()['items'], [])

    def test_post_and_read_limits_are_separate_and_return_retryable_errors(self):
        self.serve()
        body = self.body()
        for _ in range(3): self.post(body)
        self.failure(429, '/api/message-board', body)
        self.assertEqual(len(self.feed()['items']), 3)
        access = self.site.access
        access.rates.clear()
        for index in range(30): access.post_message({'category': 'other', 'message': str(index)}, 'client-' + str(index))
        with self.assertRaises(site_server.ResourceBusy): access.post_message({'category': 'other', 'message': 'global limit'}, 'new-client')
        access.rates.clear()
        for _ in range(60): access.message_board('read-client')
        with self.assertRaises(site_server.ResourceBusy): access.message_board('read-client')

    def test_capacity_is_atomic_bounded_and_latest_fifty_are_returned(self):
        self.serve()
        access = self.site.access
        barrier = threading.Barrier(4)
        def submit(index):
            barrier.wait()
            try:
                access.post_message({'category': 'song', 'message': str(index)}, 'capacity-' + str(index))
                return 'accepted'
            except site_server.ResourceBusy:
                return 'full'
        with mock.patch.object(site_server, 'MESSAGE_BOARD_MAX_ENTRIES', 1), ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(submit, range(4)))
        self.assertEqual(results.count('accepted'), 1)
        self.assertEqual(results.count('full'), 3)
        access.rates.clear()
        with mock.patch.object(site_server, 'MESSAGE_BOARD_MAX_BYTES', 1), self.assertRaises(site_server.ResourceBusy):
            access.post_message({'category': 'other', 'message': 'byte quota'}, 'bytes-client')
        with closing(sqlite3.connect(access.message_board_path)) as db:
            db.execute('DELETE FROM messages')
            db.executemany('INSERT INTO messages VALUES (?, ?, ?, ?, ?)',
                           [('%032x' % index, 'other', str(index), 1000, 30) for index in range(55)])
            db.commit()
        rows = access.message_board('latest')['items']
        self.assertEqual(len(rows), 50)
        self.assertEqual([item['message'] for item in rows], [str(index) for index in range(54, 4, -1)])

    def test_disabled_or_linked_storage_cannot_expose_another_private_file(self):
        release = self.serve()
        disabled = self.root / 'disabled'; disabled.mkdir()
        access = site_server.SiteAccess(None, disabled, release)
        self.assertFalse(access.message_board_enabled)
        self.assertEqual(access.message_board('anonymous'), {'enabled': False, 'items': []})
        self.assertFalse(access.message_board_path.exists())
        with self.assertRaisesRegex(ValueError, '尚未启用'):
            access.post_message({'category': 'other', 'message': 'text'}, 'anonymous')
        for kind in ('symlink', 'hardlink', 'world-readable'):
            with self.subTest(kind=kind):
                runtime = self.root / kind; runtime.mkdir()
                target = runtime / 'private-other-data'; target.write_bytes(b'PRIVATE SENTINEL'); target.chmod(0o600)
                board = runtime / 'message-board.sqlite3'
                if kind == 'symlink': board.symlink_to(target)
                elif kind == 'hardlink': os.link(target, board)
                else: board.write_bytes(b''); board.chmod(0o644)
                with self.assertRaises((ValueError, OSError)):
                    site_server.SiteAccess(self.config_path, runtime, release)
                self.assertEqual(target.read_bytes(), b'PRIVATE SENTINEL')


if __name__ == '__main__': unittest.main()
