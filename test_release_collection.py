import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from uuid import uuid4
import app
import collection
import f2_worker
from test_isolation import checked_root
import test_auth_support


class CollectionDisabledTest(unittest.TestCase):
    def setUp(self):
        folder = checked_root() / ('collection-' + uuid4().hex)
        folder.mkdir()
        self.db_patch = patch.object(app, 'DB', folder / 'board.db')
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        app.initialize()
        with app.connect() as db:
            db.execute("UPDATE accounts SET work_count=123,last_collected='2026-10-20',collection_state='采集中',collection_error='synthetic retained error'")
            db.execute("INSERT INTO collection_jobs(account_id,homepage,douyin_id,token,state,started_at) VALUES('synth-account-1','','synthetic_001','synthetic-old-lease','running','2026-10-20')")
            for number, state in ((2, 'queued'), (3, 'done')):
                ident = 'synthetic-' + state
                db.execute("INSERT INTO accounts(id,computer_id,slot,name,status,note,douyin_id,homepage) VALUES(?,'synth-pc-1',?,?,'正常','','','')", (ident, number, ident))
                db.execute("INSERT INTO collection_jobs(account_id,homepage,douyin_id,token,state) VALUES(?,'','','synthetic-retained-token',?)", (ident, state))

    def snapshot(self):
        with app.connect() as db:
            return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
                    for table in ('accounts', 'collection_jobs', 'collection_settings', 'collection_worker')}

    def test_service_paths_reject_without_mutation(self):
        before = self.snapshot()
        with app.connect() as db:
            for function, argument in ((collection.enqueue, ['synth-account-1']),
                                       (collection.next_job, '2026-10-21'),
                                       (collection.complete, {}), (collection.heartbeat, '待命')):
                with self.subTest(function=function.__name__), self.assertRaisesRegex(ValueError, '采集暂未启用'):
                    function(db, argument)
        self.assertEqual(self.snapshot(), before)
        app.initialize()
        self.assertEqual(self.snapshot(), before)

    def test_worker_stops_before_config_profile_or_lock_access(self):
        for entry in (f2_worker.main, f2_worker.Worker):
            with self.subTest(entry=entry.__name__), self.assertRaisesRegex(ValueError, '采集暂未启用'):
                entry()

    def test_identity_edit_preserves_historical_task_without_reusing_old_count(self):
        import account_service
        import auth
        with app.connect() as db:
            task = tuple(db.execute("SELECT * FROM collection_jobs WHERE account_id='synth-account-1'").fetchone())
            account = dict(db.execute("SELECT * FROM accounts WHERE id='synth-account-1'").fetchone())
            account_service.save(db, {**account, 'douyin_id': 'synthetic_changed_identity'},
                                 {'permissions': list(auth.PERMISSIONS)}, '2026-10-21')
            self.assertEqual(tuple(db.execute("SELECT * FROM collection_jobs WHERE account_id='synth-account-1'").fetchone()), task)
            saved = db.execute("SELECT douyin_id,work_count FROM accounts WHERE id='synth-account-1'").fetchone()
            self.assertEqual(saved[0], 'synthetic_changed_identity')
            self.assertIsNone(saved[1])

    def test_old_valid_worker_and_control_requests_cannot_change_records(self):
        class QuietHandler(app.Handler):
            def log_message(self, *args):
                pass
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            owner = test_auth_support.owner_client(app, base)
            before = self.snapshot()
            with patch.object(app, 'WORKER_TOKEN', 'synthetic-previously-valid-token'):
                routes = [('next', None), ('heartbeat', {'status': '待命'}),
                          ('result', {'account_id': 'synth-account-1', 'token': 'synthetic-old-lease',
                                      'count': 999, 'matched_douyin_id': 'synthetic_001',
                                      'source_url': 'https://www.douyin.com/user/syntheticonly'}),
                          ('enqueue', {'ids': ['synth-account-1']}),
                          ('settings', {'resume': True, 'auto_enabled': True, 'cancel_waiting': True}),
                          ('unknown-legacy-control', {})]
                for route, body in routes:
                    request = urllib.request.Request(base + '/api/collection/' + route,
                        data=None if body is None else json.dumps(body).encode(),
                        headers={'Content-Type': 'application/json', 'X-Worker-Token': 'synthetic-previously-valid-token',
                                 'X-Collection-Engine': 'f2'})
                    with self.subTest(route=route), self.assertRaises(urllib.error.HTTPError) as error:
                        owner.open(request, timeout=5)
                    self.assertEqual(error.exception.code, 503)
                    self.assertEqual(json.load(error.exception)['code'], 'COLLECTION_DISABLED')
                    self.assertEqual(self.snapshot(), before)
                with owner.open(base + '/api/collection/status', timeout=5) as response:
                    status = json.load(response)
                self.assertFalse(status['can_manage'])
                self.assertFalse(status['collection']['enabled'])
                self.assertEqual(status['collection']['waiting'], 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
