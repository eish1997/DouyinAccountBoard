"""Synthetic HTTP baseline: two independent sessions see persisted shared writes."""
import json
from pathlib import Path
import threading
import unittest
import urllib.request
from unittest.mock import patch
from uuid import uuid4
import app
import auth
import test_auth_support
from test_isolation import checked_root


class SharedTest(unittest.TestCase):
    def test_two_users_share_saved_data_after_relogin_and_reinitialize(self):
        folder = checked_root() / ('http-' + uuid4().hex)
        folder.mkdir()
        with patch.object(app, 'DB', folder / 'board.db'):
            app.initialize()
            class QuietHandler(app.Handler):
                def log_message(self, *args):
                    pass
            server = app.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                base = 'http://127.0.0.1:' + str(server.server_port)
                owner = test_auth_support.owner_client(app, base)
                with app.connect() as db:
                    actor = auth.public_user(db.execute("SELECT * FROM auth_users WHERE role='owner'").fetchone())
                    auth.save_user(db, actor, dict(username='synthetic_employee', name='合成员工',
                        password=test_auth_support.TEST_PASSWORD, role='employee', permissions=['publish']))
                employee = test_auth_support.client(base, 'synthetic_employee')
                def board(client):
                    with client.open(base + '/api/board?date=2026-10-21', timeout=5) as response:
                        return json.load(response)
                first, second = board(owner), board(employee)
                self.assertNotEqual(first['user']['id'], second['user']['id'])
                self.assertEqual(first['accounts'], second['accounts'])
                request = urllib.request.Request(base + '/api/daily',
                    data=json.dumps(dict(id='synth-account-1', date='2026-10-21', published=True)).encode(),
                    headers={'Content-Type': 'application/json'})
                with employee.open(request, timeout=5) as response:
                    self.assertTrue(json.load(response)['ok'])
                self.assertEqual(board(owner)['accounts'][0]['published'], 1)
                app.initialize()
                employee = test_auth_support.client(base, 'synthetic_employee')
                self.assertEqual(board(employee)['accounts'][0]['published'], 1)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
