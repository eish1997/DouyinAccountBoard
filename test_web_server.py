import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
import app
import web_server
from test_auth import AuthTest
import test_auth_support


class WebServiceTest(unittest.TestCase):
    setUp = AuthTest.setUp
    tearDown = AuthTest.tearDown
    call = AuthTest.call
    reject = AuthTest.reject

    def test_two_browser_sessions_share_updates_and_keep_permissions(self):
        first = test_auth_support.owner_client(app, self.base)
        second = test_auth_support.client(self.base)
        self.call('/api/daily', dict(id='43', date='2026-10-17', published=True), first,
                  {'Origin': self.base})
        board = self.call('/api/board?date=2026-10-17', client=second)
        self.assertEqual(next(a for a in board['accounts'] if a['id']=='43')['published'], 1)
        self.call('/api/account/status', dict(id='43', status='养号7天'), second,
                  {'Origin': self.base})
        board = self.call('/api/board', client=first)
        self.assertEqual(next(a for a in board['accounts'] if a['id']=='43')['status'], '养号7天')
        self.reject(401, '/api/board')
        self.reject(403, '/api/daily', dict(id='43', date='2026-10-17', published=False), first,
                    {'Origin': 'http://other.example'})
        with patch.dict(os.environ, {'DOUYIN_BOARD_SHARED_WEB':'1'}):
            self.assertEqual(self.call('/api/health'), dict(service='douyin-account-board', shared=True,
                                                           authentication_required=True))

    def test_configuration_never_creates_or_switches_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'data').mkdir()
            path=root/'service.json'
            config=dict(enabled=True, host='0.0.0.0', port=8776, worker_token='x'*48,
                        data_dir=str(root/'data'))
            path.write_text(json.dumps(config), encoding='utf-8')
            with patch.object(web_server, 'ROOT', root):
                with self.assertRaises(ValueError): web_server.configuration(path)
                (root/'data/board.db').touch()
                self.assertEqual(web_server.configuration(path)['port'], 8776)
                for key,value in [('data_dir',str(root)),('worker_token','short'),('port',80),('host','::')]:
                    path.write_text(json.dumps(config|{key:value}),encoding='utf-8')
                    with self.assertRaises(ValueError): web_server.configuration(path)
