"""Small synthetic behavior baseline, not office authorization or load acceptance."""
import base64
import json
import math
import struct
import threading
import time
import unittest
import urllib.request
import urllib.error
import zlib
from unittest.mock import patch
from uuid import uuid4
import app
import test_auth_support
from test_isolation import checked_root

METRICS = {}


def png():
    def chunk(kind, content):
        return struct.pack('!I', len(content)) + kind + content + struct.pack('!I', zlib.crc32(kind + content))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0')) + chunk(b'IEND', b'')


class CoreBaselineTest(unittest.TestCase):
    def test_synthetic_core_workflows_and_small_fixture_latency(self):
        folder = checked_root() / ('core-' + uuid4().hex)
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
                client = test_auth_support.owner_client(app, base)
                def call(path, body=None):
                    request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
                    with client.open(request, timeout=10) as response:
                        return json.load(response)
                def board():
                    return call('/api/board')
                call('/api/account/create', dict(name='合成第二账号', computer_id='synth-pc-1', slot=2,
                     status='正常', douyin_id='synthetic_002', note='=synthetic_formula_text'))
                ids = [row['id'] for row in board()['accounts']]
                self.assertEqual(len(ids), 2)
                call('/api/batch-edit', dict(kind='accounts', ids=ids, patch={'status': '封禁'}))
                before = board()['accounts']
                with self.assertRaises(urllib.error.HTTPError) as error:
                    call('/api/batch-edit', dict(kind='accounts', ids=ids, patch={'status': '正常', 'card_stability': 'invalid-synthetic'}))
                self.assertEqual(error.exception.code, 400)
                self.assertEqual(board()['accounts'], before)
                call('/api/batch-edit', dict(kind='accounts', ids=ids, patch={'status': '正常'}))
                call('/api/phone-card', dict(phone='19900000001', label='合成卡', owner='合成负责人'))
                call('/api/phone-device/create', dict(id='9001', state='auto', owner='合成负责人'))
                call('/api/account/phone-device', dict(id='synth-account-1', phone_device_id='9001'))
                state = board()
                self.assertEqual(state['phone_cards']['cards'][0]['phone'], '19900000001')
                self.assertEqual(next(row for row in state['accounts'] if row['id']=='synth-account-1')['phone_device_id'], '9001')
                export = call('/api/workspace/export', dict(kind='accounts', format='csv', ids=ids))
                text = base64.b64decode(export['content']).decode('utf-8-sig')
                self.assertIn('合成第二账号', text)
                self.assertIn("'=synthetic_formula_text", text)
                csv = '手机卡号码,卡名称,备注\n19900000002,合成导入卡,0000123\n'
                preview = call('/api/workspace/import-preview', dict(kind='cards', filename='synthetic.csv',
                               content=base64.b64encode(csv.encode('utf-8-sig')).decode()))
                self.assertEqual(preview['rows'][0]['action'], 'create')
                imported = call('/api/workspace/import-apply', dict(preview_id=preview['preview_id'], rows=[2]))
                self.assertEqual(imported['imported'], 1)
                cards = board()['phone_cards']['cards']
                self.assertEqual(next(card for card in cards if card['phone']=='19900000002')['note'], '0000123')
                day = app.today()
                for action, fields in [('join', {}), ('verification', {'verification_state': '无需核对，可使用'}), ('start', {'started': day})]:
                    call('/api/workspace/nurture/' + action, dict(id='synth-account-1', **fields))
                image = png()
                call('/api/workspace/nurture/upload', dict(id='synth-account-1', day=day, content=base64.b64encode(image).decode()))
                record = next(row for row in board()['nurture']['records'] if row['account_id']=='synth-account-1')
                with client.open(base + '/api/nurture/image?id=' + record['images'][0]['id'], timeout=10) as response:
                    self.assertEqual(response.read(), image)
                history = call('/api/workspace/history?entity=account:synth-account-1')
                self.assertTrue(history['entries'])
                durations = []
                for _ in range(20):
                    started = time.perf_counter()
                    self.assertEqual(len(board()['accounts']), 2)
                    durations.append((time.perf_counter() - started) * 1000)
                ordered = sorted(durations)
                METRICS.update(kind='single-client-small-fixture-smoke-only', accounts=2, requests=20,
                               read_p50_ms=ordered[9], read_p95_ms=ordered[18], read_max_ms=ordered[-1],
                               target_load_acceptance=False)
                with app.connect() as db:
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
