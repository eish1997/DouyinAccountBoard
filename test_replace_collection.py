import copy
import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch
import app
import collection
import f2_worker
from test_collection import CollectionTests

spec=importlib.util.spec_from_file_location('replace_f2',Path(__file__).parent/'artifacts/reset_and_import_f2_batch.py')
replace_f2=importlib.util.module_from_spec(spec);spec.loader.exec_module(replace_f2)

class ReplaceCollectionTest(unittest.TestCase):
    setUp=CollectionTests.setUp
    tearDown=CollectionTests.tearDown

    def fixture(self,db):
        db.execute("UPDATE accounts SET douyin_id='handle43',work_count=12,last_collected='2026-01-01',status='封禁',note='保留业务备注' WHERE id='43'")
        db.execute("UPDATE accounts SET douyin_id='handle50',work_count=34 WHERE id='50'")
        collection.enqueue(db,['43','50'])
        db.execute("INSERT INTO operations_history(group_id,at,actor_name,action,entity) VALUES('old',0,'test','/api/collection/result','account:43')")
        db.execute("INSERT INTO operations_history(group_id,at,actor_name,action,entity) VALUES('business',0,'test','/api/account/status','account:43')")
        observed=collection.timestamp()
        return {'source':'F2 user.aweme_count','results':[{'account':'handle43','count':0,'status':'success','observed_at':observed,'url':'https://www.douyin.com/user/MS4wabcde'}]},['handle43']

    def test_replacement_only_collection_fields_and_history(self):
        with app.connect() as db:
            report,ids=self.fixture(db)
            before={r['id']:dict(r) for r in db.execute('SELECT * FROM accounts')}
            result=replace_f2.replace(db,report,ids)
            self.assertEqual(result['imported'],1)
            after={r['id']:dict(r) for r in db.execute('SELECT * FROM accounts')}
            for ident,row in before.items():
                self.assertEqual({k:v for k,v in row.items() if k not in replace_f2.FIELDS},{k:v for k,v in after[ident].items() if k not in replace_f2.FIELDS})
            self.assertEqual(after['43']['work_count'],0)
            self.assertEqual(after['43']['last_collected'],report['results'][0]['observed_at'])
            self.assertIsNone(after['50']['work_count'])
            self.assertEqual(db.execute('SELECT count(*) FROM collection_jobs').fetchone()[0],0)
            self.assertEqual(tuple(db.execute('SELECT auto_enabled,paused FROM collection_settings').fetchone()),(0,1))
            self.assertEqual(db.execute("SELECT count(*) FROM operations_history WHERE action='/api/collection/result'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM operations_history WHERE action='/api/account/status'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM operations_history WHERE action='/api/collection/import-report'").fetchone()[0],1)

    def test_reject_bad_reports_before_any_clear(self):
        with app.connect() as db:
            report,ids=self.fixture(db)
            cases=[]
            for field,value in [('account','unknown'),('count',True),('observed_at','2099-01-01T00:00:00+08:00'),('url','https://evil.example/user/abc'),('status','failed')]:
                r=copy.deepcopy(report);r['results'][0][field]=value;cases.append(r)
            for r in cases:
                with self.assertRaises(ValueError):replace_f2.replace(db,r,ids)
                self.assertEqual(db.execute("SELECT work_count FROM accounts WHERE id='43'").fetchone()[0],12)
                self.assertEqual(db.execute('SELECT count(*) FROM collection_jobs').fetchone()[0],2)
            db.execute("UPDATE accounts SET douyin_id='handle43' WHERE id='50'")
            with self.assertRaises(ValueError):replace_f2.replace(db,report,ids)

    def test_running_job_stops_cleanup(self):
        with app.connect() as db:
            report,ids=self.fixture(db)
            db.execute("UPDATE collection_jobs SET state='running' WHERE account_id='43'")
            with self.assertRaises(ValueError):replace_f2.replace(db,report,ids)
            self.assertEqual(db.execute("SELECT work_count FROM accounts WHERE id='43'").fetchone()[0],12)

    def test_desktop_f2_worker_uses_local_origin_only(self):
        with patch.dict(os.environ,{'DOUYIN_BOARD_WORKER_ORIGIN':'http://127.0.0.1:12345','DOUYIN_BOARD_WORKER_TOKEN':'test'}):
            self.assertEqual(f2_worker.Worker().origin,'http://127.0.0.1:12345')
        for url in ['https://evil.example:443','http://localhost:12345','http://127.0.0.1:12345/path']:
            with patch.dict(os.environ,{'DOUYIN_BOARD_WORKER_ORIGIN':url,'DOUYIN_BOARD_WORKER_TOKEN':'test'}):
                with self.assertRaises(ValueError):f2_worker.Worker()

if __name__=='__main__':unittest.main()
