import json
import tempfile
import unittest
from pathlib import Path
import app
import import_office_three
import phone_devices


class OfficeThreeTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.folder=Path(self.temp.name)
        self.original=app.DB
        app.DB=self.folder/'board.db'
        app.initialize()

    def tearDown(self):
        app.DB=self.original
        self.temp.cleanup()

    def snapshot(self):
        header=['']*22
        for i,value in [(2,'注册抖音用的手机号'),(6,'抖音在哪个手机上'),(7,'抖音号'),(11,'对应电脑')]:header[i]=value
        rows=[header]
        for name,handle,phone,computer,state in [('新甲','new_test_a','1','2','正常'),('','new_test_b','1','','需实名校验')]:
            row=['']*22
            for i,value in [(5,name),(6,phone),(7,handle),(11,computer),(15,state),(18,'2026年9月1日')]:row[i]=value
            rows.append(row)
        matrix=[['']*26,['2号电脑','','影刀测试','192.168.9.2','','负责人','图文']+['']*19]
        for name,values in [('accounts',rows),('matrix',matrix)]:
            (self.folder/(name+'.json')).write_text(json.dumps(values,ensure_ascii=False),encoding='utf-8')

    def test_import_isolated_and_idempotent(self):
        self.snapshot()
        with app.connect() as db:
            phone_devices.save(db,{'id':'1'},create=True)
            phone_devices.assign(db,'43','1')
            old_accounts=[tuple(r) for r in db.execute('SELECT * FROM accounts ORDER BY id')]
            old_computer=tuple(db.execute("SELECT * FROM computers WHERE id='2'").fetchone())
            report=import_office_three.apply(db,self.folder)
            self.assertEqual((report['accounts_added'],report['devices_added'],report['computers_added']),(2,1,1))
            self.assertEqual(report['unassigned_computers'],1)
            self.assertEqual(old_accounts,[tuple(r) for r in db.execute('SELECT * FROM accounts WHERE office_group<>? ORDER BY id',('three',))])
            self.assertEqual(old_computer,tuple(db.execute("SELECT * FROM computers WHERE id='2'").fetchone()))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM phone_devices WHERE id IN ('1','office3:1')").fetchone()[0],2)
            self.assertEqual(import_office_three.apply(db,self.folder)['accounts_added'],0)
            self.assertEqual(list(db.execute('PRAGMA foreign_key_check')),[])

    def test_device_edit_remove_and_reassign_preserve_office(self):
        with app.connect() as db:
            phone_devices.save(db,{'id':'office3:1'},create=True)
            phone_devices.save(db,{'id':'1'},create=True)
            phone_devices.assign(db,'43','office3:1')
            self.assertEqual(db.execute("SELECT office_group FROM accounts WHERE id='43'").fetchone()[0],'three')
            phone_devices.save(db,{'id':'office3:1','owner':'测试员工'})
            self.assertEqual(db.execute("SELECT owner FROM phone_devices WHERE id='office3:1'").fetchone()[0],'测试员工')
            phone_devices.assign(db,'43',None)
            self.assertEqual(db.execute("SELECT office_group FROM accounts WHERE id='43'").fetchone()[0],'three')
            phone_devices.assign(db,'43','1')
            self.assertEqual(db.execute("SELECT office_group FROM accounts WHERE id='43'").fetchone()[0],'')
            with self.assertRaises(ValueError):phone_devices.save(db,{'id':'other:1'},create=True)

    def test_duplicate_source_handle_rejected_before_write(self):
        self.snapshot()
        path=self.folder/'accounts.json'
        rows=json.loads(path.read_text(encoding='utf-8'))
        rows[2][7]=rows[1][7]
        path.write_text(json.dumps(rows),encoding='utf-8')
        with app.connect() as db:
            count=db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]
            with self.assertRaises(ValueError):import_office_three.apply(db,self.folder)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0],count)


if __name__=='__main__':unittest.main()
