import json
import tempfile
import unittest
from pathlib import Path
import app
import import_publishing_layout
import publishing_layout
import audit_publishing_matrix as audit


class PublishingLayoutTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name);self.old_db=app.DB
        app.DB=self.folder/'board.db';app.initialize()

    def tearDown(self):app.DB=self.old_db;self.temp.cleanup()

    def test_layout_positions_preserve_accounts_and_unknown_cells(self):
        main=audit.SOURCES['main']['name'];three=audit.SOURCES['three']['name']
        header=['']*11
        for i in range(7,11):header[i]='发布的设备'+str(i-6)
        matrix=[header,['3号电脑','','','','','','','43-帮途道路救援','','','999-未确认账号']]
        third=[header,['2号电脑','','','','','','','1-独立未匹配','','','']]
        records=[dict(source=main,matrix_cell='H2',kind='发布',result='对应电脑错误',handle='verified_id',reason=''),dict(source=main,matrix_cell='K2',kind='发布',result='待核对',handle='',reason='昵称未匹配'),dict(source=three,matrix_cell='H2',kind='发布',result='待核对',handle='',reason='未匹配')]
        for name,value in [('main-matrix',matrix),('three-matrix',third),('audit',dict(positions=records))]:(self.folder/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
        with app.connect() as db:
            db.execute("UPDATE accounts SET douyin_id='verified_id' WHERE id='43'")
            before=[tuple(r) for r in db.execute('SELECT * FROM accounts ORDER BY id')]
            report=import_publishing_layout.apply(db,self.folder)
            self.assertEqual((report['linked'],report['unresolved']),(1,2))
            self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM accounts ORDER BY id')])
            layout=publishing_layout.overview(db)
            self.assertTrue(layout['enabled']);self.assertEqual(len(layout['cells']),8)
            own=next(c for c in layout['cells'] if c['account_id']=='43')
            self.assertEqual((own['computer_id'],own['slot'],own['source_cell']),('3',1,'H2'))
            self.assertIsNone(next(c for c in layout['cells'] if c['source_cell']=='K2' and c['computer_id']=='3')['account_id'])
            self.assertEqual(import_publishing_layout.apply(db,self.folder)['cells_added'],0)
            self.assertEqual(list(db.execute('PRAGMA foreign_key_check')),[])

    def test_aliases_slots_and_document_namespaces(self):
        self.assertEqual(audit.split_label('14多开-同城周师傅道路救援(30)'),('14','同城周师傅道路救援'))
        self.assertEqual(audit.computer_number('111'),'111')
        self.assertIsNone(audit.computer_number('19-20 33-35 方政黑龙江电信'))
        self.assertEqual(audit.col_name(17),'R')


if __name__=='__main__':unittest.main()
