import unittest
import app,operations,phone_cards,phone_devices,test_workspace,test_auth_support

class BatchEditTest(unittest.TestCase):
 setUp=test_workspace.WorkspaceTest.setUp
 tearDown=test_workspace.WorkspaceTest.tearDown
 call=test_workspace.WorkspaceTest.call
 reject=test_workspace.WorkspaceTest.reject
 board=test_workspace.WorkspaceTest.board
 create=test_workspace.WorkspaceTest.create
 def batch(self,kind,ids,patch,client=None):return self.call('/api/batch-edit',dict(kind=kind,ids=ids,patch=patch),client or self.owner)
 def account(self,id):return next(a for a in self.board()['accounts'] if a['id']==id)
 def test_account_status_and_readiness_atomic_and_history(self):
  before=self.account('43');other=self.account('58')
  result=self.batch('accounts',['43','50'],dict(status='封禁'))
  self.assertEqual(result['count'],2);self.assertEqual(self.account('43')['status'],'封禁');self.assertEqual(self.account('50')['status'],'封禁')
  self.assertEqual(self.account('43')['name'],before['name']);self.assertEqual(self.account('58'),other)
  self.reject(400,'/api/batch-edit',dict(kind='accounts',ids=['43','50'],patch=dict(status='正常',card_stability='bad')),self.owner)
  self.assertEqual(self.account('43')['status'],'封禁')
  with app.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM operations_history WHERE action='/api/batch-edit'").fetchone()[0],2)
 def test_permission_separation_and_unknown_fields(self):
  self.create(self.owner,'status_only',permissions=['account_status']);staff=test_auth_support.client(self.base,'status_only')
  self.batch('accounts',['43'],dict(status='封禁'),staff)
  self.reject(403,'/api/batch-edit',dict(kind='accounts',ids=['43'],patch=dict(password_state='已修改')),staff)
  self.reject(400,'/api/batch-edit',dict(kind='accounts',ids=['43'],patch=dict(name='new')),self.owner)
  self.reject(400,'/api/batch-edit',dict(kind='accounts',ids=['43','43'],patch=dict(status='正常')),self.owner)
 def test_stale_token_rejects_entire_group(self):
  versions=self.board()['record_versions'];body=dict(kind='accounts',ids=['43','50'],patch=dict(status='封禁'),_expected_versions={k:versions[k] for k in ['account:43','account:50']})
  old=self.account('50')['status'];self.call('/api/account/status',dict(id='43',status='发不了'),self.owner)
  self.reject(409,'/api/batch-edit',body,self.owner);self.assertEqual(self.account('50')['status'],old)
 def test_card_common_fields_preserve_numbers_tasks_and_contacts_permission(self):
  with app.connect() as db:
   ids=[str(phone_cards.save(db,dict(phone=p,owner='旧卡主',code_state='待接码',note='保留备注'))) for p in ['13800000001','13800000002']]
  self.batch('cards',ids,dict(owner='借卡人',ownership='借用',keeper='张三',code_contact='李四'))
  with app.connect() as db:
   rows=list(db.execute('SELECT * FROM phone_cards WHERE id IN (?,?)',ids))
   self.assertTrue(all(r['owner']=='借卡人' and r['code_state']=='待接码' and r['note']=='保留备注' for r in rows))
   self.assertEqual({r['phone'] for r in rows},{'13800000001','13800000002'})
  self.create(self.owner,'tasks_only',permissions=['card_tasks']);staff=test_auth_support.client(self.base,'tasks_only')
  self.reject(403,'/api/batch-edit',dict(kind='cards',ids=ids,patch=dict(code_contact='越权')),staff)
  self.reject(400,'/api/batch-edit',dict(kind='cards',ids=ids,patch=dict(owner='不应保存',ownership='bad')),self.owner)
  with app.connect() as db:self.assertTrue(all(r[0]=='借卡人' for r in db.execute('SELECT owner FROM phone_cards WHERE id IN (?,?)',ids)))
 def test_device_validation_rolls_back_and_auto_source_preserved(self):
  with app.connect() as db:
   for id in ['9001','9002']:phone_devices.save(db,dict(id=id,state='auto'),create=True)
   db.execute("UPDATE accounts SET phone_device_id='9002' WHERE id='43'")
  self.batch('devices',['9001','9002'],dict(owner='同负责人'))
  with app.connect() as db:self.assertTrue(all(r[0]=='auto' for r in db.execute("SELECT state_source FROM phone_devices WHERE id IN ('9001','9002')")))
  self.reject(400,'/api/batch-edit',dict(kind='devices',ids=['9001','9002'],patch=dict(state='可用闲置')),self.owner)
  with app.connect() as db:self.assertEqual(db.execute("SELECT state_source FROM phone_devices WHERE id='9001'").fetchone()[0],'auto')
 def test_verification_and_daily_share_existing_records(self):
  self.batch('verification',['43','50'],dict(verification_state='核对处理中',verification_owner='共同负责人'))
  records=self.board()['nurture']['records'];self.assertTrue(all(next(r for r in records if r['account_id']==id)['verification_owner']=='共同负责人' for id in ['43','50']))
  day=self.board()['date'];self.batch('daily',['43','50'],dict(date=day,published=True))
  self.assertTrue(self.account('43')['published']);self.assertTrue(self.account('50')['published'])
  self.reject(400,'/api/batch-edit',dict(kind='daily',ids=['43','50'],patch=dict(date='bad',published=False)),self.owner)
  self.assertTrue(self.account('43')['published'])

if __name__=='__main__':unittest.main()
