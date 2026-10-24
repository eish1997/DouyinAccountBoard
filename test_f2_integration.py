import json
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import app
import collection
from f2_worker import result_body
from test_workspace import WorkspaceTest

class F2IntegrationTest(unittest.TestCase):
    setUp=WorkspaceTest.setUp
    tearDown=WorkspaceTest.tearDown
    call=WorkspaceTest.call
    board=WorkspaceTest.board

    def worker(self,path,body=None,engine='f2',token='test-worker'):
        headers={'Content-Type':'application/json','X-Worker-Token':token,'X-Collection-Engine':engine}
        with urlopen(Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)) as response:return json.load(response)

    def prepare(self):
        with app.connect() as db:
            db.execute("UPDATE collection_settings SET auto_enabled=0")
            db.execute("UPDATE accounts SET douyin_id='97350513058',homepage='',work_count=99,last_collected='old' WHERE id='43'")
        self.call('/api/collection/enqueue',{'ids':['43']},self.owner)

    def test_queue_success_shared_and_identity_guard(self):
        self.prepare()
        self.assertIsNone(self.worker('/api/collection/next',engine='old')['job'])
        job=self.worker('/api/collection/next')['job']
        self.assertIsNone(self.worker('/api/collection/next')['job'])
        body=result_body(job,dict(status='success',account='different',count=2,url='https://www.douyin.com/user/MS4wabcde'))
        self.assertIn('error',body)
        body=result_body(job,dict(status='success',account='97350513058',count=0,url='https://www.douyin.com/user/MS4wabcde'))
        self.worker('/api/collection/result',body)
        account=next(a for a in self.board()['accounts'] if a['id']=='43')
        self.assertEqual(account['work_count'],0);self.assertEqual(account['collection_state'],'采集成功')
        self.assertTrue(account['last_collected']!='old')
        self.assertTrue(self.worker('/api/collection/result',body)['ignored'])

    def test_verification_pause_preserves_last_success_and_resume(self):
        self.prepare();job=self.worker('/api/collection/next')['job']
        self.worker('/api/collection/result',result_body(job,dict(status='failed',error='SearchNeedsVerification')))
        account=next(a for a in self.board()['accounts'] if a['id']=='43')
        self.assertEqual((account['work_count'],account['last_collected']),(99,'old'))
        self.assertTrue(self.board()['collection']['paused']);self.assertIsNone(self.worker('/api/collection/next')['job'])
        self.worker('/api/collection/heartbeat',{'status':'待命'})
        self.assertTrue(self.board()['collection']['worker_online'])
        self.call('/api/collection/settings',{'open_browser':True},self.owner)
        self.assertEqual(self.worker('/api/collection/heartbeat',{'status':'待命'})['command'],'open')
        self.assertEqual(self.worker('/api/collection/heartbeat',{'status':'待命'})['command'],'')
        self.call('/api/collection/settings',{'resume':True},self.owner)
        self.assertIsNotNone(self.worker('/api/collection/next')['job'])

    def test_worker_authentication_and_stale_lease(self):
        with self.assertRaises(HTTPError) as error:self.worker('/api/collection/heartbeat',{'status':'待命'},token='wrong')
        self.assertEqual(error.exception.code,403)
        self.prepare();old=self.worker('/api/collection/next')['job']
        with app.connect() as db:db.execute("UPDATE collection_jobs SET started_at='2000-01-01'")
        new=self.worker('/api/collection/next')['job'];self.assertNotEqual(old['token'],new['token'])
        self.assertTrue(self.worker('/api/collection/result',result_body(old,dict(status='success',account='97350513058',count=1,url='https://www.douyin.com/user/MS4wabcde')))['ignored'])

    def test_cancel_waiting_and_pause_are_not_data_deletion(self):
        self.prepare()
        self.call('/api/collection/settings',{'cancel_waiting':True},self.owner)
        self.assertIsNone(self.worker('/api/collection/next')['job'])
        account=next(a for a in self.board()['accounts'] if a['id']=='43')
        self.assertEqual((account['work_count'],account['last_collected']),(99,'old'))
        self.assertEqual(account['collection_state'],'已取消')
        self.call('/api/collection/enqueue',{'ids':['43']},self.owner)
        self.call('/api/collection/settings',{'pause':True},self.owner)
        self.assertIsNone(self.worker('/api/collection/next')['job'])

    def test_offline_control_and_cached_homepage_still_require_exact_handle(self):
        with self.assertRaises(HTTPError) as error:self.call('/api/collection/settings',{'open_browser':True},self.owner)
        self.assertEqual(error.exception.code,400)
        self.prepare()
        with app.connect() as db:
            db.execute("UPDATE accounts SET homepage='https://www.douyin.com/user/MS4wabcde' WHERE id='43'")
            db.execute("UPDATE collection_jobs SET homepage='https://www.douyin.com/user/MS4wabcde' WHERE account_id='43'")
        job=self.worker('/api/collection/next')['job']
        with self.assertRaises(HTTPError) as error:self.worker('/api/collection/result',dict(account_id='43',token=job['token'],source_url=job['homepage'],count=5,matched_douyin_id='wrong'))
        self.assertEqual(error.exception.code,400)

    def test_progress_status_requires_login_and_reflects_pause(self):
        with self.assertRaises(HTTPError) as error:
            with urlopen(self.base+'/api/collection/status'):pass
        self.assertEqual(error.exception.code,401)
        result=self.call('/api/collection/status',client=self.owner)
        self.assertTrue(result['can_manage'])
        self.call('/api/collection/settings',{'pause':True},self.owner)
        self.assertTrue(self.call('/api/collection/status',client=self.owner)['collection']['paused'])

if __name__=='__main__':unittest.main()
