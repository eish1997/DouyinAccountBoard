import test_auth_support
import csv
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
import app
import phone_cards
import phone_devices
import import_phone_devices


class PhoneDeviceTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.original=app.DB
        app.DB=Path(self.tmp.name)/'data'/'board.db';app.initialize()

    def tearDown(self):
        app.DB=self.original;self.tmp.cleanup()

    def snapshot(self,rows,name='snapshot.json'):
        content=io.StringIO();csv.writer(content).writerows([['抖音名称','抖音在哪个手机上','抖音号'],*rows])
        target=Path(self.tmp.name)/name;target.write_text(json.dumps({'csv_data':content.getvalue()}),encoding='utf-8');return target

    def test_import_positions_identity_and_repeat_preserves_manual(self):
        with app.connect() as db:
            db.execute("UPDATE accounts SET douyin_id='main_id',status='封禁' WHERE id='43'")
            db.execute("UPDATE accounts SET douyin_id='small_id',status='正常' WHERE id='50'")
            db.execute("UPDATE accounts SET douyin_id='blocked_id',status='停用' WHERE id='58'")
            phone_cards.save(db,{'phone':'13800000000','location':'412'})
            before=[tuple(r) for r in db.execute('SELECT id,registration_phone,computer_id,status FROM accounts ORDER BY id')]
            source=self.snapshot([['帮途道路救援','408','main_id'],['锐途道路救援','408多开','small_id'],['稻壳道路救援','411','blocked_id']])
            report=import_phone_devices.apply(db,source)
            self.assertEqual((report['devices_added'],report['accounts_assigned']),(3,3))
            accounts=[dict(r) for r in db.execute('SELECT * FROM accounts')]
            states={d['id']:d['state'] for d in phone_devices.overview(db,accounts)['devices']}
            self.assertEqual(states,{'408':'在用','411':'待清理','412':'待确认'})
            self.assertEqual(import_phone_devices.apply(db,source)['accounts_assigned'],0)
            phone_devices.assign(db,'50','412')
            repeat=import_phone_devices.apply(db,source)
            self.assertEqual(repeat['manual_kept'],1)
            self.assertEqual(db.execute("SELECT phone_device_id FROM accounts WHERE id='50'").fetchone()[0],'412')
            self.assertEqual(before,[tuple(r) for r in db.execute('SELECT id,registration_phone,computer_id,status FROM accounts ORDER BY id')])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_ambiguous_labels_and_conflicting_rows_not_guessed(self):
        with app.connect() as db:
            db.execute("UPDATE accounts SET douyin_id='main_id' WHERE id='43'")
            db.execute("UPDATE accounts SET douyin_id='small_id' WHERE id='50'")
            db.execute("UPDATE accounts SET douyin_id='blocked_id' WHERE id='58'")
            source=self.snapshot([['帮途道路救援','苹果13','main_id'],['锐途道路救援','13多开抖音','small_id'],['稻壳道路救援','14','blocked_id'],['稻壳道路救援','15','blocked_id']])
            report=import_phone_devices.apply(db,source)
            self.assertEqual(report['accounts_assigned'],1)
            self.assertEqual(len(report['pending']),2)
            self.assertIsNone(db.execute("SELECT phone_device_id FROM accounts WHERE id='43'").fetchone()[0])
            self.assertIn('苹果13',db.execute("SELECT phone_device_note FROM accounts WHERE id='43'").fetchone()[0])
            self.assertIsNone(db.execute("SELECT phone_device_id FROM accounts WHERE id='58'").fetchone()[0])
            self.assertEqual(db.execute("SELECT phone_device_id FROM accounts WHERE id='50'").fetchone()[0],'13')
            self.assertIsNone(phone_devices.number('苹果13'))
            self.assertIsNone(phone_devices.number('0'))

    def test_api_idle_guard_move_fault_and_dynamic_status(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base='http://127.0.0.1:'+str(server.server_port)
        client=test_auth_support.owner_client(app,base)
        def get():
            with client.open(base+'/api/board') as response:return json.load(response)
        def post(route,body):
            req=urllib.request.Request(base+route,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
            with client.open(req) as response:return json.load(response)
        def state(device_id):return next(d['state'] for d in get()['phone_devices']['devices'] if d['id']==device_id)
        def reject(route,body,text):
            with self.assertRaises(urllib.error.HTTPError) as error:post(route,body)
            self.assertEqual(error.exception.code,400)
            with error.exception:self.assertIn(text,json.load(error.exception)['error'])
        try:
            post('/api/phone-device/create',{'id':'408','state':'auto'})
            post('/api/phone-device/create',{'id':'411','state':'可用闲置'})
            post('/api/phone-device/create',{'id':'412','state':'故障'})
            reject('/api/phone-device/create',{'id':'408'},'已经存在')
            post('/api/account/phone-device',{'id':'43','phone_device_id':'408'})
            self.assertEqual(state('408'),'在用')
            reject('/api/phone-device',{'id':'408','state':'可用闲置'},'先移出账号')
            reject('/api/account/phone-device',{'id':'43','phone_device_id':'412'},'故障')
            post('/api/daily',{'id':'43','date':'2026-10-15','published':True})
            original=next(a for a in get()['accounts'] if a['id']=='43')
            post('/api/account/phone-device',{'id':'43','phone_device_id':'411'})
            self.assertEqual(state('411'),'在用');self.assertEqual(state('408'),'待确认')
            moved=next(a for a in get()['accounts'] if a['id']=='43')
            for field in ['registration_phone','computer_id','slot','status','published']:self.assertEqual(moved[field],original[field])
            # Legacy account edits must preserve its current phone.
            legacy={**moved,'status':'封禁'};legacy.pop('phone_device_id')
            post('/api/account',legacy)
            self.assertEqual(state('411'),'待清理')
            self.assertEqual(next(a for a in get()['accounts'] if a['id']=='43')['phone_device_id'],'411')
            post('/api/account/phone-device',{'id':'43','phone_device_id':None})
            post('/api/phone-device',{'id':'411','state':'可用闲置'})
            self.assertEqual(state('411'),'可用闲置')
            self.assertEqual(len(get()['accounts']),27)
        finally:
            server.shutdown();server.server_close()


if __name__=='__main__':unittest.main()
