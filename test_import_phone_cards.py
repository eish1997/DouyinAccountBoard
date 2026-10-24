import csv
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
import import_phone_cards as importer


class CardImportTest(unittest.TestCase):
    def test_aggregation_conflicts_no_password_and_safe_repeat(self):
        with tempfile.TemporaryDirectory() as tmp,closing(sqlite3.connect(':memory:')) as db:
            db.row_factory=sqlite3.Row
            db.execute('CREATE TABLE accounts(id TEXT PRIMARY KEY)')
            header=['谁的卡\n红色已注销','','注册抖音用的手机号']+['']*18
            rows=[header]
            for owner,location,phone in [('张某','1','13800000000'),('张某抖音小号','1','13800000000'),('李某接码','外借','13900000000'),('王某','','13900000000'),('其他','','未知手机号')]:
                row=['']*21;row[0]=owner;row[1]=location;row[2]=phone;row[14]='PRIVATE_PASSWORD_SENTINEL';rows.append(row)
            content=io.StringIO();csv.writer(content).writerows(rows)
            snapshot=Path(tmp)/'snapshot.json';snapshot.write_text(json.dumps({'csv_data':content.getvalue()}),encoding='utf-8')
            report=importer.apply(db,snapshot)
            self.assertEqual((report['added'],report['unique_phones'],report['owner_conflicts']),(2,2,1))
            cards=[dict(row) for row in db.execute('SELECT * FROM phone_cards ORDER BY phone')]
            self.assertEqual(cards[0]['owner'],'张某')
            self.assertEqual(cards[1]['owner'],'')
            self.assertEqual(cards[1]['code_contact'],'李某')
            self.assertEqual(cards[1]['code_state'],'无待办')
            self.assertNotIn('PRIVATE_PASSWORD_SENTINEL',json.dumps(cards))
            db.execute("UPDATE phone_cards SET owner='人工确认人',status='待注销',code_state='待接码' WHERE phone='13900000000'")
            repeated=importer.apply(db,snapshot)
            self.assertEqual((repeated['added'],repeated['fields_filled']),(0,0))
            card=dict(db.execute("SELECT * FROM phone_cards WHERE phone='13900000000'").fetchone())
            self.assertEqual((card['owner'],card['status'],card['code_state']),('人工确认人','待注销','待接码'))


if __name__=='__main__':unittest.main()
