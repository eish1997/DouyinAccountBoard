import unittest
import sqlite3
from datetime import date
import account_lifecycle as lifecycle


class LifecycleTest(unittest.TestCase):
    def test_day_30_boundary_missing_date_and_independent_child(self):
        def account(aid,registered='',kind='大号',aux='未注册',parent=None):
            return dict(id=aid,name=aid,account_type=kind,parent_account_id=parent,
                        registered_on=registered,auxiliary_state=aux)
        accounts=[account('1','2026-09-14'),account('2','2026-09-15'),account('3'),
                  account('4',kind='大号',aux='已注册'),account('5',kind='小号',parent='4')]
        lifecycle.enrich(accounts,'2026-10-14')
        self.assertEqual(accounts[0]['lifecycle'],'满30天未注册')
        self.assertEqual(accounts[0]['mature_on'],'2026-10-14')
        self.assertEqual((accounts[1]['lifecycle'],accounts[1]['days_remaining']),('未满30天',1))
        self.assertEqual(accounts[2]['lifecycle'],'注册日期待补充')
        self.assertEqual(accounts[3]['effective_auxiliary_state'],'已注册')
        self.assertEqual(accounts[4]['lifecycle'],'小号')

    def test_phone_matching_and_relationship_validation(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE accounts(id TEXT PRIMARY KEY,name TEXT)')
        lifecycle.migrate(db)
        db.execute("INSERT INTO accounts(id,name,account_type,registration_phone) VALUES('1','大号','大号','13800000000')")
        db.execute("INSERT INTO accounts(id,name,account_type) VALUES('2','小号','小号')")
        old=db.execute("SELECT * FROM accounts WHERE id='2'").fetchone()
        result=lifecycle.validate(db,'2',{'registration_phone':'13800000000'},old,'2026-10-14')
        self.assertEqual(result[1],'1')
        for body in [{'parent_account_id':'2'}, {'parent_account_id':'1','registration_phone':'13900000000'}, {'registered_on':'2026-10-15'}]:
            with self.assertRaises(ValueError):
                lifecycle.validate(db,'2',body,old,'2026-10-14')
        db.execute("UPDATE accounts SET parent_account_id='1' WHERE id='2'")
        old=db.execute("SELECT * FROM accounts WHERE id='1'").fetchone()
        with self.assertRaises(ValueError):
            lifecycle.validate(db,'1',{'auxiliary_state':'未注册'},old,'2026-10-14')
        db.close()


if __name__=='__main__':unittest.main()
