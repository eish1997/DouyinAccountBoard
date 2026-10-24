import base64
import csv
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from http.server import ThreadingHTTPServer
import app
import auth
import backup
import operations
import test_auth_support
from test_auth import AuthTest


class WorkspaceTest(unittest.TestCase):
    call=AuthTest.call
    reject=AuthTest.reject
    create=AuthTest.create

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old_db=app.DB;self.old_worker=app.WORKER_TOKEN
        app.DB=Path(self.tmp.name)/'data/board.db';app.WORKER_TOKEN='test-worker';app.initialize()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler);threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base='http://127.0.0.1:'+str(self.server.server_port);self.owner=test_auth_support.owner_client(app,self.base)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();app.DB=self.old_db;app.WORKER_TOKEN=self.old_worker;self.tmp.cleanup()

    def board(self,client=None):return self.call('/api/board',client=client or self.owner)

    def csv(self,rows):
        value=io.StringIO();csv.writer(value).writerows(rows)
        return base64.b64encode(value.getvalue().encode('utf-8-sig')).decode()

    def test_conflicts_history_and_frozen_daily(self):
        board=self.board();stale=board['record_versions']['account:43'];other=test_auth_support.client(self.base)
        self.call('/api/account/status',dict(id='43',status='养号7天',_expected_versions={'account:43':stale}),self.owner)
        self.reject(409,'/api/account/status',dict(id='43',status='停用',_expected_versions={'account:43':stale}),other)
        self.reject(409,'/api/account/status',dict(id='43',status='停用',_expected_versions={}),other)
        history=self.call('/api/workspace/history?entity=account:43',client=self.owner)
        self.assertTrue(any(change['field']=='status' and change['after']=='养号7天' for row in history['entries'] for change in row['changes']))
        board=self.board();key='daily:43:'+board['date'];body=dict(id='43',date=board['date'],published=True,_expected_versions={'account:43':board['record_versions']['account:43'],key:None})
        self.call('/api/daily',body,self.owner);self.reject(409,'/api/daily',{**body,'published':False},other)

    def test_import_preview_preserves_and_explicit_apply(self):
        rows=[['内部编号','昵称','抖音号','注册手机号','实际发布位置'],['43','更正昵称','','','不要导入'],['','新账号','new_unique_handle','13800000000','不要导入']]
        preview=self.call('/api/workspace/import-preview',dict(kind='accounts',filename='accounts.csv',content=self.csv(rows)),self.owner)
        self.assertFalse(preview['rows'][0]['selected']);self.assertTrue(preview['rows'][1]['selected']);self.assertIn('实际发布位置',preview['ignored_columns'])
        self.assertNotEqual(next(a for a in self.board()['accounts'] if a['id']=='43')['name'],'更正昵称')
        result=self.call('/api/workspace/import-apply',dict(preview_id=preview['preview_id'],rows=[3]),self.owner);self.assertEqual(result['imported'],1)
        self.assertEqual(len(self.board()['accounts']),28)
        self.reject(400,'/api/workspace/import-apply',dict(preview_id=preview['preview_id'],rows=[2]),self.owner)

    def test_import_rejects_stale_rows_duplicates_and_permission(self):
        rows=[['内部编号','昵称'],['43','预览名字']]
        preview=self.call('/api/workspace/import-preview',dict(kind='accounts',filename='x.csv',content=self.csv(rows)),self.owner)
        self.call('/api/account/status',dict(id='43',status='停用'),self.owner)
        self.reject(409,'/api/workspace/import-apply',dict(preview_id=preview['preview_id'],rows=[2]),self.owner)
        duplicate=self.call('/api/workspace/import-preview',dict(kind='accounts',filename='x.csv',content=self.csv(rows+[rows[1]])),self.owner)
        self.assertTrue(all(row['action']=='error' for row in duplicate['rows']))
        self.create(self.owner,'ordinary_staff');staff=test_auth_support.client(self.base,'ordinary_staff')
        self.reject(403,'/api/workspace/import-preview',dict(kind='accounts',filename='x.csv',content=self.csv(rows)),staff)
        self.reject(403,'/api/workspace/history',client=staff)
        self.reject(403,'/api/workspace/backups',client=staff)

    def test_養号_dates_and_no_inferred_date(self):
        self.assertEqual(next(a for a in self.board()['accounts'] if a['id']=='43')['nurture_started'],'')
        self.reject(400,'/api/account/nurture',dict(id='43',started='2099-01-01'),self.owner)
        self.call('/api/account/nurture',dict(id='43',started='2026-10-01'),self.owner)
        self.assertEqual(next(a for a in self.board()['accounts'] if a['id']=='43')['nurture_started'],'2026-10-01')
        self.call('/api/account/status',dict(id='43',status='养号7天'),self.owner)
        self.assertEqual(next(a for a in self.board()['accounts'] if a['id']=='43')['nurture_started'],'')

    def test_move_and_remove_preserve_account_and_daily(self):
        with app.connect() as db:
            db.execute("INSERT INTO publishing_layout_state VALUES(1,'batch')")
            for slot,account,label in [(1,'43','原安排'),(2,None,''),(3,None,'待匹配')]:
                db.execute('INSERT INTO publishing_layout VALUES(?,?,?,?,?,?,?,?,?)',('batch','3',slot,account,label,'one',str(slot),'source',''))
            before=[tuple(row) for row in db.execute('SELECT * FROM accounts')]
        body=dict(batch='batch',computer_id='3',slot=1,action='move',target_computer='3',target_slot=2)
        self.call('/api/publishing/assignment',body,self.owner)
        cells=self.board()['publishing_layout']['cells'];self.assertIsNone(cells[0]['account_id']);self.assertEqual(cells[1]['account_id'],'43')
        self.reject(409,'/api/publishing/assignment',{**body,'slot':2,'target_slot':3},self.owner)
        self.call('/api/publishing/assignment',dict(batch='batch',computer_id='3',slot=2,action='remove'),self.owner)
        history=self.call('/api/workspace/history?entity=account:43',client=self.owner)
        self.assertTrue(any(row['entity'].startswith('layout:') for row in history['entries']))
        with app.connect() as db:self.assertEqual(before,[tuple(row) for row in db.execute('SELECT * FROM accounts')])

    def test_restore_preserves_current_users_and_requires_fresh_preview(self):
        folder=app.DB.parent.parent/'backups';folder.mkdir();target=folder/'manual-testbackup.db'
        # A realistic allowed name, not a disposable test/validation filename.
        target=folder/'manual-20261019.db';backup.snapshot(app.DB,target)
        self.call('/api/account/status',dict(id='43',status='停用'),self.owner)
        self.create(self.owner,'keep_employee')
        preview=self.call('/api/workspace/restore-preview',dict(backup_id=target.name),self.owner)
        self.reject(400,'/api/workspace/restore-apply',dict(preview_id=preview['preview_id'],confirmation='恢复业务数据',password='wrong'),self.owner)
        result=self.call('/api/workspace/restore-apply',dict(preview_id=preview['preview_id'],confirmation='恢复业务数据',password=test_auth_support.TEST_PASSWORD),self.owner)
        self.assertIn('保持当前',result['note'])
        with app.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM auth_users').fetchone()[0],2)
            self.assertNotEqual(db.execute("SELECT status FROM accounts WHERE id='43'").fetchone()[0],'停用')
            self.assertEqual(db.execute('SELECT auto_enabled FROM collection_settings').fetchone()[0],0)
            self.assertFalse(list(db.execute('PRAGMA foreign_key_check')))
        self.reject(400,'/api/workspace/restore-preview',dict(backup_id='../data/board.db'),self.owner)
        preview=self.call('/api/workspace/restore-preview',dict(backup_id=target.name),self.owner)
        self.call('/api/account/status',dict(id='43',status='停用'),self.owner)
        self.reject(409,'/api/workspace/restore-apply',dict(preview_id=preview['preview_id'],confirmation='恢复业务数据',password=test_auth_support.TEST_PASSWORD),self.owner)

    def test_export_csv_and_xlsx_roundtrip(self):
        for fmt in ['csv','xlsx']:
            result=self.call('/api/workspace/export',dict(kind='accounts',ids=['43'],format=fmt),self.owner)
            raw=base64.b64decode(result['content']);self.assertEqual(result['count'],1)
            if fmt=='xlsx':
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    self.assertIn('xl/worksheets/sheet1.xml',archive.namelist())
                import data_exchange
                rows=data_exchange.read_file('export.xlsx',result['content']);self.assertEqual(rows[1][0],'43')

    def test_invalid_workbook_and_device_import_preserve_auto_source(self):
        self.reject(400,'/api/workspace/import-preview',dict(kind='accounts',filename='broken.xlsx',content=base64.b64encode(b'not-a-zip').decode()),self.owner)
        self.call('/api/phone-device/create',dict(id='29',state='auto',name='旧名称'),self.owner)
        rows=[['手机序号','手机名称','安排状态'],['29','改后名称','待确认']]
        preview=self.call('/api/workspace/import-preview',dict(kind='devices',filename='devices.csv',content=self.csv(rows)),self.owner)
        self.call('/api/workspace/import-apply',dict(preview_id=preview['preview_id'],rows=[2]),self.owner)
        with app.connect() as db:
            row=db.execute("SELECT * FROM phone_devices WHERE id='29'").fetchone()
            self.assertEqual(row['name'],'改后名称');self.assertEqual(row['state_source'],'auto')
