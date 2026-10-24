import test_auth_support
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
import app


class PhoneCardTest(unittest.TestCase):
    def test_card_inventory_import_and_account_isolation(self):
        original=app.DB
        with tempfile.TemporaryDirectory() as tmp:
            app.DB=Path(tmp)/'data'/'board.db';app.initialize();app.initialize()
            server=ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            base='http://127.0.0.1:'+str(server.server_port)
            client=test_auth_support.owner_client(app,base)
            def get():
                with client.open(base+'/api/board') as response:return json.load(response)
            def post(path,body):
                req=urllib.request.Request(base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
                with client.open(req) as response:return json.load(response)
            try:
                self.assertEqual(get()['phone_cards']['cards'],[])
                with app.connect() as db:
                    db.execute("UPDATE accounts SET registration_phone='13800000000' WHERE id IN ('43','50')")
                    db.execute("UPDATE accounts SET registration_phone='13900000000' WHERE id='58'")
                    db.execute("INSERT INTO daily VALUES('43','2026-10-15',1)")
                    before=[tuple(row) for row in db.execute('SELECT * FROM accounts ORDER BY id')]
                self.assertEqual(get()['phone_cards']['importable'],2)
                card={'phone':'13700000000','label':'A001','carrier':'中国移动','status':'正常','owner':'测试负责人','location':'卡盒1','expires_on':'2026-10-22','note':'<测试备注>'}
                result=post('/api/phone-card',card)
                card_id=result['id']
                for invalid in [card,{**card,'phone':'123'},{**card,'phone':'13600000000','status':'未知状态'},{**card,'phone':'13600000000','expires_on':'2026-02-30'},{**card,'id':999}]:
                    with self.assertRaises(urllib.error.HTTPError) as error:post('/api/phone-card',invalid)
                    self.assertEqual(error.exception.code,400);error.exception.close()
                post('/api/phone-card',{**card,'id':card_id,'phone':'13900000000','status':'停用'})
                saved=get()['phone_cards']['cards'][0]
                self.assertEqual((saved['id'],saved['phone'],saved['status'],saved['note']),(card_id,'13900000000','停用','<测试备注>'))
                self.assertEqual(post('/api/phone-cards/import',{})['added'],1)
                self.assertEqual(post('/api/phone-cards/import',{})['added'],0)
                info=get()['phone_cards'];self.assertEqual(info['importable'],0)
                self.assertEqual(len(info['cards']),2)
                self.assertEqual(next(c for c in info['cards'] if c['phone']=='13800000000')['status'],'待确认')
                self.assertEqual(next(c for c in info['cards'] if c['phone']=='13900000000')['status'],'停用')
                with app.connect() as db:
                    self.assertEqual(before,[tuple(row) for row in db.execute('SELECT * FROM accounts ORDER BY id')])
                    self.assertEqual(tuple(db.execute('SELECT * FROM daily').fetchone()),('43','2026-10-15',1))
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                post('/api/account/card-readiness',{'id':'43','card_stability':'已稳定'})
                post('/api/account/card-readiness',{'id':'43','password_state':'已修改'})
                record=next(a for a in get()['accounts'] if a['id']=='43')
                sibling=next(a for a in get()['accounts'] if a['id']=='50')
                self.assertEqual((record['card_stability'],record['password_state']),('已稳定','已修改'))
                self.assertEqual((sibling['card_stability'],sibling['password_state']),('待确认','待确认'))
                with self.assertRaises(urllib.error.HTTPError) as error:
                    post('/api/account/card-readiness',{'id':'43','password_state':'密码原文'})
                error.exception.close()
                details={**card,'id':card_id,'phone':'13900000000','status':'待注销','ownership':'借用','keeper':'负责人','code_contact':'提供人','code_state':'待接码','code_note':'小号改密需接码','return_state':'待归还','return_due':'2026-10-22'}
                post('/api/phone-card',details)
                post('/api/phone-card',{**card,'id':card_id,'phone':'13900000000','status':'待注销'})
                saved=next(c for c in get()['phone_cards']['cards'] if c['id']==card_id)
                self.assertEqual((saved['ownership'],saved['code_state'],saved['return_state'],saved['code_note']),('借用','待接码','待归还','小号改密需接码'))
                self.assertEqual(next(a for a in get()['accounts'] if a['id']=='58')['status'],'待确认')
                with self.assertRaises(urllib.error.HTTPError) as error:
                    post('/api/phone-card',{**details,'return_state':'已归还'})
                error.exception.close()
                post('/api/phone-card',{**details,'return_state':'已归还','returned_on':'2026-10-15'})
            finally:
                server.shutdown();server.server_close();app.DB=original


if __name__=='__main__':unittest.main()
