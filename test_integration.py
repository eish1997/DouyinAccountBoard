"""Business journeys use temporary databases exclusively, never the shared database."""
import json
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import app
import collection
import test_auth_support
import test_workspace
import unittest


class IntegrationTest(unittest.TestCase):
    setUp=test_workspace.WorkspaceTest.setUp
    tearDown=test_workspace.WorkspaceTest.tearDown
    call=test_workspace.WorkspaceTest.call
    reject=test_workspace.WorkspaceTest.reject
    create=test_workspace.WorkspaceTest.create
    board=test_workspace.WorkspaceTest.board
    csv=test_workspace.WorkspaceTest.csv

    def account(self,ident,**changes):
        old=next(a for a in self.board()['accounts'] if a['id']==ident)
        self.call('/api/account',{**old,'slot':old['slot'] or 1,**changes},self.owner)

    def prepare_family(self):
        for ident in ['29','329','office3:29']:
            self.call('/api/phone-device/create',dict(id=ident,state='auto'),self.owner)
        self.account('43',account_type='大号',auxiliary_state='未注册',registered_on=(date.today()-timedelta(days=30)).isoformat(),registration_phone='13800000000',phone_device_id='29',status='正常')
        self.assertEqual(next(a for a in self.board()['accounts'] if a['id']=='43')['lifecycle'],'满30天未注册')
        # Change the parent flag first, as the real editor requires for an existing family.
        self.call('/api/lifecycle-status',dict(id='43',auxiliary_state='已注册'),self.owner)
        self.account('50',account_type='小号',registration_phone='13800000000',phone_device_id='329',status='正常')
        card=self.call('/api/phone-card',dict(phone='13800000000',owner='测试卡主',location='29',ownership='借用',code_state='待接码',return_state='待归还'),self.owner)['id']
        with app.connect() as db:
            db.execute("INSERT INTO publishing_layout_state VALUES(1,'journey')")
            for slot,account,label in [(1,'43','大号原安排'),(2,'50','小号原安排'),(3,None,''),(4,None,'待匹配')]:
                db.execute('INSERT INTO publishing_layout VALUES(?,?,?,?,?,?,?,?,?)',('journey','3',slot,account,label,'one','K'+str(slot),'fixture','需核对' if slot==4 else ''))
        return card

    def test_family_card_phone_daily_and_layout_journey(self):
        card_id=self.prepare_family();board=self.board();today=board['date']
        accounts={a['id']:a for a in board['accounts']}
        self.assertEqual(accounts['50']['parent_account_id'],'43')
        self.assertEqual(accounts['43']['child_accounts'][0]['id'],'50')
        self.assertEqual(accounts['43']['lifecycle'],'已注册辅助账号')
        self.call('/api/daily',dict(id='43',date=today,published=True),self.owner)
        self.call('/api/account/status',dict(id='43',status='封禁'),self.owner)
        board=self.board();devices={d['id']:d for d in board['phone_devices']['devices']}
        self.assertEqual(devices['29']['state'],'待清理');self.assertEqual(devices['329']['state'],'在用')
        self.assertEqual(next(a for a in board['accounts'] if a['id']=='50')['status'],'正常')
        self.assertEqual(board['phone_cards']['cards'][0]['status'],'待确认')
        self.reject(400,'/api/phone-device',dict(id='29',state='可用闲置'),self.owner)
        self.call('/api/account/phone-device',dict(id='43',phone_device_id=None),self.owner)
        self.call('/api/phone-device',dict(id='29',state='可用闲置'),self.owner)
        for ident in ['43','50']:
            self.call('/api/account/card-readiness',dict(id=ident,card_stability='已稳定',password_state='已修改'),self.owner)
        card=next(c for c in self.board()['phone_cards']['cards'] if c['id']==card_id)
        self.call('/api/phone-card',{**card,'status':'待注销','code_state':'已完成'},self.owner)
        self.call('/api/publishing/assignment',dict(batch='journey',computer_id='3',slot=2,action='move',target_computer='3',target_slot=3),self.owner)
        board=self.board();accounts={a['id']:a for a in board['accounts']}
        self.assertTrue(accounts['43']['published']);self.assertFalse(accounts['50']['published'])
        self.assertEqual(accounts['50']['phone_device_id'],'329');self.assertEqual(accounts['50']['registration_phone'],'13800000000')
        self.assertEqual(accounts['50']['computer_id'],'2')
        self.assertEqual(next(c for c in board['publishing_layout']['cells'] if c['slot']==3)['account_id'],'50')
        card=board['phone_cards']['cards'][0]
        self.call('/api/phone-card',{**card,'status':'已注销','return_state':'已归还','returned_on':today},self.owner)
        board=self.board();self.assertEqual(next(a for a in board['accounts'] if a['id']=='50')['status'],'正常')
        with app.connect() as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertFalse(list(db.execute('PRAGMA foreign_key_check')))
        for entity in ['account:43','account:50','card:'+str(card_id),'device:29']:
            self.assertTrue(self.call('/api/workspace/history?entity='+entity,client=self.owner)['entries'])

    def test_independent_days_and_public_count_identity_change(self):
        self.account('43',douyin_id='fixture_handle')
        first='2026-10-17';second='2026-10-18'
        self.call('/api/daily',dict(id='43',date=first,published=True),self.owner)
        self.call('/api/daily',dict(id='50',date=second,published=True),self.owner)
        with app.connect() as db:
            collection.enqueue(db,['43']);job=collection.next_job(db,app.today())
        self.call('/api/collection/result',dict(account_id='43',token=job['token'],count=0,source_url='https://www.douyin.com/user/MS4wLjABAAAA123',matched_douyin_id='fixture_handle'),headers={'X-Worker-Token':app.WORKER_TOKEN})
        for day,expected in [(first,(True,False)),(second,(False,True))]:
            board=self.call('/api/board?date='+day,client=self.owner);a={a['id']:a for a in board['accounts']}
            self.assertEqual((a['43']['published'],a['50']['published']),expected);self.assertEqual(a['43']['work_count'],0)
        self.account('43',douyin_id='changed_fixture_handle')
        a=next(a for a in self.board()['accounts'] if a['id']=='43')
        self.assertIsNone(a['work_count']);self.assertEqual(a['homepage'],'')
        board=self.call('/api/board?date='+first,client=self.owner)
        self.assertTrue(next(a for a in board['accounts'] if a['id']=='43')['published'])

    def test_simultaneous_same_row_conflicts_and_independent_rows_succeed(self):
        before=self.board()['record_versions'];other=test_auth_support.client(self.base)
        def update(client,ident,status):
            try:self.call('/api/account/status',dict(id=ident,status=status,_expected_versions={'account:'+ident:before['account:'+ident]}),client);return 200
            except urllib.error.HTTPError as error:
                with error:return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda args:update(*args),[(self.owner,'43','封禁'),(other,'43','停用')]))
        self.assertEqual(sorted(results),[200,409])
        before=self.board()['record_versions']
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda args:update(*args),[(self.owner,'43','正常'),(other,'50','正常')]))
        self.assertEqual(results,[200,200])

    def test_phone_namespace_family_validation_and_match_cas(self):
        self.prepare_family()
        self.reject(400,'/api/account',dict(**{**next(a for a in self.board()['accounts'] if a['id']=='43'),'registration_phone':'13900000000'}),self.owner)
        self.account('58',phone_device_id='office3:29')
        body=dict(batch='journey',computer_id='3',slot=4,account_id='58',expected_account_id=None)
        self.reject(400,'/api/publishing/link',body,self.owner)
        self.account('58',phone_device_id='29')
        self.call('/api/publishing/link',body,self.owner)
        self.reject(409,'/api/publishing/link',{**body,'account_id':'50'},self.owner)
        self.call('/api/publishing/link',{**body,'action':'clear','expected_account_id':'58'},self.owner)
        board=self.board();cell=next(c for c in board['publishing_layout']['cells'] if c['slot']==4)
        self.assertIsNone(cell['account_id']);self.assertEqual(cell['reason'],'需核对')
        self.assertEqual(next(a for a in board['accounts'] if a['id']=='58')['phone_device_id'],'29')

    def test_import_atomicity_and_staff_card_tasks_preserve_links(self):
        card_id=self.prepare_family();self.create(self.owner,'journey_staff');staff=test_auth_support.client(self.base,'journey_staff')
        self.call('/api/phone-card',dict(id=card_id,status='待注销',code_state='已完成'),staff)
        card=next(c for c in self.board()['phone_cards']['cards'] if c['id']==card_id)
        self.assertEqual((card['phone'],card['owner'],card['location']),('13800000000','测试卡主','29'))
        self.reject(403,'/api/publishing/link',{},staff)
        self.reject(403,'/api/account/phone-device',dict(id='43',phone_device_id='329'),staff)
        rows=[['内部编号','昵称'],['43','不能部分写入'],['50','陈旧预览']]
        preview=self.call('/api/workspace/import-preview',dict(kind='accounts',filename='journey.csv',content=self.csv(rows)),self.owner)
        self.call('/api/account/status',dict(id='50',status='养号7天'),staff)
        self.reject(409,'/api/workspace/import-apply',dict(preview_id=preview['preview_id'],rows=[2,3]),self.owner)
        board=self.board();self.assertNotEqual(next(a for a in board['accounts'] if a['id']=='43')['name'],'不能部分写入')
        self.assertEqual(next(a for a in board['accounts'] if a['id']=='50')['parent_account_id'],'43')
        self.assertEqual(len(board['publishing_layout']['cells']),4)
