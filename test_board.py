import test_auth_support
import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer

spec=importlib.util.spec_from_file_location('board',Path(__file__).with_name('app.py'))
board=importlib.util.module_from_spec(spec); spec.loader.exec_module(board)

class BoardTest(unittest.TestCase):
    def test_persistence_history_move_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            board.DB=Path(tmp)/'board.db'; board.initialize(); board.initialize()
            server=ThreadingHTTPServer(('127.0.0.1',0),board.Handler)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            base='http://127.0.0.1:'+str(server.server_port)
            client=test_auth_support.owner_client(board,base)
            def get(day):
                return json.load(client.open(base+'/api/board?date='+day))
            def post(path,data):
                req=urllib.request.Request(base+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})
                return json.load(client.open(req))
            try:
                initial=get('2026-10-12')
                self.assertIn('养号7天', initial['statuses'])
                self.assertEqual((len(initial['computers']),len(initial['accounts'])),(10,27))
                self.assertFalse(any(a['computer_id']=='11' for a in initial['accounts']))
                self.assertTrue(all(a['work_count'] is None for a in initial['accounts']))
                account=next(a for a in initial['accounts'] if a['id']=='43')
                post('/api/daily',{'id':'43','date':'2026-10-12','published':True})
                self.assertEqual(next(a for a in get('2026-10-12')['accounts'] if a['id']=='43')['published'],1)
                self.assertEqual(next(a for a in get('2026-10-11')['accounts'] if a['id']=='43')['published'],0)
                post('/api/account',{**account,'computer_id':'11','slot':1,'status':'养号7天','note':'验证备注'})
                board.initialize()
                saved=next(a for a in get('2026-10-12')['accounts'] if a['id']=='43')
                self.assertEqual((saved['computer_id'],saved['status'],saved['published']),('11','养号7天',1))
                with self.assertRaises(urllib.error.HTTPError) as error:
                    post('/api/account',{**saved,'computer_id':'3','slot':1})
                self.assertEqual(error.exception.code,400)
                post('/api/bulk-status',{'ids':['43','50'],'status':'养号7天'})
                self.assertTrue(all(a['status']=='养号7天' for a in get('2026-10-12')['accounts'] if a['id'] in ['43','50']))
                with board.connect() as db:
                    db.execute("INSERT INTO computers VALUES('unassigned','未分配电脑','','','','')")
                saved=next(a for a in get('2026-10-12')['accounts'] if a['id']=='43')
                post('/api/account',{**saved,'computer_id':'unassigned','slot':1})
                unassigned=next(a for a in get('2026-10-12')['accounts'] if a['id']=='43')
                self.assertIsNone(unassigned['slot'])
                self.assertEqual(unassigned['published'],1)
                post('/api/account',{**unassigned,'computer_id':'11','slot':1})
                assigned=next(a for a in get('2026-10-12')['accounts'] if a['id']=='43')
                self.assertEqual((assigned['computer_id'],assigned['slot'],assigned['published']),('11',1,1))
                post('/api/account',{**assigned,'account_type':'大号','registered_on':'2026-09-14','auxiliary_state':'未注册','registration_phone':'13800000000'})
                post('/api/account/create',{'name':'测试辅助账号','computer_id':'unassigned','slot':1,'status':'养号7天','douyin_id':'test_small_123','account_type':'小号','registration_phone':'13800000000'})
                records=get('2026-10-12')['accounts']
                child=next(a for a in records if a['douyin_id']=='test_small_123')
                parent=next(a for a in records if a['id']=='43')
                self.assertEqual((child['parent_account_id'],child['published'],child['work_count']),('43',0,None))
                self.assertEqual((parent['effective_auxiliary_state'],parent['published']),('已注册',1))
                with self.assertRaises(urllib.error.HTTPError):
                    post('/api/account/create',{'name':'重复账号','computer_id':'unassigned','slot':1,'status':'正常','douyin_id':'test_small_123'})
                self.assertEqual(len(get('2026-10-12')['accounts']),28)
                with self.assertRaises(urllib.error.HTTPError) as duplicate:
                    post('/api/account',{**parent,'slot':1,'auxiliary_state':'已注册','douyin_id':'test_small_123'})
                self.assertEqual(duplicate.exception.code,400)
                self.assertIn('抖音号已经',json.load(duplicate.exception)['error'])
                for phone in ['13900000000','']:
                    with self.assertRaises(urllib.error.HTTPError) as mismatch:
                        post('/api/account',{**parent,'slot':1,'auxiliary_state':'已注册','registration_phone':phone})
                    self.assertIn('手机号不一致',json.load(mismatch.exception)['error'])
                unchanged=next(a for a in get('2026-10-12')['accounts'] if a['id']=='43')
                self.assertEqual(unchanged['registration_phone'],'13800000000')
                self.assertEqual(unchanged['douyin_id'],parent['douyin_id'])
                with self.assertRaises(urllib.error.HTTPError):
                    post('/api/lifecycle-status',{'id':'43','auxiliary_state':'未注册'})
                target=next(a for a in get('2026-10-12')['accounts'] if a['id']=='50')
                post('/api/account',{**target,'account_type':'大号','registered_on':'2026-09-14','auxiliary_state':'未注册'})
                post('/api/lifecycle-status',{'id':'50','auxiliary_state':'已注册'})
                updated=next(a for a in get('2026-10-12')['accounts'] if a['id']=='50')
                self.assertEqual((updated['registered_on'],updated['auxiliary_state'],updated['status'],updated['computer_id']),('2026-09-14','已注册',target['status'],target['computer_id']))
                # Existing imported duplicates stay editable without creating new ones.
                with board.connect() as db:
                    db.execute("UPDATE accounts SET douyin_id='historical_duplicate' WHERE id IN ('39','50')")
                historical=next(a for a in get('2026-10-12')['accounts'] if a['id']=='50')
                post('/api/account',{**historical,'note':'保留历史重复，允许修改备注'})
                self.assertEqual(next(a for a in get('2026-10-12')['accounts'] if a['id']=='50')['note'],'保留历史重复，允许修改备注')
            finally:
                server.shutdown();server.server_close()

    def test_concurrent_identity_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            board.DB=Path(tmp)/'board.db';board.initialize()
            server=ThreadingHTTPServer(('127.0.0.1',0),board.Handler)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            base='http://127.0.0.1:'+str(server.server_port)
            client=test_auth_support.owner_client(board,base)
            records=json.load(client.open(base+'/api/board'))['accounts'][:2]
            barrier=threading.Barrier(2);results=[]
            def edit(account):
                barrier.wait()
                req=urllib.request.Request(base+'/api/account',data=json.dumps({**account,'douyin_id':'concurrent_test'}).encode(),headers={'Content-Type':'application/json'})
                try:
                    with client.open(req) as response:results.append(response.status)
                except urllib.error.HTTPError as error:results.append(error.code);error.close()
            try:
                threads=[threading.Thread(target=edit,args=(account,)) for account in records]
                for thread in threads:thread.start()
                for thread in threads:thread.join(timeout=20)
                self.assertEqual(sorted(results),[200,400])
                with board.connect() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM accounts WHERE douyin_id='concurrent_test'").fetchone()[0],1)
            finally:
                server.shutdown();server.server_close()

if __name__=='__main__':
    unittest.main()
