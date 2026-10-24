import http.cookiejar
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer
import app
import auth
import test_auth_support


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old_db=app.DB;self.old_worker=app.WORKER_TOKEN
        app.DB=Path(self.tmp.name)/'board.db';app.WORKER_TOKEN='test-local-worker'
        app.initialize();self.server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();app.DB=self.old_db;app.WORKER_TOKEN=self.old_worker;self.tmp.cleanup()

    def call(self,path,body=None,client=None,headers=None):
        opener=client or urllib.request.build_opener()
        request=urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=({'Content-Type':'application/json'} if body is not None else {})| (headers or {}))
        with opener.open(request) as response:return json.load(response)

    def reject(self,status,path,body=None,client=None,headers=None):
        with self.assertRaises(urllib.error.HTTPError) as error:self.call(path,body,client,headers)
        with error.exception:self.assertEqual(error.exception.code,status,json.load(error.exception))

    def owner(self):return test_auth_support.owner_client(app,self.base)

    def create(self,owner,username,role='employee',permissions=None):
        return self.call('/api/auth/users',dict(username=username,name=username,role=role,permissions=auth.DEFAULTS[role] if permissions is None else permissions,password=test_auth_support.TEST_PASSWORD,active=True),owner)['id']

    def test_first_owner_setup_requires_local_grant_and_is_unique(self):
        self.reject(401,'/api/board')
        self.reject(403,'/api/auth/desktop-setup',{})
        self.reject(403,'/api/auth/setup',dict(username='intruder',password=test_auth_support.TEST_PASSWORD))
        token=self.call('/api/auth/desktop-setup',{},headers={'X-Worker-Token':app.WORKER_TOKEN})['setup_token']
        self.assertTrue(self.call('/api/auth/me',headers={'Cookie':'board_setup='+token})['can_setup'])
        self.call('/api/auth/setup',dict(username='real_owner',name='本人',password=test_auth_support.TEST_PASSWORD),headers={'Cookie':'board_setup='+token})
        self.reject(403,'/api/auth/setup',dict(username='another',password=test_auth_support.TEST_PASSWORD),headers={'Cookie':'board_setup='+token})
        with app.connect() as db:
            row=db.execute('SELECT * FROM auth_users').fetchone()
            self.assertNotIn(test_auth_support.TEST_PASSWORD,row['password_hash'])
            self.assertEqual(row['role'],'owner');self.assertEqual(db.execute('SELECT count(*) FROM auth_users').fetchone()[0],1)

    def test_employee_and_admin_cannot_grant_or_escalate(self):
        owner=self.owner();employee_id=self.create(owner,'staff_one');self.create(owner,'admin_one','admin')
        for username in ['staff_one','admin_one']:
            client=test_auth_support.client(self.base,username)
            self.reject(403,'/api/auth/users',dict(username='intruder',role='owner',password=test_auth_support.TEST_PASSWORD),client)
            self.reject(403,'/api/auth/users',dict(id=employee_id,role='admin',permissions=list(auth.PERMISSIONS)),client)
            self.reject(403,'/api/auth/users',client=client)
            self.reject(403,'/api/auth/audit',client=client)
        staff=test_auth_support.client(self.base,'staff_one')
        self.call('/api/daily',dict(id='43',date='2026-10-17',published=True),staff)
        self.call('/api/account/status',dict(id='43',status='养号7天',name='不能改名'),staff)
        for path in ['/api/account','/api/account/create','/api/phone-device','/api/phone-device/create','/api/account/phone-device','/api/phone-cards/import','/api/collection/enqueue','/api/collection/settings']:
            self.reject(403,path,{},staff)
        with app.connect() as db:self.assertEqual(db.execute("SELECT name FROM accounts WHERE id='43'").fetchone()[0],'帮途道路救援')

    def test_permission_change_disable_and_password_revoke_sessions(self):
        owner=self.owner();uid=self.create(owner,'staff_two')
        staff=test_auth_support.client(self.base,'staff_two')
        self.call('/api/auth/users',dict(id=uid,name='staff_two',role='viewer',permissions=[],active=True),owner)
        self.reject(401,'/api/board',client=staff)
        staff=test_auth_support.client(self.base,'staff_two')
        self.reject(403,'/api/daily',dict(id='43',date='2026-10-17',published=True),staff)
        other=test_auth_support.client(self.base,'staff_two')
        self.call('/api/auth/password',dict(old_password=test_auth_support.TEST_PASSWORD,password='new-test-password-2026'),staff)
        self.reject(401,'/api/board',client=other)
        self.assertTrue(self.call('/api/board',client=staff)['accounts'])
        self.call('/api/auth/users',dict(id=uid,name='staff_two',role='employee',permissions=auth.DEFAULTS['employee'],active=False),owner)
        self.reject(401,'/api/board',client=staff)

    def test_csrf_owner_protection_card_tasks_and_audit(self):
        owner=self.owner();uid=self.create(owner,'staff_card')
        actor=self.call('/api/auth/me',client=owner)['user']
        self.reject(403,'/api/auth/users',dict(id=actor['id'],role='employee',permissions=[],active=False),owner)
        self.reject(400,'/api/auth/users',dict(username='other_owner',role='owner',permissions=[]),owner)
        card=self.call('/api/phone-card',dict(phone='13800000000',owner='原卡主'),owner)['id']
        staff=test_auth_support.client(self.base,'staff_card')
        self.call('/api/phone-card',dict(id=card,status='待注销',code_state='待接码'),staff)
        self.reject(403,'/api/phone-card',dict(id=card,owner='冒改卡主'),staff)
        self.reject(403,'/api/phone-card',dict(phone='13900000000'),staff)
        self.reject(403,'/api/daily',dict(id='43',date='2026-10-17',published=True),staff,{'X-CSRF-Token':'invalid'})
        self.reject(403,'/api/daily',dict(id='43',date='2026-10-17',published=True),owner,{'Origin':'http://evil.example'})
        self.assertTrue(self.call('/api/auth/audit',client=owner)['entries'])
        self.call('/api/auth/logout',{},staff);self.reject(401,'/api/board',client=staff)

    def test_login_rate_limit_and_worker_isolation(self):
        self.owner()
        for i in range(5):self.reject(401,'/api/auth/login',dict(username='test_owner',password='incorrect-password'))
        self.reject(429,'/api/auth/login',dict(username='test_owner',password=test_auth_support.TEST_PASSWORD))
        self.reject(403,'/api/collection/next')
        self.assertIn('job',self.call('/api/collection/next',headers={'X-Worker-Token':app.WORKER_TOKEN}))
        self.reject(401,'/api/board',headers={'X-Worker-Token':app.WORKER_TOKEN})

    def test_basic_management_does_not_bypass_other_grants(self):
        owner=self.owner();self.create(owner,'basic_only','admin',['account_manage','card_manage'])
        basic=test_auth_support.client(self.base,'basic_only')
        account=next(a for a in self.call('/api/board',client=basic)['accounts'] if a['id']=='43')
        self.call('/api/account',{**account,'name':'允许修改基础昵称'},basic)
        for changes in [dict(status='停用'),dict(card_stability='已稳定'),dict(phone_device_id='office3:1')]:
            self.reject(403,'/api/account',{**account,**changes},basic)
        card=self.call('/api/phone-card',dict(phone='13800000000',owner='原卡主'),owner)['id']
        self.reject(403,'/api/phone-card',dict(id=card,phone='13800000000',status='待注销'),basic)

    def test_creation_and_edit_grants_are_independent(self):
        owner=self.owner()
        self.create(owner,'create_only','employee',['account_create','card_create','device_create'])
        self.create(owner,'edit_only','admin',['account_manage','card_manage','device_manage'])
        creator=test_auth_support.client(self.base,'create_only');editor=test_auth_support.client(self.base,'edit_only')
        body=dict(name='新测试账号',computer_id='unassigned',slot=1,status='待确认')
        self.call('/api/account/create',body,creator)
        self.reject(403,'/api/account/create',body,editor)
        self.reject(403,'/api/account',{**body,'id':'43'},creator)
        card=self.call('/api/phone-card',dict(phone='13800000000',owner='新增卡主'),creator)['id']
        self.reject(403,'/api/phone-card',dict(phone='13900000000'),editor)
        self.reject(403,'/api/phone-card',dict(id=card,owner='不能修改'),creator)
        self.call('/api/phone-card',dict(id=card,phone='13800000000',owner='可以修改'),editor)
        self.call('/api/phone-device/create',dict(id='9001',state='待确认'),creator)
        self.reject(403,'/api/phone-device/create',dict(id='9002',state='待确认'),editor)
        self.reject(403,'/api/phone-device',dict(id='9001',state='可用闲置'),creator)
        self.reject(403,'/api/phone-cards/import',{},editor)
        self.reject(403,'/api/auth/users',{},creator)
        info=self.call('/api/auth/me',client=owner)
        self.assertEqual(set(info['permissions']),{key for _,keys in info['permission_groups'] for key in keys})
        self.assertEqual(set(info['permissions']),set(info['permission_details']))

    def test_legacy_grants_migrate_only_once(self):
        owner=self.owner();uid=self.create(owner,'legacy_staff','employee',['account_manage','card_manage','device_manage'])
        with app.connect() as db:
            db.execute("DELETE FROM auth_migrations WHERE name='split-create-v1'")
            auth.migrate(db)
            grants=json.loads(db.execute('SELECT permissions FROM auth_users WHERE id=?',(uid,)).fetchone()[0])
            self.assertTrue(set(['account_create','card_create','card_import','device_create']).issubset(grants))
            db.execute("UPDATE auth_users SET permissions='[]' WHERE id=?",(uid,))
            auth.migrate(db)
            self.assertEqual(db.execute('SELECT permissions FROM auth_users WHERE id=?',(uid,)).fetchone()[0],'[]')

    def test_publishing_links_permissions_conflicts_and_reversible_data(self):
        owner=self.owner();self.create(owner,'layout_employee');self.create(owner,'layout_only','employee',['publishing_manage'])
        staff=test_auth_support.client(self.base,'layout_employee');linker=test_auth_support.client(self.base,'layout_only')
        with app.connect() as db:
            db.execute("INSERT INTO publishing_layout_state VALUES(1,'test-layout')")
            for slot,account,label in [(4,None,'29-待匹配甲'),(5,None,'30-待匹配乙'),(6,'58','原表匹配'),(7,None,'')]:
                db.execute('INSERT INTO publishing_layout VALUES(?,?,?,?,?,?,?,?,?)',('test-layout','3',slot,account,label,'one','K'+str(slot),'https://example.invalid','编号重复'))
            before=[tuple(row) for row in db.execute('SELECT * FROM accounts ORDER BY id')]
        body=dict(batch='test-layout',computer_id='3',slot=4,expected_account_id=None,account_id='43')
        self.reject(403,'/api/publishing/link',body,staff)
        self.reject(409,'/api/publishing/link',{**body,'batch':'stale'},owner)
        self.reject(400,'/api/publishing/link',{**body,'account_id':'missing'},owner)
        self.call('/api/publishing/link',body,linker)
        self.reject(409,'/api/publishing/link',{**body,'account_id':'50'},owner)
        self.reject(409,'/api/publishing/link',{**body,'slot':5},owner)
        self.reject(400,'/api/publishing/link',{**body,'slot':6,'expected_account_id':'58'},owner)
        self.reject(400,'/api/publishing/link',{**body,'slot':7},owner)
        self.reject(403,'/api/daily',dict(id='43',date='2026-10-18',published=True),linker)
        self.call('/api/daily',dict(id='43',date='2026-10-18',published=True),owner)
        self.call('/api/publishing/link',{**body,'expected_account_id':'43','account_id':'50'},owner)
        board=self.call('/api/board',client=owner)
        cell=next(cell for cell in board['publishing_layout']['cells'] if cell['slot']==4)
        self.assertEqual(cell['account_id'],'50');self.assertTrue(cell['manual_link'])
        self.call('/api/publishing/link',{**body,'expected_account_id':'50','action':'clear'},owner)
        with app.connect() as db:
            self.assertEqual(before,[tuple(row) for row in db.execute('SELECT * FROM accounts ORDER BY id')])
            cell=db.execute("SELECT * FROM publishing_layout WHERE slot=4").fetchone()
            self.assertIsNone(cell['account_id']);self.assertEqual(cell['reason'],'编号重复')
            self.assertEqual(db.execute("SELECT published FROM daily WHERE account_id='43' AND day='2026-10-18'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM publishing_layout_links').fetchone()[0],0)
            db.execute("UPDATE accounts SET office_group='three' WHERE id='50'")
        self.reject(400,'/api/publishing/link',{**body,'account_id':'50'},owner)


if __name__=='__main__':unittest.main()
