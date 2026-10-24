import json
import tempfile
import unittest
from pathlib import Path
import app
import collection

class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.original=app.DB;app.DB=Path(self.tmp.name)/'board.db';app.initialize()
    def tearDown(self):
        app.DB=self.original;self.tmp.cleanup()
    def test_search_binding_zero_failure_and_manual_state(self):
        with app.connect() as db:
            db.execute("UPDATE accounts SET douyin_id='97350513058' WHERE id='43'")
            collection.enqueue(db,['43']);job=collection.next_job(db,app.today())
            self.assertEqual(job['douyin_id'],'97350513058')
            collection.complete(db,dict(account_id='43',token=job['token'],count=0,source_url='https://www.douyin.com/user/MS4wLjABAAAA123',matched_douyin_id='97350513058'))
            a=dict(db.execute("SELECT * FROM accounts WHERE id='43'").fetchone())
            self.assertEqual(a['work_count'],0);self.assertEqual(a['status'],'待确认');self.assertTrue(a['last_collected']);self.assertTrue(a['homepage'])
            last=a['last_collected'];collection.enqueue(db,['43']);job=collection.next_job(db,app.today())
            collection.complete(db,dict(account_id='43',token=job['token'],error='安全验证',blocked=True))
            a=dict(db.execute("SELECT * FROM accounts WHERE id='43'").fetchone())
            self.assertEqual((a['work_count'],a['last_collected'],a['status']),(0,last,'待确认'))
            self.assertEqual(a['collection_state'],'需要验证');self.assertTrue(collection.overview(db)['paused'])
            self.assertIsNone(collection.next_job(db,app.today()))
    def test_reject_mismatch_and_stale_results(self):
        with app.connect() as db:
            db.execute("UPDATE accounts SET homepage='https://www.douyin.com/user/MS4wLjABAAAA123' WHERE id='43'")
            collection.enqueue(db,['43']);job=collection.next_job(db,app.today())
            with self.assertRaises(ValueError):
                collection.complete(db,dict(account_id='43',token=job['token'],count=12,source_url='https://www.douyin.com/user/MS4wLjABOTHER123'))
            db.execute("UPDATE accounts SET homepage='' WHERE id='43'")
            result=collection.complete(db,dict(account_id='43',token=job['token'],count=12,source_url=job['homepage']))
            self.assertTrue(result['ignored'])
            self.assertIsNone(db.execute("SELECT work_count FROM accounts WHERE id='43'").fetchone()[0])
    def test_url_restriction_and_queue_deduplication(self):
        for url in ['https://www.douyin.com.evil.com/user/abcdef','http://127.0.0.1/user/abcdef','https://www.douyin.com/?x=1','https://www.douyin.com@evil.com/user/abcdef']:
            with self.assertRaises(ValueError):collection.normalize_homepage(url)
        with app.connect() as db:
            self.assertEqual(collection.enqueue(db,['43'])['missing'],1)
            db.execute("UPDATE accounts SET douyin_id='example123' WHERE id='43'")
            self.assertEqual(collection.enqueue(db,['43','43'])['queued'],1)
            self.assertEqual(collection.enqueue(db,['43'])['queued'],0)
        app.initialize()
        with app.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM collection_jobs").fetchone()[0],1)

if __name__=='__main__':unittest.main()
