"""Scoped board/image/status HTTP checks using the configured PG provider."""
import json
import base64
import csv
import io
import concurrent.futures
import time
import os
import sys
import threading
import urllib.error
import urllib.request
from test_isolation import checked_root, install
from pg_synthetic_migration import connect_synthetic, install_schema
from pg_service_adapter import ServiceConnection
from pg_runtime import PostgresRuntime


def main(write_checks=False):
    root=checked_root();install(root)
    import app
    import auth
    import office_scope as scope
    import test_auth_support
    from test_release_core import png
    secret='合成B范围外不可见标记'
    with connect_synthetic() as conn:
        install_schema(conn);conn.execute('SET search_path=board_candidate,pg_catalog')
        db=ServiceConnection(conn,writer_epoch=1,restore_generation='synthetic-read-1',schema='board_candidate')
        with conn.transaction():
            setup=auth.mint_setup(db)
            token=auth.setup_owner(db,dict(username='test_owner',password=test_auth_support.TEST_PASSWORD),'board_setup='+setup)
            owner=auth.session(db,'board_session='+token)
            employee=auth.save_user(db,owner,dict(username='synthetic_employee',password=test_auth_support.TEST_PASSWORD))
            auth.save_user(db,owner,dict(username='synthetic_viewer',role='viewer',password=test_auth_support.TEST_PASSWORD))
            for ident,name in [('pc-a','合成电脑A'),('pc-b',secret)]:db.execute('INSERT INTO computers(id,name) VALUES(?,?)',(ident,name))
            for ident,name in [('1','合成共用手机'),('2',secret)]:db.execute('INSERT INTO phone_devices(id,name) VALUES(?,?)',(ident,name))
            db.execute("INSERT INTO accounts(id,name,computer_id,phone_device_id,status,douyin_id,registration_phone) VALUES('account-b',?,'pc-b','1','正常','synthetic-b','19900000002')",(secret,))
            db.execute("INSERT INTO accounts(id,name,computer_id,phone_device_id,parent_account_id,status,douyin_id,registration_phone) VALUES('account-a','合成账号A','pc-b','2','account-b','正常','synthetic-a','19900000001')")
            db.execute("INSERT INTO accounts(id,name,office_group) VALUES('account-unknown',?,'three')",(secret,))
            db.execute("INSERT INTO phone_cards(id,phone,label) VALUES(1,'13800000001','合成共用卡')")
            db.execute("INSERT INTO phone_cards(id,phone,label) VALUES(2,'13800000002',?)",(secret,))
            # Explicit fixture IDs must advance the identity just as migration does.
            db.execute('ALTER TABLE phone_cards ALTER COLUMN id RESTART WITH 3')
            db.execute('INSERT INTO collection_settings(id,auto_enabled) VALUES(1,0)');db.execute('INSERT INTO collection_worker(id) VALUES(1)')
            for ident in ('account-a','account-b'):
                db.execute("INSERT INTO collection_jobs(account_id,homepage,token,state) VALUES(?,'','synthetic-inert','queued')",(ident,))
                db.execute('INSERT INTO nurture_records(account_id,image_count) VALUES(?,1)',(ident,))
                db.execute("INSERT INTO nurture_images VALUES(?,?,1,'2026-10-21','image/png',?,'合成人员')",('image-'+ident,ident,png()))
            db.execute("UPDATE accounts SET status='封禁',account_type='大号' WHERE id='account-b'")
            db.execute("UPDATE nurture_records SET workflow='运行',active_since='2026-10-21',started_on='2026-10-21' WHERE account_id='account-b'")
            db.execute("INSERT INTO publishing_layout_state VALUES(1,'synthetic-batch')")
            db.execute("INSERT INTO publishing_layout VALUES('synthetic-batch','pc-a',1,'account-a','合成格子A','','','','')")
            db.execute("INSERT INTO publishing_layout VALUES('synthetic-batch','pc-a',2,'account-b',?,'','','','')",(secret,))
            db.execute("UPDATE runtime_control SET mode='open',restore_generation='synthetic-read-1'")
            db.execute("INSERT INTO operations_history(group_id,at,actor_name,action,entity,before_json,after_json) VALUES('legacy',1,'合成人员','legacy','account:account-a','null',?)",(json.dumps(dict(id='account-a',name=secret)),))
        db.begin_write()
        office_a=scope.create_office(db,owner,'合成办公室A');office_b=scope.create_office(db,owner,secret)
        for kind,ident,offices in [('accounts','account-a',[office_a]),('accounts','account-b',[office_b]),
            ('computers','pc-a',[office_a]),('computers','pc-b',[office_b]),('devices','1',[office_a,office_b]),
            ('devices','2',[office_b]),('cards',1,[office_a,office_b]),('cards',2,[office_b])]:
            scope.assign_entity(db,owner,kind,ident,offices)
        scope.grant_user(db,owner,employee,[office_a]);db.commit()
    runtime=PostgresRuntime(dict(host='127.0.0.1',port=int(os.environ['PGPORT']),database='synthetic_probe',user='synthetic_owner',schema='board_candidate',writer_epoch=1,restore_generation='synthetic-read-1',connection_limit=3,synthetic_root=str(root)),{'DOUYIN_BOARD_DATABASE_PASSWORD':os.environ['PGPASSWORD']})
    app.configure_database(runtime);app.initialize()
    class QuietHandler(app.Handler):
        def log_message(self,*args):pass
    server=app.ThreadingHTTPServer(('127.0.0.1',0),QuietHandler);thread=threading.Thread(target=server.serve_forever);thread.start()
    try:
        base='http://127.0.0.1:'+str(server.server_port)
        employee_client=test_auth_support.client(base,'synthetic_employee')
        viewer=test_auth_support.client(base,'synthetic_viewer')
        owner_client=test_auth_support.client(base)
        def get(client,path):
            with client.open(base+path,timeout=10) as response:return json.load(response)
        board=get(employee_client,'/api/board')
        assert [row['id'] for row in board['accounts']]==['account-a']
        account=board['accounts'][0]
        assert account['computer_id']=='unassigned' and account['parent_account_id'] is None and account['phone_device_id'] is None
        assert account['parent_name']==''
        assert [row['id'] for row in board['phone_cards']['cards']]==[1]
        assert set(board['phone_cards']['cards'][0]['office_ids'])=={office_a,office_b}
        assert board['phone_cards']['importable']==1
        assert [row['id'] for row in board['phone_devices']['devices']]==['1']
        assert set(board['phone_devices']['devices'][0]['office_ids'])=={office_a,office_b}
        assert board['phone_devices']['devices'][0]['account_count']==0
        assert len(board['publishing_layout']['cells'])==1
        assert [row['account_id'] for row in board['nurture']['records']]==['account-a']
        assert board['collection']['waiting']==1 and board['collection']['linked']==1
        serialized=json.dumps(board,ensure_ascii=False)
        assert secret not in serialized and 'account-b' not in serialized and 'pc-b' not in serialized
        assert all('account-b' not in key for key in board['record_versions'])
        empty=get(viewer,'/api/board')
        assert not empty['accounts'] and not empty['computers'] and not empty['record_versions'] and not empty['offices']
        assert empty['publishing_layout']==dict(enabled=False,cells=[])
        assert not empty['phone_cards']['cards'] and not empty['phone_devices']['devices'] and not empty['nurture']['records']
        for image_id in ('image-account-a','image-account-b'):
            try:
                viewer.open(base+'/api/nurture/image?id='+image_id,timeout=10)
                raise AssertionError('Unscoped image served')
            except urllib.error.HTTPError as error:assert error.code==403
        for entity in ('account:account-a','account:account-b'):
            try:
                get(viewer,'/api/workspace/history?entity='+entity)
                raise AssertionError('Unscoped history accepted')
            except urllib.error.HTTPError as error:assert error.code==403
        export_request=urllib.request.Request(base+'/api/workspace/export',data=json.dumps(dict(kind='accounts',format='csv')).encode(),headers={'Content-Type':'application/json'})
        try:
            with viewer.open(export_request,timeout=10) as response:
                export_status,export_body=response.status,response.read()
        except urllib.error.HTTPError as error:
            export_status,export_body=error.code,error.read()
        export_text=export_body.decode('utf-8','replace')
        export_leaked=(secret in export_text) or ('account-b' in export_text)
        assert export_status==403 and export_leaked is False, export_status
        assert secret in json.dumps(get(owner_client,'/api/board'),ensure_ascii=False)
        try:
            employee_client.open(base+'/api/nurture/image?id=image-account-b',timeout=10)
            raise AssertionError('Cross-office image served')
        except urllib.error.HTTPError as error:assert error.code==403
        with employee_client.open(base+'/api/nurture/image?id=image-account-a',timeout=10) as response:assert response.read()==png()
        status=get(employee_client,'/api/collection/status')['collection']
        assert status['waiting']==1 and status['linked']==1
        if write_checks:
            def post(client,path,body):
                request=urllib.request.Request(base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
                try:
                    with client.open(request,timeout=10) as response:return response.status,json.load(response)
                except urllib.error.HTTPError as error:return error.code,json.load(error)
            with runtime.connect() as raw_db:
                viewer_row=raw_db.execute("SELECT id,name FROM auth_users WHERE username='synthetic_viewer'").fetchone()
            permitted_viewer=post(owner_client,'/api/auth/users',dict(id=viewer_row['id'],name=viewer_row['name'],role='viewer',permissions=['history_view','data_export'],office_ids=[],active=True,request_id='3c3c3c3c-3c3c-43c3-83c3-3c3c3c3c3c3c'))
            assert permitted_viewer[0]==200, permitted_viewer[0]
            permitted=test_auth_support.client(base,'synthetic_viewer')
            assert not get(permitted,'/api/board')['accounts']
            for image_id in ('image-account-a','image-account-b'):
                try:
                    permitted.open(base+'/api/nurture/image?id='+image_id,timeout=10)
                    raise AssertionError('Unscoped image served')
                except urllib.error.HTTPError as error:assert error.code==403
            history_list=get(permitted,'/api/workspace/history')
            history_text=json.dumps(history_list,ensure_ascii=False)
            history_leaked=(secret in history_text) or ('account-b' in history_text)
            assert len(history_list['entries'])==0 and history_leaked is False, len(history_list['entries'])
            for entity in ('account:account-a','account:account-b'):
                try:
                    get(permitted,'/api/workspace/history?entity='+entity)
                    raise AssertionError('Unscoped history accepted')
                except urllib.error.HTTPError as error:assert error.code==403
            for kind in ('accounts','cards','devices'):
                exported=post(permitted,'/api/workspace/export',dict(kind=kind,format='csv'))
                assert exported[0]==200, exported[0]
                exported_text=base64.b64decode(exported[1]['content']).decode('utf-8-sig')
                exported_leaked=(secret in exported_text) or ('account-b' in exported_text) or ('account-a' in exported_text) or ('合成账号A' in exported_text) or ('13800000002' in exported_text)
                assert exported[1].get('count')==0 and exported_leaked is False, (kind, exported[1].get('count'))
            update=dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a])
            missing_person=post(owner_client,'/api/auth/users',update)
            assert missing_person[0]==400 and missing_person[1].get('code')=='REQUEST_ID_REQUIRED', missing_person
            blank_person=post(owner_client,'/api/auth/users',dict(update,request_id=''))
            assert blank_person[0]==400 and blank_person[1].get('code')=='REQUEST_ID_REQUIRED', blank_person
            unchanged_person=next(person for person in get(owner_client,'/api/auth/users')['users'] if person['id']==employee)
            assert 'account_manage' not in unchanged_person['permissions']
            assert post(owner_client,'/api/auth/users',dict(update,request_id='c0c0c0c0-c0c0-4c0c-8c0c-c0c0c0c0c0c0'))[0]==200
            personnel=get(owner_client,'/api/auth/users')
            assert personnel['scope_enabled'] is True
            assert next(person for person in personnel['users'] if person['id']==employee)['office_ids']==[office_a]
            assert {office['id'] for office in personnel['offices']}=={office_a,office_b}
            missing_office=post(owner_client,'/api/auth/offices',dict(name='合成管理界面办公室'))
            assert missing_office[0]==400 and missing_office[1].get('code')=='REQUEST_ID_REQUIRED', missing_office
            blank_office=post(owner_client,'/api/auth/offices',dict(name='合成管理界面办公室',request_id=''))
            assert blank_office[0]==400 and blank_office[1].get('code')=='REQUEST_ID_REQUIRED', blank_office
            assert not any(office['name']=='合成管理界面办公室' for office in get(owner_client,'/api/auth/users')['offices'])
            created_office=post(owner_client,'/api/auth/offices',dict(name='合成管理界面办公室',request_id='e5e5e5e5-e5e5-4e5e-8e5e-e5e5e5e5e5e5'))
            assert created_office[0]==200,created_office
            inventory=get(owner_client,'/api/auth/entity-scopes?kind=accounts')
            pending_entity=next(row for row in inventory['entities'] if row['id']=='account-unknown')
            assignment=dict(kind='accounts',id='account-unknown',expected_scope=pending_entity['scope_version'],office_ids=[created_office[1]['id']])
            missing_scope=post(owner_client,'/api/auth/entity-scope',assignment)
            assert missing_scope[0]==400 and missing_scope[1].get('code')=='REQUEST_ID_REQUIRED', missing_scope
            blank_scope=post(owner_client,'/api/auth/entity-scope',dict(assignment,request_id=''))
            assert blank_scope[0]==400 and blank_scope[1].get('code')=='REQUEST_ID_REQUIRED', blank_scope
            assert created_office[1]['id'] not in next(row['office_ids'] for row in get(owner_client,'/api/auth/entity-scopes?kind=accounts')['entities'] if row['id']=='account-unknown')
            assigned=post(owner_client,'/api/auth/entity-scope',dict(assignment,request_id='e8e8e8e8-e8e8-4e8e-8e8e-e8e8e8e8e8e8'))
            assert assigned[0]==200,assigned
            assert post(owner_client,'/api/auth/entity-scope',dict(assignment,office_ids=[office_a],request_id='e9e9e9e9-e9e9-4e9e-8e9e-e9e9e9e9e9e9'))[0]==409
            invalid=dict(assignment,expected_scope=assigned[1]['scope_version'],office_ids=[office_a,office_b],request_id='eaeaeaea-eaea-4eae-8eae-eaeaeaeaeaea')
            assert post(owner_client,'/api/auth/entity-scope',invalid)[0]==400
            refreshed=next(row for row in get(owner_client,'/api/auth/entity-scopes?kind=accounts')['entities'] if row['id']=='account-unknown')
            assert refreshed['office_ids']==[created_office[1]['id']]
            employee_client=test_auth_support.client(base,'synthetic_employee')
            assert post(employee_client,'/api/auth/offices',dict(name='禁止员工创建'))[0]==403
            assert post(employee_client,'/api/auth/entity-scope',dict(assignment,expected_scope=assigned[1]['scope_version']))[0]==403
            try:get(employee_client,'/api/auth/entity-scopes?kind=computers');raise AssertionError('employee scope inventory allowed')
            except urllib.error.HTTPError as error:assert error.code==403
            day=get(employee_client,'/api/board')['today']
            assert post(employee_client,'/api/daily',dict(id='account-b',date=day,published=True))[0]==403
            assert post(employee_client,'/api/batch-edit',dict(kind='accounts',ids=['account-a','account-b'],patch={'status':'停用'}))[0]==403
            assert get(employee_client,'/api/board')['accounts'][0]['status']=='正常'
            assert post(employee_client,'/api/account/patch',dict(id='account-a',changes={'slot':5}))[0]==403
            assert post(employee_client,'/api/phone-card',dict(id=1,phone='13800000001',status='正常'))[0]==403
            assert post(employee_client,'/api/phone-device',dict(id='1',state='在用'))[0]==403
            create=dict(name='合成范围新增',computer_id='pc-a',slot=1,status='正常')
            assert post(employee_client,'/api/account/create',create)[0]==400
            assert post(employee_client,'/api/account/create',dict(create,office_id=office_b))[0]==403
            missing_id=post(employee_client,'/api/account/create',dict(create,office_id=office_a))
            assert missing_id[0]==400 and missing_id[1].get('code')=='REQUEST_ID_REQUIRED', missing_id
            blank_id=post(employee_client,'/api/account/create',dict(create,office_id=office_a,request_id=''))
            assert blank_id[0]==400 and blank_id[1].get('code')=='REQUEST_ID_REQUIRED', blank_id
            assert not any(row['name']=='合成范围新增' for row in get(owner_client,'/api/board')['accounts'])
            assert post(employee_client,'/api/account/create',dict(create,office_id=office_a,request_id='c4c4c4c4-c4c4-4c4c-8c4c-c4c4c4c4c4c4'))[0]==200
            new=next(row for row in get(employee_client,'/api/board')['accounts'] if row['name']=='合成范围新增')
            assert new['office_ids']==[office_a]
            limited_body=dict(username='synthetic_patch_employee',name='合成字段员工',password=test_auth_support.TEST_PASSWORD,permissions=['account_status','account_progress'],office_ids=[office_a])
            missing_limited=post(owner_client,'/api/auth/users',limited_body)
            assert missing_limited[0]==400 and missing_limited[1].get('code')=='REQUEST_ID_REQUIRED', missing_limited
            assert not any(person['username']=='synthetic_patch_employee' for person in get(owner_client,'/api/auth/users')['users'])
            limited=post(owner_client,'/api/auth/users',dict(limited_body,request_id='cacacaca-caca-4cac-8cac-cacacacacaca'))
            assert limited[0]==200,limited
            patch_client=test_auth_support.client(base,'synthetic_patch_employee')
            before_patch=get(patch_client,'/api/board')
            old_version=before_patch['record_versions']['account:'+new['id']]
            patch_body=dict(id=new['id'],changes=dict(status='养号7天',card_stability='已稳定',password_state='已修改'))
            missing_patch=post(patch_client,'/api/account/patch',patch_body)
            assert missing_patch[0]==400 and missing_patch[1].get('code')=='REQUEST_ID_REQUIRED', missing_patch
            blank_patch=post(patch_client,'/api/account/patch',dict(patch_body,request_id=''))
            assert blank_patch[0]==400 and blank_patch[1].get('code')=='REQUEST_ID_REQUIRED', blank_patch
            assert next(row for row in get(patch_client,'/api/board')['accounts'] if row['id']==new['id'])['status']=='正常'
            valid_patch=post(patch_client,'/api/account/patch',dict(patch_body,request_id='ebebebeb-ebeb-4ebe-8ebe-ebebebebebeb'))
            assert valid_patch[0]==200,valid_patch
            saved=next(row for row in get(patch_client,'/api/board')['accounts'] if row['id']==new['id'])
            assert (saved['status'],saved['card_stability'],saved['password_state'])==('养号7天','已稳定','已修改')
            invalid_patch=post(patch_client,'/api/account/patch',dict(id=new['id'],changes=dict(status='停用',password_state='invalid-synthetic'),request_id='ecececec-ecec-4cec-8cec-ecececececec'))
            assert invalid_patch[0]==400,invalid_patch
            assert post(patch_client,'/api/account/patch',dict(id=new['id'],changes=dict(status='停用',name='夹带无权字段'),request_id='edededed-eded-4ded-8ded-edededededed'))[0]==403
            assert post(patch_client,'/api/account/patch',dict(id=new['id'],changes=dict(status='停用',work_count=99),request_id='efefefef-efef-4fef-8fef-efefefefefef'))[0]==400
            assert post(patch_client,'/api/account/patch',dict(id=new['id'],changes=dict(status='停用'),_expected_versions={'account:'+new['id']:old_version},request_id='f0f0f0f0-f0f0-4f0f-8f0f-f0f0f0f0f0f0'))[0]==409
            current=next(row for row in get(patch_client,'/api/board')['accounts'] if row['id']==new['id'])
            assert current['status']=='养号7天' and current['name']==new['name'] and current['password_state']=='已修改'
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute("INSERT INTO accounts(id,name,status) VALUES('account-null','合成空值保留','正常')")
                scope.assign_entity(raw_db,owner,'accounts','account-null',[office_a],newly_created=True);raw_db.commit()
            assert post(patch_client,'/api/account/patch',dict(id='account-null',changes={'status':'停用'},request_id='f3f3f3f3-f3f3-4f3f-8f3f-f3f3f3f3f3f3'))[0]==200
            with runtime.connect() as raw_db:
                raw=raw_db.execute("SELECT note,douyin_id,homepage,computer_id,slot FROM accounts WHERE id='account-null'").fetchone()
                assert all(raw[index] is None for index in range(5)),dict(raw)
            hidden_pair=post(employee_client,'/api/account/create',dict(create,name='禁止隐式越界配对',office_id=office_a,account_type='小号',registration_phone='19900000002',request_id='c8c8c8c8-c8c8-4c8c-8c8c-c8c8c8c8c8c8'))
            assert hidden_pair[0]==403,hidden_pair
            assert not any(row['name']=='禁止隐式越界配对' for row in get(owner_client,'/api/board')['accounts'])
            parent_body=dict(create,id=new['id'],account_type='大号',registration_phone='19900000006')
            missing_parent=post(employee_client,'/api/account',parent_body)
            assert missing_parent[0]==400 and missing_parent[1].get('code')=='REQUEST_ID_REQUIRED', missing_parent
            blank_parent=post(employee_client,'/api/account',dict(parent_body,request_id=''))
            assert blank_parent[0]==400 and blank_parent[1].get('code')=='REQUEST_ID_REQUIRED', blank_parent
            assert next(row for row in get(owner_client,'/api/board')['accounts'] if row['id']==new['id'])['registration_phone']!='19900000006'
            parent=post(employee_client,'/api/account',dict(parent_body,request_id='e6e6e6e6-e6e6-4e6e-8e6e-e6e6e6e6e6e6'))
            assert parent[0]==200,parent
            child=post(employee_client,'/api/account/create',dict(create,name='合成范围内配对小号',slot=3,office_id=office_a,account_type='小号',registration_phone='19900000006',request_id='c5c5c5c5-c5c5-4c5c-8c5c-c5c5c5c5c5c5'))
            assert child[0]==200,child
            assert next(row for row in get(employee_client,'/api/board')['accounts'] if row['name']=='合成范围内配对小号')['parent_account_id']==new['id']
            same=get(owner_client,'/api/board')
            same_key='account:'+new['id']
            same_version=same['record_versions'][same_key]
            with runtime.connect() as raw_db:
                history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(same_key,)).fetchone()[0]
                audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch' AND target=?",(new['id'],)).fetchone()[0]
            barrier=threading.Barrier(2, timeout=5)
            def race_same(client, note, request_id):
                barrier.wait()
                return post(client,'/api/account/patch',dict(id=new['id'],changes={'note':note},_expected_versions={same_key:same_version},request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                raced=[future.result(timeout=20) for future in (executor.submit(race_same,owner_client,'合成并发甲','f4f4f4f4-f4f4-4f4f-8f4f-f4f4f4f4f4f4'),executor.submit(race_same,employee_client,'合成并发乙','f5f5f5f5-f5f5-4f5f-8f5f-f5f5f5f5f5f5'))]
            assert sorted(item[0] for item in raced)==[200,409], raced
            winner=next(row for row in get(owner_client,'/api/board')['accounts'] if row['id']==new['id'])
            assert winner['note'] in ('合成并发甲','合成并发乙') and winner['name']=='合成范围新增' and winner['office_ids']==[office_a]
            with runtime.connect() as raw_db:
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                history_rows=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(same_key,)).fetchall()
                audit_rows=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/account/patch' AND target=? ORDER BY id",(new['id'],)).fetchall()
            winner_user='test_owner' if winner['note']=='合成并发甲' else 'synthetic_employee'
            loser_user='synthetic_employee' if winner_user=='test_owner' else 'test_owner'
            winner_name,loser_name=names[winner_user],names[loser_user]
            added=[row['actor_name'] for row in history_rows[history_before:]]
            audited=[row['actor_name'] for row in audit_rows[audit_before:]]
            assert len(history_rows)==history_before+1 and added==[winner_name], (winner['note'],winner_name,added)
            assert len(audit_rows)==audit_before+1 and audited==[winner_name] and loser_name not in audited, (winner_name,audited)
            child_row=next(row for row in get(owner_client,'/api/board')['accounts'] if row['name']=='合成范围内配对小号')
            fresh=get(owner_client,'/api/board')
            versions={key:fresh['record_versions'][key] for key in ('account:'+new['id'],'account:'+child_row['id'])}
            barrier_other=threading.Barrier(2, timeout=5)
            def race_other(client, ident, note, request_id):
                barrier_other.wait()
                key='account:'+ident
                return post(client,'/api/account/patch',dict(id=ident,changes={'note':note},_expected_versions={key:versions[key]},request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                other=[future.result(timeout=20) for future in (executor.submit(race_other,owner_client,new['id'],'合成不同记录甲','f9f9f9f9-f9f9-4f9f-8f9f-f9f9f9f9f9f9'),executor.submit(race_other,employee_client,child_row['id'],'合成不同记录乙','fafafafa-fafa-4faf-8faf-fafafafafafa'))]
            assert [item[0] for item in other]==[200,200], other
            after={row['id']:row for row in get(owner_client,'/api/board')['accounts']}
            assert after[new['id']]['note']=='合成不同记录甲' and after[child_row['id']]['note']=='合成不同记录乙'
            assert after[new['id']]['office_ids']==[office_a] and after[child_row['id']]['office_ids']==[office_a]
            missing_card=post(employee_client,'/api/phone-card',dict(phone='19900000003',office_ids=[office_a]))
            assert missing_card[0]==400 and missing_card[1].get('code')=='REQUEST_ID_REQUIRED', missing_card
            blank_card=post(employee_client,'/api/phone-card',dict(phone='19900000003',office_ids=[office_a],request_id=''))
            assert blank_card[0]==400 and blank_card[1].get('code')=='REQUEST_ID_REQUIRED', blank_card
            assert not any(row['phone']=='19900000003' for row in get(owner_client,'/api/board')['phone_cards']['cards'])
            card_created=post(employee_client,'/api/phone-card',dict(phone='19900000003',office_ids=[office_a],request_id='c9c9c9c9-c9c9-4c9c-8c9c-c9c9c9c9c9c9'))
            assert card_created[0]==200,card_created
            missing_device=post(employee_client,'/api/phone-device/create',dict(id='3',state='在用',office_ids=[office_a]))
            assert missing_device[0]==400 and missing_device[1].get('code')=='REQUEST_ID_REQUIRED', missing_device
            blank_device=post(employee_client,'/api/phone-device/create',dict(id='3',state='在用',office_ids=[office_a],request_id=''))
            assert blank_device[0]==400 and blank_device[1].get('code')=='REQUEST_ID_REQUIRED', blank_device
            assert not any(row['id']=='3' for row in get(owner_client,'/api/board')['phone_devices']['devices'])
            assert post(employee_client,'/api/phone-device/create',dict(id='3',state='在用',office_ids=[office_a],request_id='e4e4e4e4-e4e4-4e4e-8e4e-e4e4e4e4e4e4'))[0]==200
            assert post(employee_client,'/api/account/phone-device',dict(id=new['id'],phone_device_id='3',request_id='b6b6b6b6-b6b6-4b6b-8b6b-b6b6b6b6b6b6'))[0]==200
            assert post(employee_client,'/api/account/phone-device',dict(id=new['id'],phone_device_id='2'))[0]==403
            moved=post(employee_client,'/api/account',dict(create,id=new['id'],name='must-rollback',office_id=office_b,request_id='e7e7e7e7-e7e7-4e7e-8e7e-e7e7e7e7e7e7'))
            assert moved[0]==403,moved
            check=next(row for row in get(employee_client,'/api/board')['accounts'] if row['id']==new['id'])
            assert check['name']=='合成范围新增' and check['office_ids']==[office_a] and check['phone_device_id']=='3'
            assert post(employee_client,'/api/phone-device/create',dict(id='4',state='在用',office_ids=[office_a],request_id='d4d4d4d4-d4d4-4d4d-8d4d-d4d4d4d4d4d4'))[0]==200
            assert post(employee_client,'/api/phone-device/create',dict(id='5',state='在用',office_ids=[office_a],request_id='d9d9d9d9-d9d9-4d9d-8d9d-d9d9d9d9d9d9'))[0]==200
            phone_board=get(owner_client,'/api/board')
            phone_key='account:'+new['id']
            phone_versions={key:phone_board['record_versions'][key] for key in (phone_key,'device:3','device:4','device:5')}
            with runtime.connect() as raw_db:
                phone_history={entity:raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(entity,)).fetchone()[0] for entity in (phone_key,'device:3','device:4','device:5')}
            barrier_phone=threading.Barrier(2, timeout=5)
            def race_phone(client, device_id, request_id):
                barrier_phone.wait()
                keys=[phone_key,'device:3','device:'+device_id]
                return post(client,'/api/account/phone-device',dict(id=new['id'],phone_device_id=device_id,_expected_versions={key:phone_versions[key] for key in keys},request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                phone_raced=[future.result(timeout=20) for future in (executor.submit(race_phone,owner_client,'4','d5d5d5d5-d5d5-4d5d-8d5d-d5d5d5d5d5d5'),executor.submit(race_phone,employee_client,'5','d6d6d6d6-d6d6-4d6d-8d6d-d6d6d6d6d6d6'))]
            assert sorted(item[0] for item in phone_raced)==[200,409], phone_raced
            chosen=next(row for row in get(owner_client,'/api/board')['accounts'] if row['id']==new['id'])['phone_device_id']
            assert chosen in ('4','5')
            loser_device='5' if chosen=='4' else '4'
            after_phone=get(owner_client,'/api/board')['record_versions']
            assert after_phone['device:'+loser_device]==phone_versions['device:'+loser_device]
            assert after_phone[phone_key]!=phone_versions[phone_key] and after_phone['device:3']!=phone_versions['device:3']
            with runtime.connect() as raw_db:
                phone_after={entity:raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(entity,)).fetchone()[0] for entity in (phone_key,'device:3','device:4','device:5')}
            assert phone_after[phone_key]==phone_history[phone_key]+1
            assert phone_after['device:3']==phone_history['device:3']+1
            assert phone_after['device:'+loser_device]==phone_history['device:'+loser_device]
            with runtime.connect() as check_db:
                assert check_db.execute("SELECT workflow FROM nurture_records WHERE account_id='account-b'").fetchone()[0]=='运行'
                assert check_db.execute("SELECT count(*) FROM operations_history WHERE entity='account:account-b'").fetchone()[0]==0
                assert check_db.execute('SELECT scope_offices FROM operations_history WHERE entity=? ORDER BY id LIMIT 1',('account:'+new['id'],)).fetchone()[0]==json.dumps([office_a],ensure_ascii=False,separators=(',',':'))
            history=get(employee_client,'/api/workspace/history')
            assert history['entries'] and secret not in json.dumps(history,ensure_ascii=False)
            assert all(item['group_id']!='legacy' for item in history['entries'])
            try:get(employee_client,'/api/workspace/history?entity=account:account-b');raise AssertionError('foreign history accepted')
            except urllib.error.HTTPError as error:assert error.code==403
            assert secret in json.dumps(get(owner_client,'/api/workspace/history'),ensure_ascii=False)
            exported=post(employee_client,'/api/workspace/export',dict(kind='accounts',format='csv'))
            assert exported[0]==200,exported
            csv_text=base64.b64decode(exported[1]['content']).decode('utf-8-sig')
            assert '合成账号A' in csv_text and '合成范围新增' in csv_text
            exported_values={value for row in csv.reader(io.StringIO(csv_text)) for value in row}
            assert secret not in csv_text and 'account-b' not in exported_values and 'pc-b' not in exported_values
            assert post(employee_client,'/api/workspace/export',dict(kind='accounts',format='csv',ids=['account-a','account-b']))[0]==403
            for kind,own,foreign in [('cards','13800000001','13800000002'),('devices','合成共用手机',secret)]:
                exported=post(employee_client,'/api/workspace/export',dict(kind=kind,format='csv'))
                assert exported[0]==200,exported
                text=base64.b64decode(exported[1]['content']).decode('utf-8-sig')
                assert own in text and foreign not in text
            def preview_csv(text,**scope_fields):
                result=post(employee_client,'/api/workspace/import-preview',dict(kind='accounts',filename='synthetic.csv',content=base64.b64encode(text.encode('utf-8-sig')).decode(),**scope_fields))
                assert result[0]==200,result
                return result[1]
            content='抖音号,昵称\nsynthetic-import-one,合成导入新增\n'
            assert preview_csv(content)['rows'][0]['action']=='error'
            assert preview_csv(content,office_id=office_b)['rows'][0]['action']=='error'
            preview=preview_csv(content,office_id=office_a)
            assert preview['rows'][0]['action']=='create',preview
            applied=post(employee_client,'/api/workspace/import-apply',dict(preview_id=preview['preview_id'],rows=[2],request_id='b7b7b7b7-b7b7-4b7b-8b7b-b7b7b7b7b7b7'))
            assert applied[0]==200,applied
            imported=next(row for row in get(employee_client,'/api/board')['accounts'] if row['douyin_id']=='synthetic-import-one')
            assert imported['office_ids']==[office_a]
            partial_preview=preview_csv('抖音号,昵称,账号状态\nsynthetic-import-partial,合成部分导入,正常\nsynthetic-import-bad,合成错误导入,不是状态\n',office_id=office_a)
            assert [row['action'] for row in partial_preview['rows']]==['create','error'], [row.get('action') for row in partial_preview['rows']]
            partial_apply_body=dict(preview_id=partial_preview['preview_id'],rows=[2,3],request_id='5c5c5c5c-5c5c-45c5-85c5-5c5c5c5c5c5c',_expected_versions={})
            partial_apply=post(employee_client,'/api/workspace/import-apply',partial_apply_body)
            assert partial_apply[0]==400 and '不能导入错误行' in str(partial_apply[1].get('error','')), partial_apply[0]
            assert post(employee_client,'/api/workspace/import-apply',partial_apply_body)==partial_apply
            assert not any(row['douyin_id'] in ('synthetic-import-partial','synthetic-import-bad') for row in get(owner_client,'/api/board')['accounts'])
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT state FROM idempotency_records WHERE request_id=?',('5c5c5c5c-5c5c-45c5-85c5-5c5c5c5c5c5c',)).fetchone()[0]=='failed'
            idem_preview=preview_csv('抖音号,昵称\nsynthetic-import-idem,合成幂等导入\n',office_id=office_a)
            assert idem_preview['rows'][0]['action']=='create', idem_preview
            missing_import=post(employee_client,'/api/workspace/import-apply',dict(preview_id=idem_preview['preview_id'],rows=[2],_expected_versions={}))
            assert missing_import[0]==400 and missing_import[1].get('code')=='REQUEST_ID_REQUIRED', missing_import
            blank_import=post(employee_client,'/api/workspace/import-apply',dict(preview_id=idem_preview['preview_id'],rows=[2],request_id='',_expected_versions={}))
            assert blank_import[0]==400 and blank_import[1].get('code')=='REQUEST_ID_REQUIRED', blank_import
            assert not any(row['douyin_id']=='synthetic-import-idem' for row in get(owner_client,'/api/board')['accounts'])
            import_idem=dict(preview_id=idem_preview['preview_id'],rows=[2],request_id='99999999-9999-4999-8999-999999999999',_expected_versions={})
            import_first=post(employee_client,'/api/workspace/import-apply',import_idem)
            assert import_first[0]==200 and import_first[1].get('imported')==1, import_first
            import_again=post(employee_client,'/api/workspace/import-apply',import_idem)
            assert import_again[0]==200 and import_again[1].get('imported')==1, import_again
            imported_rows=[row for row in get(owner_client,'/api/board')['accounts'] if row['douyin_id']=='synthetic-import-idem']
            assert len(imported_rows)==1 and imported_rows[0]['office_ids']==[office_a]
            import_reused=post(employee_client,'/api/workspace/import-apply',dict(import_idem,rows=[99]))
            assert import_reused[0]==409 and import_reused[1].get('code')=='REQUEST_ID_REUSED', import_reused
            assert len([row for row in get(owner_client,'/api/board')['accounts'] if row['douyin_id']=='synthetic-import-idem'])==1
            bad_preview=preview_csv('抖音号,昵称\nsynthetic-import-idem-bad,合成幂等导入失败\n',office_id=office_a)
            bad_import=dict(preview_id=bad_preview['preview_id'],rows=[],request_id='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',_expected_versions={})
            bad_import_first=post(employee_client,'/api/workspace/import-apply',bad_import)
            assert bad_import_first[0]==400 and bad_import_first[1].get('error'), bad_import_first
            assert post(employee_client,'/api/workspace/import-apply',bad_import)==bad_import_first
            assert not any(row['douyin_id']=='synthetic-import-idem-bad' for row in get(owner_client,'/api/board')['accounts'])
            joined_create=post(employee_client,'/api/account/create',dict(name='合成养号加入',computer_id='pc-a',slot=7,status='正常',office_id=office_a,request_id='c6c6c6c6-c6c6-4c6c-8c6c-c6c6c6c6c6c6'))
            assert joined_create[0]==200, joined_create
            joined=next(row for row in get(employee_client,'/api/board')['accounts'] if row['name']=='合成养号加入')
            before_join=get(employee_client,'/api/board')
            join_versions={'account:'+joined['id']:before_join['record_versions']['account:'+joined['id']],'nurture:'+joined['id']:before_join['record_versions'].get('nurture:'+joined['id'])}
            missing_join=post(employee_client,'/api/workspace/nurture/join',dict(id=joined['id'],_expected_versions=join_versions))
            assert missing_join[0]==400 and missing_join[1].get('code')=='REQUEST_ID_REQUIRED', missing_join
            blank_join=post(employee_client,'/api/workspace/nurture/join',dict(id=joined['id'],request_id='',_expected_versions=join_versions))
            assert blank_join[0]==400 and blank_join[1].get('code')=='REQUEST_ID_REQUIRED', blank_join
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]==0
            join_idem=dict(id=joined['id'],request_id='dddddddd-dddd-4ddd-8ddd-dddddddddddd',_expected_versions=join_versions)
            join_first=post(employee_client,'/api/workspace/nurture/join',join_idem)
            assert join_first[0]==200 and join_first[1].get('ok') is True, join_first
            join_again=post(employee_client,'/api/workspace/nurture/join',join_idem)
            assert join_again[0]==200 and join_again[1].get('ok') is True, join_again
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]==1
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]=='待开始'
                account_a_before=raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',('account-a',)).fetchone()[0]
            join_reused=post(employee_client,'/api/workspace/nurture/join',dict(join_idem,id='account-a'))
            assert join_reused[0]==409 and join_reused[1].get('code')=='REQUEST_ID_REUSED', join_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]==1
                assert raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',('account-a',)).fetchone()[0]==account_a_before
            after_join=get(employee_client,'/api/board')
            bad_join=dict(id=joined['id'],request_id='eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',_expected_versions={'account:'+joined['id']:after_join['record_versions']['account:'+joined['id']],'nurture:'+joined['id']:after_join['record_versions']['nurture:'+joined['id']]})
            bad_join_first=post(employee_client,'/api/workspace/nurture/join',bad_join)
            assert bad_join_first[0]==400 and '已在养号面板' in bad_join_first[1].get('error',''), bad_join_first
            assert post(employee_client,'/api/workspace/nurture/join',bad_join)==bad_join_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]==1
            race_join_create=post(employee_client,'/api/account/create',dict(name='合成养号并发加入',computer_id='pc-a',slot=6,status='正常',office_id=office_a,request_id='ce11ce11-ce11-4ce1-8ce1-ce11ce11ce11'))
            assert race_join_create[0]==200, race_join_create
            race_joined=next(row for row in get(owner_client,'/api/board')['accounts'] if row['name']=='合成养号并发加入')
            race_join_account='account:'+race_joined['id']
            race_join_nurture='nurture:'+race_joined['id']
            race_join_board=get(owner_client,'/api/board')
            race_join_versions={race_join_account:race_join_board['record_versions'][race_join_account],race_join_nurture:race_join_board['record_versions'].get(race_join_nurture)}
            assert race_join_versions[race_join_nurture] is None
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()[0]==0
                race_join_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_nurture,)).fetchone()[0]
                race_join_account_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_account,)).fetchone()[0]
                joined_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+joined['id'],)).fetchone()[0]
                race_join_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/join' AND target=''").fetchone()[0]
            barrier_join=threading.Barrier(2, timeout=5)
            def race_join(client, request_id):
                barrier_join.wait()
                return post(client,'/api/workspace/nurture/join',dict(id=race_joined['id'],_expected_versions=dict(race_join_versions),request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                join_owner,join_employee=[future.result(timeout=20) for future in (executor.submit(race_join,owner_client,'be11be11-be11-4be1-8be1-be11be11be11'),executor.submit(race_join,employee_client,'be22be22-be22-4be2-8be2-be22be22be22'))]
            assert sorted(item[0] for item in (join_owner,join_employee))==[200,409], (join_owner,join_employee)
            assert next(item for item in (join_owner,join_employee) if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()[0]==1
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()[0]=='待开始'
                assert raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]==1
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]=='待开始'
                assert raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',('account-a',)).fetchone()[0]==account_a_before
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                race_join_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_nurture,)).fetchall()
                race_join_account_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_account,)).fetchall()
                joined_history_after=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+joined['id'],)).fetchone()[0]
                race_join_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/workspace/nurture/join' AND target='' ORDER BY id").fetchall()
            join_winner='test_owner' if join_owner[0]==200 else 'synthetic_employee'
            join_loser='synthetic_employee' if join_winner=='test_owner' else 'test_owner'
            join_added=[row['actor_name'] for row in race_join_history[race_join_history_before:]]
            join_account_added=[row['actor_name'] for row in race_join_account_history[race_join_account_history_before:]]
            join_audited=[row['actor_name'] for row in race_join_audit[race_join_audit_before:]]
            assert join_added==[names[join_winner]] and names[join_loser] not in join_added, join_added
            assert all(name==names[join_winner] for name in join_account_added) and names[join_loser] not in join_account_added, join_account_added
            assert join_audited==[names[join_winner]] and names[join_loser] not in join_audited, join_audited
            assert joined_history_after==joined_history_before
            after_join_race=get(owner_client,'/api/board')
            assert after_join_race['record_versions'][race_join_nurture] not in (None,race_join_versions[race_join_nurture])
            bad_start=dict(id=joined['id'],started=day,request_id='abababab-abab-4aba-8aba-abababababab',_expected_versions={'account:'+joined['id']:after_join['record_versions']['account:'+joined['id']],'nurture:'+joined['id']:after_join['record_versions']['nurture:'+joined['id']]})
            bad_start_first=post(employee_client,'/api/workspace/nurture/start',bad_start)
            assert bad_start_first[0]==400 and '请先确认' in bad_start_first[1].get('error',''), bad_start_first
            assert post(employee_client,'/api/workspace/nurture/start',bad_start)==bad_start_first
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute("UPDATE nurture_records SET verification_state='无需核对，可使用' WHERE account_id=?",(joined['id'],))
                raw_db.commit()
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]=='待开始'
            ready_join=get(employee_client,'/api/board')
            start_versions={'account:'+joined['id']:ready_join['record_versions']['account:'+joined['id']],'nurture:'+joined['id']:ready_join['record_versions']['nurture:'+joined['id']]}
            missing_start=post(employee_client,'/api/workspace/nurture/start',dict(id=joined['id'],started=day,_expected_versions=start_versions))
            assert missing_start[0]==400 and missing_start[1].get('code')=='REQUEST_ID_REQUIRED', missing_start
            blank_start=post(employee_client,'/api/workspace/nurture/start',dict(id=joined['id'],started=day,request_id='',_expected_versions=start_versions))
            assert blank_start[0]==400 and blank_start[1].get('code')=='REQUEST_ID_REQUIRED', blank_start
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]=='待开始'
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(joined['id'],)).fetchone()[0]!='养号7天'
            start_idem=dict(id=joined['id'],started=day,request_id='ffffffff-ffff-4fff-8fff-ffffffffffff',_expected_versions=start_versions)
            start_first=post(employee_client,'/api/workspace/nurture/start',start_idem)
            assert start_first[0]==200 and start_first[1].get('ok') is True, start_first
            start_again=post(employee_client,'/api/workspace/nurture/start',start_idem)
            assert start_again[0]==200 and start_again[1].get('ok') is True, start_again
            with runtime.connect() as raw_db:
                started_row=raw_db.execute('SELECT workflow,started_on FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()
                assert (started_row[0],started_row[1])==('运行',day)
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(joined['id'],)).fetchone()[0]=='养号7天'
            start_reused=post(employee_client,'/api/workspace/nurture/start',dict(start_idem,started='2020-01-01'))
            assert start_reused[0]==409 and start_reused[1].get('code')=='REQUEST_ID_REUSED', start_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT started_on FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]==day
            assert day!='2026-10-22'
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute("UPDATE nurture_records SET verification_state='无需核对，可使用' WHERE account_id=?",(race_joined['id'],))
                raw_db.commit()
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()[0]=='待开始'
            race_start_board=get(owner_client,'/api/board')
            race_start_versions={race_join_account:race_start_board['record_versions'][race_join_account],race_join_nurture:race_start_board['record_versions'][race_join_nurture]}
            with runtime.connect() as raw_db:
                race_start_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_nurture,)).fetchone()[0]
                race_start_account_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_account,)).fetchone()[0]
                joined_start_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+joined['id'],)).fetchone()[0]
                race_start_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/start' AND target=''").fetchone()[0]
            barrier_start=threading.Barrier(2, timeout=5)
            def race_start(client, started, request_id):
                barrier_start.wait()
                return post(client,'/api/workspace/nurture/start',dict(id=race_joined['id'],started=started,_expected_versions=dict(race_start_versions),request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                start_owner,start_employee=[future.result(timeout=20) for future in (executor.submit(race_start,owner_client,'2026-10-22','bf11bf11-bf11-4bf1-8bf1-bf11bf11bf11'),executor.submit(race_start,employee_client,day,'bf22bf22-bf22-4bf2-8bf2-bf22bf22bf22'))]
            assert sorted(item[0] for item in (start_owner,start_employee))==[200,409], (start_owner,start_employee)
            assert next(item for item in (start_owner,start_employee) if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            with runtime.connect() as raw_db:
                raced_start=raw_db.execute('SELECT workflow,started_on FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(race_joined['id'],)).fetchone()[0]=='养号7天'
                joined_still=raw_db.execute('SELECT workflow,started_on FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()
                assert (joined_still[0],joined_still[1])==('运行',day)
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(joined['id'],)).fetchone()[0]=='养号7天'
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                race_start_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_nurture,)).fetchall()
                race_start_account_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_account,)).fetchall()
                joined_start_history_after=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+joined['id'],)).fetchone()[0]
                race_start_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/workspace/nurture/start' AND target='' ORDER BY id").fetchall()
            assert raced_start[0]=='运行' and raced_start[1] in ('2026-10-22',day)
            start_winner='test_owner' if raced_start[1]=='2026-10-22' else 'synthetic_employee'
            start_loser='synthetic_employee' if start_winner=='test_owner' else 'test_owner'
            assert (start_owner[0]==200)==(start_winner=='test_owner')
            start_added=[row['actor_name'] for row in race_start_history[race_start_history_before:]]
            start_account_added=[row['actor_name'] for row in race_start_account_history[race_start_account_history_before:]]
            start_audited=[row['actor_name'] for row in race_start_audit[race_start_audit_before:]]
            assert start_added==[names[start_winner]] and names[start_loser] not in start_added, start_added
            assert all(name==names[start_winner] for name in start_account_added) and names[start_loser] not in start_account_added, start_account_added
            assert start_audited==[names[start_winner]] and names[start_loser] not in start_audited, start_audited
            assert joined_start_history_after==joined_start_history_before
            after_start_race=get(owner_client,'/api/board')
            assert after_start_race['record_versions'][race_join_nurture]!=race_start_versions[race_join_nurture]
            after_start=get(employee_client,'/api/board')
            verify_versions={'account:'+joined['id']:after_start['record_versions']['account:'+joined['id']],'nurture:'+joined['id']:after_start['record_versions']['nurture:'+joined['id']]}
            verify_body=dict(id=joined['id'],verification_state='无需核对，可使用',verification_owner='合成核对人',verification_note='合成核对备注',_expected_versions=verify_versions)
            missing_verify=post(employee_client,'/api/workspace/nurture/verification',verify_body)
            assert missing_verify[0]==400 and missing_verify[1].get('code')=='REQUEST_ID_REQUIRED', missing_verify
            blank_verify=post(employee_client,'/api/workspace/nurture/verification',dict(verify_body,request_id=''))
            assert blank_verify[0]==400 and blank_verify[1].get('code')=='REQUEST_ID_REQUIRED', blank_verify
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT verification_note FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]!='合成核对备注'
            verify_idem=dict(verify_body,request_id='10101010-1010-4101-8101-101010101010')
            verify_first=post(employee_client,'/api/workspace/nurture/verification',verify_idem)
            assert verify_first[0]==200 and verify_first[1].get('ok') is True, verify_first
            verify_again=post(employee_client,'/api/workspace/nurture/verification',verify_idem)
            assert verify_again[0]==200 and verify_again[1].get('ok') is True, verify_again
            with runtime.connect() as raw_db:
                saved_verify=raw_db.execute('SELECT verification_owner,verification_note,workflow FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()
                assert (saved_verify[0],saved_verify[1],saved_verify[2])==('合成核对人','合成核对备注','运行')
            verify_reused=post(employee_client,'/api/workspace/nurture/verification',dict(verify_idem,verification_note='合成核对被拒绝'))
            assert verify_reused[0]==409 and verify_reused[1].get('code')=='REQUEST_ID_REUSED', verify_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT verification_note FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]=='合成核对备注'
            after_verify=get(employee_client,'/api/board')
            bad_verify=dict(id=joined['id'],verification_state='不是核对情况',verification_owner='',verification_note='',request_id='20202020-2020-4202-8202-202020202020',_expected_versions={'account:'+joined['id']:after_verify['record_versions']['account:'+joined['id']],'nurture:'+joined['id']:after_verify['record_versions']['nurture:'+joined['id']]})
            bad_verify_first=post(employee_client,'/api/workspace/nurture/verification',bad_verify)
            assert bad_verify_first[0]==400 and bad_verify_first[1].get('error'), bad_verify_first
            assert post(employee_client,'/api/workspace/nurture/verification',bad_verify)==bad_verify_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT verification_state,verification_note FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]=='无需核对，可使用'
            race_verify_board=get(owner_client,'/api/board')
            race_verify_versions={race_join_account:race_verify_board['record_versions'][race_join_account],race_join_nurture:race_verify_board['record_versions'][race_join_nurture]}
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()[0]=='运行'
                race_verify_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_nurture,)).fetchone()[0]
                joined_verify_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+joined['id'],)).fetchone()[0]
                race_verify_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/verification' AND target=''").fetchone()[0]
            barrier_verify=threading.Barrier(2, timeout=5)
            def race_verify(client, note, request_id):
                barrier_verify.wait()
                return post(client,'/api/workspace/nurture/verification',dict(id=race_joined['id'],verification_state='无需核对，可使用',verification_owner='合成核对人',verification_note=note,_expected_versions=dict(race_verify_versions),request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                verify_owner,verify_employee=[future.result(timeout=20) for future in (executor.submit(race_verify,owner_client,'合成核对并发甲','b011b011-b011-4b01-8b01-b011b011b011'),executor.submit(race_verify,employee_client,'合成核对并发乙','b022b022-b022-4b02-8b02-b022b022b022'))]
            assert sorted(item[0] for item in (verify_owner,verify_employee))==[200,409], (verify_owner,verify_employee)
            assert next(item for item in (verify_owner,verify_employee) if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            with runtime.connect() as raw_db:
                raced_verify=raw_db.execute('SELECT verification_note,workflow FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()
                joined_verify=raw_db.execute('SELECT verification_note,workflow FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                race_verify_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_nurture,)).fetchall()
                joined_verify_history_after=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+joined['id'],)).fetchone()[0]
                race_verify_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/workspace/nurture/verification' AND target='' ORDER BY id").fetchall()
            assert raced_verify[0] in ('合成核对并发甲','合成核对并发乙') and raced_verify[1]=='运行'
            assert (joined_verify[0],joined_verify[1])==('合成核对备注','运行')
            verify_winner='test_owner' if raced_verify[0]=='合成核对并发甲' else 'synthetic_employee'
            verify_loser='synthetic_employee' if verify_winner=='test_owner' else 'test_owner'
            assert (verify_owner[0]==200)==(verify_winner=='test_owner')
            verify_added=[row['actor_name'] for row in race_verify_history[race_verify_history_before:]]
            verify_audited=[row['actor_name'] for row in race_verify_audit[race_verify_audit_before:]]
            assert verify_added==[names[verify_winner]] and names[verify_loser] not in verify_added, verify_added
            assert verify_audited==[names[verify_winner]] and names[verify_loser] not in verify_audited, verify_audited
            assert joined_verify_history_after==joined_verify_history_before
            after_verify_race=get(owner_client,'/api/board')
            assert after_verify_race['record_versions'][race_join_nurture]!=race_verify_versions[race_join_nurture]
            pause_versions={'account:'+joined['id']:after_verify['record_versions']['account:'+joined['id']],'nurture:'+joined['id']:after_verify['record_versions']['nurture:'+joined['id']]}
            missing_pause=post(employee_client,'/api/workspace/nurture/pause',dict(id=joined['id'],_expected_versions=pause_versions))
            assert missing_pause[0]==400 and missing_pause[1].get('code')=='REQUEST_ID_REQUIRED', missing_pause
            blank_pause=post(employee_client,'/api/workspace/nurture/pause',dict(id=joined['id'],request_id='',_expected_versions=pause_versions))
            assert blank_pause[0]==400 and blank_pause[1].get('code')=='REQUEST_ID_REQUIRED', blank_pause
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]=='运行'
            pause_idem=dict(id=joined['id'],request_id='30303030-3030-4303-8303-303030303030',_expected_versions=pause_versions)
            pause_first=post(employee_client,'/api/workspace/nurture/pause',pause_idem)
            assert pause_first[0]==200 and pause_first[1].get('ok') is True, pause_first
            pause_again=post(employee_client,'/api/workspace/nurture/pause',pause_idem)
            assert pause_again[0]==200 and pause_again[1].get('ok') is True, pause_again
            with runtime.connect() as raw_db:
                paused=raw_db.execute('SELECT workflow,paused_on FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()
                assert (paused[0],paused[1])==('暂停',day)
                account_a_pause=raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',('account-a',)).fetchone()[0]
            pause_reused=post(employee_client,'/api/workspace/nurture/pause',dict(pause_idem,id='account-a'))
            assert pause_reused[0]==409 and pause_reused[1].get('code')=='REQUEST_ID_REUSED', pause_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]=='暂停'
                assert raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',('account-a',)).fetchone()[0]==account_a_pause
            after_pause=get(employee_client,'/api/board')
            bad_pause=dict(id=joined['id'],request_id='40404040-4040-4404-8404-404040404040',_expected_versions={'account:'+joined['id']:after_pause['record_versions']['account:'+joined['id']],'nurture:'+joined['id']:after_pause['record_versions']['nurture:'+joined['id']]})
            bad_pause_first=post(employee_client,'/api/workspace/nurture/pause',bad_pause)
            assert bad_pause_first[0]==400 and '只有进行中的养号可以暂停' in bad_pause_first[1].get('error',''), bad_pause_first
            assert post(employee_client,'/api/workspace/nurture/pause',bad_pause)==bad_pause_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]=='暂停'
            race_pause_board=get(owner_client,'/api/board')
            race_pause_versions={race_join_account:race_pause_board['record_versions'][race_join_account],race_join_nurture:race_pause_board['record_versions'][race_join_nurture]}
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()[0]=='运行'
                race_pause_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_nurture,)).fetchone()[0]
                race_pause_account_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_account,)).fetchone()[0]
                joined_pause_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+joined['id'],)).fetchone()[0]
                race_pause_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/pause' AND target=''").fetchone()[0]
            barrier_pause=threading.Barrier(2, timeout=5)
            def race_pause(client, request_id):
                barrier_pause.wait()
                return post(client,'/api/workspace/nurture/pause',dict(id=race_joined['id'],_expected_versions=dict(race_pause_versions),request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                pause_owner,pause_employee=[future.result(timeout=20) for future in (executor.submit(race_pause,owner_client,'b311b311-b311-4b31-8b31-b311b311b311'),executor.submit(race_pause,employee_client,'b322b322-b322-4b32-8b32-b322b322b322'))]
            assert sorted(item[0] for item in (pause_owner,pause_employee))==[200,409], (pause_owner,pause_employee)
            assert next(item for item in (pause_owner,pause_employee) if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            with runtime.connect() as raw_db:
                raced_pause=raw_db.execute('SELECT workflow,paused_on FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]=='暂停'
                assert raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',('account-a',)).fetchone()[0]==account_a_pause
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                race_pause_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_nurture,)).fetchall()
                race_pause_account_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_account,)).fetchall()
                joined_pause_history_after=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+joined['id'],)).fetchone()[0]
                race_pause_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/workspace/nurture/pause' AND target='' ORDER BY id").fetchall()
            assert (raced_pause[0],raced_pause[1])==('暂停',day)
            pause_winner='test_owner' if pause_owner[0]==200 else 'synthetic_employee'
            pause_loser='synthetic_employee' if pause_winner=='test_owner' else 'test_owner'
            pause_added=[row['actor_name'] for row in race_pause_history[race_pause_history_before:]]
            pause_account_added=[row['actor_name'] for row in race_pause_account_history[race_pause_account_history_before:]]
            pause_audited=[row['actor_name'] for row in race_pause_audit[race_pause_audit_before:]]
            assert pause_added==[names[pause_winner]] and names[pause_loser] not in pause_added, pause_added
            assert all(name==names[pause_winner] for name in pause_account_added) and names[pause_loser] not in pause_account_added, pause_account_added
            assert pause_audited==[names[pause_winner]] and names[pause_loser] not in pause_audited, pause_audited
            assert joined_pause_history_after==joined_pause_history_before
            after_pause_race=get(owner_client,'/api/board')
            assert after_pause_race['record_versions'][race_join_nurture]!=race_pause_versions[race_join_nurture]
            ready_resume=get(employee_client,'/api/board')
            resume_versions={'account:'+joined['id']:ready_resume['record_versions']['account:'+joined['id']],'nurture:'+joined['id']:ready_resume['record_versions']['nurture:'+joined['id']]}
            missing_resume=post(employee_client,'/api/workspace/nurture/resume',dict(id=joined['id'],_expected_versions=resume_versions))
            assert missing_resume[0]==400 and missing_resume[1].get('code')=='REQUEST_ID_REQUIRED', missing_resume
            blank_resume=post(employee_client,'/api/workspace/nurture/resume',dict(id=joined['id'],request_id='',_expected_versions=resume_versions))
            assert blank_resume[0]==400 and blank_resume[1].get('code')=='REQUEST_ID_REQUIRED', blank_resume
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]=='暂停'
            resume_idem=dict(id=joined['id'],request_id='50505050-5050-4505-8505-505050505050',_expected_versions=resume_versions)
            resume_first=post(employee_client,'/api/workspace/nurture/resume',resume_idem)
            assert resume_first[0]==200 and resume_first[1].get('ok') is True, resume_first
            resume_again=post(employee_client,'/api/workspace/nurture/resume',resume_idem)
            assert resume_again[0]==200 and resume_again[1].get('ok') is True, resume_again
            with runtime.connect() as raw_db:
                resumed=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()
                assert (resumed[0],resumed[1])==('运行',day)
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(joined['id'],)).fetchone()[0]=='养号7天'
                account_a_resume=raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',('account-a',)).fetchone()[0]
            resume_reused=post(employee_client,'/api/workspace/nurture/resume',dict(resume_idem,id='account-a'))
            assert resume_reused[0]==409 and resume_reused[1].get('code')=='REQUEST_ID_REUSED', resume_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]=='运行'
                assert raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',('account-a',)).fetchone()[0]==account_a_resume
            race_resume_board=get(owner_client,'/api/board')
            race_resume_versions={race_join_account:race_resume_board['record_versions'][race_join_account],race_join_nurture:race_resume_board['record_versions'][race_join_nurture]}
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()[0]=='暂停'
                race_resume_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_nurture,)).fetchone()[0]
                race_resume_account_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_account,)).fetchone()[0]
                joined_resume_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+joined['id'],)).fetchone()[0]
                race_resume_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/resume' AND target=''").fetchone()[0]
            barrier_resume=threading.Barrier(2, timeout=5)
            def race_resume(client, request_id):
                barrier_resume.wait()
                return post(client,'/api/workspace/nurture/resume',dict(id=race_joined['id'],_expected_versions=dict(race_resume_versions),request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                resume_owner,resume_employee=[future.result(timeout=20) for future in (executor.submit(race_resume,owner_client,'b411b411-b411-4b41-8b41-b411b411b411'),executor.submit(race_resume,employee_client,'b422b422-b422-4b42-8b42-b422b422b422'))]
            assert sorted(item[0] for item in (resume_owner,resume_employee))==[200,409], (resume_owner,resume_employee)
            assert next(item for item in (resume_owner,resume_employee) if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            with runtime.connect() as raw_db:
                raced_resume=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(race_joined['id'],)).fetchone()[0]=='养号7天'
                joined_still=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()
                assert (joined_still[0],joined_still[1])==('运行',day)
                assert raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',('account-a',)).fetchone()[0]==account_a_resume
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                race_resume_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_nurture,)).fetchall()
                race_resume_account_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_account,)).fetchall()
                joined_resume_history_after=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+joined['id'],)).fetchone()[0]
                race_resume_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/workspace/nurture/resume' AND target='' ORDER BY id").fetchall()
            assert (raced_resume[0],raced_resume[1])==('运行',day)
            resume_winner='test_owner' if resume_owner[0]==200 else 'synthetic_employee'
            resume_loser='synthetic_employee' if resume_winner=='test_owner' else 'test_owner'
            resume_added=[row['actor_name'] for row in race_resume_history[race_resume_history_before:]]
            resume_account_added=[row['actor_name'] for row in race_resume_account_history[race_resume_account_history_before:]]
            resume_audited=[row['actor_name'] for row in race_resume_audit[race_resume_audit_before:]]
            assert resume_added==[names[resume_winner]] and names[resume_loser] not in resume_added, resume_added
            assert all(name==names[resume_winner] for name in resume_account_added) and names[resume_loser] not in resume_account_added, resume_account_added
            assert resume_audited==[names[resume_winner]] and names[resume_loser] not in resume_audited, resume_audited
            assert joined_resume_history_after==joined_resume_history_before
            after_resume_race=get(owner_client,'/api/board')
            assert after_resume_race['record_versions'][race_join_nurture]!=race_resume_versions[race_join_nurture]
            after_resume=get(employee_client,'/api/board')
            bad_resume=dict(id=joined['id'],request_id='60606060-6060-4606-8606-606060606060',_expected_versions={'account:'+joined['id']:after_resume['record_versions']['account:'+joined['id']],'nurture:'+joined['id']:after_resume['record_versions']['nurture:'+joined['id']]})
            bad_resume_first=post(employee_client,'/api/workspace/nurture/resume',bad_resume)
            assert bad_resume_first[0]==400 and '只有暂停的养号可以继续' in bad_resume_first[1].get('error',''), bad_resume_first
            assert post(employee_client,'/api/workspace/nurture/resume',bad_resume)==bad_resume_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT active_since FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]==day
            early_finish=dict(id=joined['id'],request_id='71717171-7171-4717-8717-717171717171',_expected_versions={'account:'+joined['id']:after_resume['record_versions']['account:'+joined['id']],'nurture:'+joined['id']:after_resume['record_versions']['nurture:'+joined['id']]})
            early_finish_first=post(employee_client,'/api/workspace/nurture/finish',early_finish)
            assert early_finish_first[0]==400 and '实际养号尚未满7天' in early_finish_first[1].get('error',''), early_finish_first
            assert post(employee_client,'/api/workspace/nurture/finish',early_finish)==early_finish_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT workflow,completed_on FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]=='运行'
                raw_db.begin_write();raw_db.execute('UPDATE nurture_records SET banked_days=7 WHERE account_id=?',(joined['id'],));raw_db.commit()
            ready_finish=get(employee_client,'/api/board')
            finish_versions={'account:'+joined['id']:ready_finish['record_versions']['account:'+joined['id']],'nurture:'+joined['id']:ready_finish['record_versions']['nurture:'+joined['id']]}
            missing_finish=post(employee_client,'/api/workspace/nurture/finish',dict(id=joined['id'],_expected_versions=finish_versions))
            assert missing_finish[0]==400 and missing_finish[1].get('code')=='REQUEST_ID_REQUIRED', missing_finish
            blank_finish=post(employee_client,'/api/workspace/nurture/finish',dict(id=joined['id'],request_id='',_expected_versions=finish_versions))
            assert blank_finish[0]==400 and blank_finish[1].get('code')=='REQUEST_ID_REQUIRED', blank_finish
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]=='运行'
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(joined['id'],)).fetchone()[0]=='养号7天'
            finish_idem=dict(id=joined['id'],request_id='72727272-7272-4727-8727-727272727272',_expected_versions=finish_versions)
            finish_first=post(employee_client,'/api/workspace/nurture/finish',finish_idem)
            assert finish_first[0]==200 and finish_first[1].get('ok') is True, finish_first
            finish_again=post(employee_client,'/api/workspace/nurture/finish',finish_idem)
            assert finish_again[0]==200 and finish_again[1].get('ok') is True, finish_again
            with runtime.connect() as raw_db:
                finished=raw_db.execute('SELECT workflow,completed_on FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()
                assert (finished[0],finished[1])==('已结束',day)
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(joined['id'],)).fetchone()[0]=='正常'
                account_a_finish=raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',('account-a',)).fetchone()[0]
            finish_reused=post(employee_client,'/api/workspace/nurture/finish',dict(finish_idem,id='account-a'))
            assert finish_reused[0]==409 and finish_reused[1].get('code')=='REQUEST_ID_REUSED', finish_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT completed_on FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]==day
                assert raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',('account-a',)).fetchone()[0]==account_a_finish
            after_finish=get(employee_client,'/api/board')
            bad_finish=dict(id=joined['id'],request_id='73737373-7373-4737-8737-737373737373',_expected_versions={'account:'+joined['id']:after_finish['record_versions']['account:'+joined['id']],'nurture:'+joined['id']:after_finish['record_versions']['nurture:'+joined['id']]})
            bad_finish_first=post(employee_client,'/api/workspace/nurture/finish',bad_finish)
            assert bad_finish_first[0]==400 and '该账号没有进行中的养号记录' in bad_finish_first[1].get('error',''), bad_finish_first
            assert post(employee_client,'/api/workspace/nurture/finish',bad_finish)==bad_finish_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]=='已结束'
            with runtime.connect() as raw_db:
                raw_db.begin_write();raw_db.execute('UPDATE nurture_records SET banked_days=7 WHERE account_id=?',(race_joined['id'],));raw_db.commit()
            race_finish_board=get(owner_client,'/api/board')
            race_finish_versions={race_join_account:race_finish_board['record_versions'][race_join_account],race_join_nurture:race_finish_board['record_versions'][race_join_nurture]}
            with runtime.connect() as raw_db:
                race_ready=raw_db.execute('SELECT workflow,verification_state,banked_days FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()
                assert (race_ready[0],race_ready[1],race_ready[2])==('运行','无需核对，可使用',7)
                joined_done=raw_db.execute('SELECT workflow,completed_on FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()
                assert (joined_done[0],joined_done[1])==('已结束',day)
                race_finish_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_nurture,)).fetchone()[0]
                race_finish_account_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_account,)).fetchone()[0]
                joined_finish_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+joined['id'],)).fetchone()[0]
                race_finish_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/finish' AND target=''").fetchone()[0]
            barrier_finish=threading.Barrier(2, timeout=5)
            def race_finish(client, request_id):
                barrier_finish.wait()
                return post(client,'/api/workspace/nurture/finish',dict(id=race_joined['id'],_expected_versions=dict(race_finish_versions),request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                finish_owner,finish_employee=[future.result(timeout=20) for future in (executor.submit(race_finish,owner_client,'b511b511-b511-4b51-8b51-b511b511b511'),executor.submit(race_finish,employee_client,'b522b522-b522-4b52-8b52-b522b522b522'))]
            assert sorted(item[0] for item in (finish_owner,finish_employee))==[200,409], (finish_owner,finish_employee)
            assert next(item for item in (finish_owner,finish_employee) if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            with runtime.connect() as raw_db:
                raced_finish=raw_db.execute('SELECT workflow,completed_on FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(race_joined['id'],)).fetchone()[0]=='正常'
                joined_after=raw_db.execute('SELECT workflow,completed_on FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()
                assert (joined_after[0],joined_after[1])==('已结束',day)
                assert raw_db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?',('account-a',)).fetchone()[0]==account_a_finish
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                race_finish_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_nurture,)).fetchall()
                race_finish_account_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_account,)).fetchall()
                joined_finish_history_after=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+joined['id'],)).fetchone()[0]
                race_finish_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/workspace/nurture/finish' AND target='' ORDER BY id").fetchall()
            assert (raced_finish[0],raced_finish[1])==('已结束',day)
            finish_winner='test_owner' if finish_owner[0]==200 else 'synthetic_employee'
            finish_loser='synthetic_employee' if finish_winner=='test_owner' else 'test_owner'
            finish_added=[row['actor_name'] for row in race_finish_history[race_finish_history_before:]]
            finish_account_added=[row['actor_name'] for row in race_finish_account_history[race_finish_account_history_before:]]
            finish_audited=[row['actor_name'] for row in race_finish_audit[race_finish_audit_before:]]
            assert finish_added==[names[finish_winner]] and names[finish_loser] not in finish_added, finish_added
            assert finish_account_added==[names[finish_winner]] and names[finish_loser] not in finish_account_added, finish_account_added
            assert finish_audited==[names[finish_winner]] and names[finish_loser] not in finish_audited, finish_audited
            assert joined_finish_history_after==joined_finish_history_before
            after_finish_race=get(owner_client,'/api/board')
            assert after_finish_race['record_versions'][race_join_nurture]!=race_finish_versions[race_join_nurture]
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute("INSERT INTO nurture_records(account_id,started_on,workflow,verification_state,active_since) VALUES(?,?,'运行','无需核对，可使用',?) ON CONFLICT(account_id) DO UPDATE SET started_on=excluded.started_on,workflow=excluded.workflow,verification_state=excluded.verification_state,active_since=excluded.active_since",(new['id'],day,day))
                raw_db.commit()
            fresh=get(employee_client,'/api/board')
            image=base64.b64encode(png()).decode()
            upload_versions={'account:'+new['id']:fresh['record_versions']['account:'+new['id']],'nurture:'+new['id']:fresh['record_versions']['nurture:'+new['id']]}
            missing_upload=post(employee_client,'/api/workspace/nurture/upload',dict(id=new['id'],day=day,content=image,_expected_versions=upload_versions))
            assert missing_upload[0]==400 and missing_upload[1].get('code')=='REQUEST_ID_REQUIRED' and image not in json.dumps(missing_upload[1],ensure_ascii=False), missing_upload[0]
            blank_upload=post(employee_client,'/api/workspace/nurture/upload',dict(id=new['id'],day=day,content=image,request_id='',_expected_versions=upload_versions))
            assert blank_upload[0]==400 and blank_upload[1].get('code')=='REQUEST_ID_REQUIRED' and image not in json.dumps(blank_upload[1],ensure_ascii=False), blank_upload[0]
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(new['id'],)).fetchone()[0]==0
            upload_idem=dict(id=new['id'],day=day,content=image,request_id='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',_expected_versions=upload_versions)
            upload_first=post(employee_client,'/api/workspace/nurture/upload',upload_idem)
            assert upload_first[0]==200 and upload_first[1].get('ok') is True and 'content' not in upload_first[1], upload_first
            upload_again=post(employee_client,'/api/workspace/nurture/upload',upload_idem)
            assert upload_again[0]==200 and upload_again[1].get('ok') is True and image not in json.dumps(upload_again[1],ensure_ascii=False), upload_again
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(new['id'],)).fetchone()[0]==1
                stored=raw_db.execute("SELECT result_json FROM idempotency_records WHERE request_id='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'").fetchone()[0]
                assert image not in stored and 'content' not in stored
            upload_reused=post(employee_client,'/api/workspace/nurture/upload',dict(upload_idem,day='2026-10-01'))
            assert upload_reused[0]==409 and upload_reused[1].get('code')=='REQUEST_ID_REUSED', upload_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(new['id'],)).fetchone()[0]==1
            after_upload=get(employee_client,'/api/board')
            bad_upload=dict(id=new['id'],day=day,content=base64.b64encode(b'<svg/>').decode(),request_id='cccccccc-cccc-4ccc-8ccc-cccccccccccc',_expected_versions={'account:'+new['id']:after_upload['record_versions']['account:'+new['id']],'nurture:'+new['id']:after_upload['record_versions']['nurture:'+new['id']]})
            bad_upload_first=post(employee_client,'/api/workspace/nurture/upload',bad_upload)
            assert bad_upload_first[0]==400 and bad_upload_first[1].get('error'), bad_upload_first
            assert post(employee_client,'/api/workspace/nurture/upload',bad_upload)==bad_upload_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(new['id'],)).fetchone()[0]==1
                image_id=raw_db.execute('SELECT id FROM nurture_images WHERE account_id=?',(new['id'],)).fetchone()[0]
                account_a_images=raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',('account-a',)).fetchone()[0]
                assert raw_db.execute('SELECT workflow FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()[0]=='已结束'
                raw_db.begin_write();raw_db.execute("UPDATE nurture_records SET workflow='运行',completed_on='',active_since=started_on WHERE account_id=?",(race_joined['id'],));raw_db.commit()
            race_upload_board=get(owner_client,'/api/board')
            race_upload_versions={race_join_account:race_upload_board['record_versions'][race_join_account],race_join_nurture:race_upload_board['record_versions'][race_join_nurture]}
            with runtime.connect() as raw_db:
                race_upload_ready=raw_db.execute('SELECT workflow,started_on FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()
                assert race_upload_ready[0]=='运行' and race_upload_ready[1] and race_upload_ready[1]<=day
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(race_joined['id'],)).fetchone()[0]==0
                joined_upload_images_before=raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(joined['id'],)).fetchone()[0]
                race_upload_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_nurture,)).fetchone()[0]
                race_upload_account_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_account,)).fetchone()[0]
                new_upload_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+new['id'],)).fetchone()[0]
                race_upload_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/upload' AND target=''").fetchone()[0]
            barrier_upload=threading.Barrier(2, timeout=5)
            def race_upload(client, request_id):
                barrier_upload.wait()
                return post(client,'/api/workspace/nurture/upload',dict(id=race_joined['id'],day=day,content=image,_expected_versions=dict(race_upload_versions),request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                upload_owner,upload_employee=[future.result(timeout=20) for future in (executor.submit(race_upload,owner_client,'b611b611-b611-4b61-8b61-b611b611b611'),executor.submit(race_upload,employee_client,'b622b622-b622-4b62-8b62-b622b622b622'))]
            assert sorted(item[0] for item in (upload_owner,upload_employee))==[200,409], tuple((item[0], item[1].get('code'), item[1].get('error'), item[1].get('ok')) for item in (upload_owner,upload_employee))
            assert next(item for item in (upload_owner,upload_employee) if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            upload_winner='test_owner' if upload_owner[0]==200 else 'synthetic_employee'
            upload_loser='synthetic_employee' if upload_winner=='test_owner' else 'test_owner'
            upload_winner_request='b611b611-b611-4b61-8b61-b611b611b611' if upload_owner[0]==200 else 'b622b622-b622-4b62-8b62-b622b622b622'
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(race_joined['id'],)).fetchone()[0]==1
                raced_upload=raw_db.execute('SELECT image_count,workflow FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()
                assert (raced_upload[0],raced_upload[1])==(1,'运行')
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(new['id'],)).fetchone()[0]==1
                assert raw_db.execute('SELECT id FROM nurture_images WHERE account_id=?',(new['id'],)).fetchone()[0]==image_id
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',('account-a',)).fetchone()[0]==account_a_images
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(joined['id'],)).fetchone()[0]==joined_upload_images_before
                stored_upload=raw_db.execute('SELECT result_json FROM idempotency_records WHERE request_id=?',(upload_winner_request,)).fetchone()[0]
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                race_upload_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_nurture,)).fetchall()
                race_upload_account_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_account,)).fetchall()
                new_upload_history_after=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+new['id'],)).fetchone()[0]
                race_upload_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/workspace/nurture/upload' AND target='' ORDER BY id").fetchall()
            assert stored_upload and image not in stored_upload and 'content' not in stored_upload
            upload_added=[row['actor_name'] for row in race_upload_history[race_upload_history_before:]]
            upload_account_added=[row['actor_name'] for row in race_upload_account_history[race_upload_account_history_before:]]
            upload_audited=[row['actor_name'] for row in race_upload_audit[race_upload_audit_before:]]
            assert upload_added==[names[upload_winner]] and names[upload_loser] not in upload_added, upload_added
            assert all(name==names[upload_winner] for name in upload_account_added) and names[upload_loser] not in upload_account_added, upload_account_added
            assert upload_audited==[names[upload_winner]] and names[upload_loser] not in upload_audited, upload_audited
            assert new_upload_history_after==new_upload_history_before
            after_upload_race=get(owner_client,'/api/board')
            assert after_upload_race['record_versions'][race_join_nurture]!=race_upload_versions[race_join_nurture]
            ready_delete=get(employee_client,'/api/board')
            delete_versions={'account:'+new['id']:ready_delete['record_versions']['account:'+new['id']],'nurture:'+new['id']:ready_delete['record_versions']['nurture:'+new['id']]}
            missing_delete=post(employee_client,'/api/workspace/nurture/delete-image',dict(id=new['id'],image_id=image_id,_expected_versions=delete_versions))
            assert missing_delete[0]==400 and missing_delete[1].get('code')=='REQUEST_ID_REQUIRED', missing_delete
            blank_delete=post(employee_client,'/api/workspace/nurture/delete-image',dict(id=new['id'],image_id=image_id,request_id='',_expected_versions=delete_versions))
            assert blank_delete[0]==400 and blank_delete[1].get('code')=='REQUEST_ID_REQUIRED', blank_delete
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(new['id'],)).fetchone()[0]==1
            delete_idem=dict(id=new['id'],image_id=image_id,request_id='74747474-7474-4747-8747-747474747474',_expected_versions=delete_versions)
            delete_first=post(employee_client,'/api/workspace/nurture/delete-image',delete_idem)
            assert delete_first[0]==200 and delete_first[1].get('ok') is True, delete_first
            delete_again=post(employee_client,'/api/workspace/nurture/delete-image',delete_idem)
            assert delete_again[0]==200 and delete_again[1].get('ok') is True, delete_again
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(new['id'],)).fetchone()[0]==0
            delete_reused=post(employee_client,'/api/workspace/nurture/delete-image',dict(delete_idem,id='account-a'))
            assert delete_reused[0]==409 and delete_reused[1].get('code')=='REQUEST_ID_REUSED', delete_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(new['id'],)).fetchone()[0]==0
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',('account-a',)).fetchone()[0]==account_a_images
                race_image=raw_db.execute('SELECT id FROM nurture_images WHERE account_id=?',(race_joined['id'],)).fetchone()
                race_delete_ready=raw_db.execute('SELECT workflow,completed_on,image_count FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()
            assert race_image and (race_delete_ready[0],race_delete_ready[1],race_delete_ready[2])==('运行','',1)
            race_image_id=race_image[0]
            race_delete_board=get(owner_client,'/api/board')
            race_delete_versions={race_join_account:race_delete_board['record_versions'][race_join_account],race_join_nurture:race_delete_board['record_versions'][race_join_nurture]}
            with runtime.connect() as raw_db:
                race_delete_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_nurture,)).fetchone()[0]
                race_delete_account_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_account,)).fetchone()[0]
                new_delete_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+new['id'],)).fetchone()[0]
                race_delete_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/delete-image' AND target=''").fetchone()[0]
            barrier_delete=threading.Barrier(2, timeout=5)
            def race_delete(client, request_id):
                barrier_delete.wait()
                return post(client,'/api/workspace/nurture/delete-image',dict(id=race_joined['id'],image_id=race_image_id,_expected_versions=dict(race_delete_versions),request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                delete_owner,delete_employee=[future.result(timeout=20) for future in (executor.submit(race_delete,owner_client,'b711b711-b711-4b71-8b71-b711b711b711'),executor.submit(race_delete,employee_client,'b722b722-b722-4b72-8b72-b722b722b722'))]
            assert sorted(item[0] for item in (delete_owner,delete_employee))==[200,409], tuple((item[0], item[1].get('code'), item[1].get('error'), item[1].get('ok')) for item in (delete_owner,delete_employee))
            assert next(item for item in (delete_owner,delete_employee) if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            delete_winner='test_owner' if delete_owner[0]==200 else 'synthetic_employee'
            delete_loser='synthetic_employee' if delete_winner=='test_owner' else 'test_owner'
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(race_joined['id'],)).fetchone()[0]==0
                raced_delete=raw_db.execute('SELECT image_count,workflow FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()
                assert (raced_delete[0],raced_delete[1])==(0,'运行')
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(new['id'],)).fetchone()[0]==0
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',('account-a',)).fetchone()[0]==account_a_images
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                race_delete_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_nurture,)).fetchall()
                race_delete_account_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_account,)).fetchall()
                new_delete_history_after=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+new['id'],)).fetchone()[0]
                race_delete_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/workspace/nurture/delete-image' AND target='' ORDER BY id").fetchall()
            delete_added=[row['actor_name'] for row in race_delete_history[race_delete_history_before:]]
            delete_account_added=[row['actor_name'] for row in race_delete_account_history[race_delete_account_history_before:]]
            delete_audited=[row['actor_name'] for row in race_delete_audit[race_delete_audit_before:]]
            assert delete_added==[names[delete_winner]] and names[delete_loser] not in delete_added, delete_added
            assert all(name==names[delete_winner] for name in delete_account_added) and names[delete_loser] not in delete_account_added, delete_account_added
            assert delete_audited==[names[delete_winner]] and names[delete_loser] not in delete_audited, delete_audited
            assert new_delete_history_after==new_delete_history_before
            after_delete_race=get(owner_client,'/api/board')
            assert after_delete_race['record_versions'][race_join_nurture]!=race_delete_versions[race_join_nurture]
            after_delete=get(employee_client,'/api/board')
            bad_delete=dict(id=new['id'],image_id=image_id,request_id='75757575-7575-4757-8757-757575757575',_expected_versions={'account:'+new['id']:after_delete['record_versions']['account:'+new['id']],'nurture:'+new['id']:after_delete['record_versions']['nurture:'+new['id']]})
            bad_delete_first=post(employee_client,'/api/workspace/nurture/delete-image',bad_delete)
            assert bad_delete_first[0]==400 and '截图不存在' in bad_delete_first[1].get('error',''), bad_delete_first
            assert post(employee_client,'/api/workspace/nurture/delete-image',bad_delete)==bad_delete_first
            early_purge=dict(id=new['id'],request_id='76767676-7676-4767-8767-767676767676',_expected_versions={'account:'+new['id']:after_delete['record_versions']['account:'+new['id']],'nurture:'+new['id']:after_delete['record_versions']['nurture:'+new['id']]})
            early_purge_first=post(employee_client,'/api/workspace/nurture/purge',early_purge)
            assert early_purge_first[0]==400 and '请先结束本轮养号' in early_purge_first[1].get('error',''), early_purge_first
            assert post(employee_client,'/api/workspace/nurture/purge',early_purge)==early_purge_first
            with runtime.connect() as raw_db:
                cycle=raw_db.execute('SELECT cycle FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]
                raw_db.begin_write()
                raw_db.execute("INSERT INTO nurture_images VALUES(?,?,?,?,?,?,?)",('synthetic-purge-image',joined['id'],cycle,day,'image/png',png(),'合成人员'))
                raw_db.execute('UPDATE nurture_records SET image_count=image_count+1 WHERE account_id=?',(joined['id'],))
                raw_db.commit()
                account_a_purge=raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',('account-a',)).fetchone()[0]
            ready_purge=get(employee_client,'/api/board')
            purge_versions={'account:'+joined['id']:ready_purge['record_versions']['account:'+joined['id']],'nurture:'+joined['id']:ready_purge['record_versions']['nurture:'+joined['id']]}
            missing_purge=post(employee_client,'/api/workspace/nurture/purge',dict(id=joined['id'],_expected_versions=purge_versions))
            assert missing_purge[0]==400 and missing_purge[1].get('code')=='REQUEST_ID_REQUIRED', missing_purge
            blank_purge=post(employee_client,'/api/workspace/nurture/purge',dict(id=joined['id'],request_id='',_expected_versions=purge_versions))
            assert blank_purge[0]==400 and blank_purge[1].get('code')=='REQUEST_ID_REQUIRED', blank_purge
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(joined['id'],)).fetchone()[0]==1
            purge_idem=dict(id=joined['id'],request_id='78787878-7878-4787-8787-787878787878',_expected_versions=purge_versions)
            purge_first=post(employee_client,'/api/workspace/nurture/purge',purge_idem)
            assert purge_first[0]==200 and purge_first[1].get('ok') is True, purge_first
            with runtime.connect() as raw_db:
                purge_revision=raw_db.execute('SELECT revision,image_count FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()
                assert purge_revision[1]==0
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(joined['id'],)).fetchone()[0]==0
            purge_again=post(employee_client,'/api/workspace/nurture/purge',purge_idem)
            assert purge_again[0]==200 and purge_again[1].get('ok') is True, purge_again
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT revision,image_count FROM nurture_records WHERE account_id=?',(joined['id'],)).fetchone()[0]==purge_revision[0]
            purge_reused=post(employee_client,'/api/workspace/nurture/purge',dict(purge_idem,id='account-a'))
            assert purge_reused[0]==409 and purge_reused[1].get('code')=='REQUEST_ID_REUSED', purge_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(joined['id'],)).fetchone()[0]==0
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',('account-a',)).fetchone()[0]==account_a_purge
                race_cycle=raw_db.execute('SELECT cycle FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()[0]
                raw_db.begin_write()
                raw_db.execute("UPDATE nurture_records SET workflow='已结束',completed_on=?,active_since='' WHERE account_id=?",(day,race_joined['id']))
                raw_db.execute("INSERT INTO nurture_images VALUES(?,?,?,?,?,?,?)",('synthetic-race-purge-image',race_joined['id'],race_cycle,day,'image/png',png(),'合成人员'))
                raw_db.execute('UPDATE nurture_records SET image_count=1 WHERE account_id=?',(race_joined['id'],))
                raw_db.commit()
            race_purge_board=get(owner_client,'/api/board')
            race_purge_versions={race_join_account:race_purge_board['record_versions'][race_join_account],race_join_nurture:race_purge_board['record_versions'][race_join_nurture]}
            with runtime.connect() as raw_db:
                race_purge_ready=raw_db.execute('SELECT workflow,completed_on,image_count FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()
                assert (race_purge_ready[0],race_purge_ready[1],race_purge_ready[2])==('已结束',day,1)
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(race_joined['id'],)).fetchone()[0]==1
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(joined['id'],)).fetchone()[0]==0
                race_purge_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_nurture,)).fetchone()[0]
                race_purge_account_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(race_join_account,)).fetchone()[0]
                joined_purge_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+joined['id'],)).fetchone()[0]
                race_purge_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/purge' AND target=''").fetchone()[0]
            barrier_purge=threading.Barrier(2, timeout=5)
            def race_purge(client, request_id):
                barrier_purge.wait()
                return post(client,'/api/workspace/nurture/purge',dict(id=race_joined['id'],_expected_versions=dict(race_purge_versions),request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                purge_owner,purge_employee=[future.result(timeout=20) for future in (executor.submit(race_purge,owner_client,'b811b811-b811-4b81-8b81-b811b811b811'),executor.submit(race_purge,employee_client,'b822b822-b822-4b82-8b82-b822b822b822'))]
            assert sorted(item[0] for item in (purge_owner,purge_employee))==[200,409], tuple((item[0], item[1].get('code'), item[1].get('error'), item[1].get('ok')) for item in (purge_owner,purge_employee))
            assert next(item for item in (purge_owner,purge_employee) if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            purge_winner='test_owner' if purge_owner[0]==200 else 'synthetic_employee'
            purge_loser='synthetic_employee' if purge_winner=='test_owner' else 'test_owner'
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(race_joined['id'],)).fetchone()[0]==0
                raced_purge=raw_db.execute('SELECT image_count,workflow,completed_on FROM nurture_records WHERE account_id=?',(race_joined['id'],)).fetchone()
                assert (raced_purge[0],raced_purge[1],raced_purge[2])==(0,'已结束',day)
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(joined['id'],)).fetchone()[0]==0
                assert raw_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',('account-a',)).fetchone()[0]==account_a_purge
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                race_purge_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_nurture,)).fetchall()
                race_purge_account_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(race_join_account,)).fetchall()
                joined_purge_history_after=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+joined['id'],)).fetchone()[0]
                race_purge_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/workspace/nurture/purge' AND target='' ORDER BY id").fetchall()
            purge_added=[row['actor_name'] for row in race_purge_history[race_purge_history_before:]]
            purge_account_added=[row['actor_name'] for row in race_purge_account_history[race_purge_account_history_before:]]
            purge_audited=[row['actor_name'] for row in race_purge_audit[race_purge_audit_before:]]
            assert purge_added==[names[purge_winner]] and names[purge_loser] not in purge_added, purge_added
            assert all(name==names[purge_winner] for name in purge_account_added) and names[purge_loser] not in purge_account_added, purge_account_added
            assert purge_audited==[names[purge_winner]] and names[purge_loser] not in purge_audited, purge_audited
            assert joined_purge_history_after==joined_purge_history_before
            after_purge_race=get(owner_client,'/api/board')
            assert after_purge_race['record_versions'][race_join_nurture]!=race_purge_versions[race_join_nurture]
            denied=preview_csv('内部编号,昵称\naccount-b,合成拒绝修改\n',office_id=office_a)
            assert denied['rows'][0]['action']=='error' and denied['rows'][0]['changes']==[]
            assert secret not in json.dumps(denied,ensure_ascii=False) and 'identity' not in denied['rows'][0]
            denied=preview_csv('内部编号,关联大号编号\naccount-a,account-b\n',office_id=office_a)
            assert denied['rows'][0]['action']=='error' and denied['rows'][0]['changes']==[]
            pending=preview_csv('抖音号,昵称\nsynthetic-import-revoked,合成撤权拒绝\n',office_id=office_a)
            assert pending['rows'][0]['action']=='create'
            assert post(owner_client,'/api/auth/users',dict(update,office_ids=[office_b],request_id='cbcbcbcb-cbcb-4cbc-8cbc-cbcbcbcbcbcb'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            narrowed=get(employee_client,'/api/board')
            assert all(row['id']!='account-a' for row in narrowed['accounts'])
            assert any(row['id']=='account-b' for row in narrowed['accounts'])
            linked_device=next(row for row in narrowed['phone_devices']['devices'] if row['id']=='2')
            assert linked_device['account_ids']==[] and linked_device['account_count']==0 and linked_device['office_ids']==[office_b]
            try:
                employee_client.open(base+'/api/nurture/image?id=image-account-a',timeout=10)
                raise AssertionError('Linked account image served')
            except urllib.error.HTTPError as error:assert error.code==403
            try:
                get(employee_client,'/api/workspace/history?entity=account:account-a')
                raise AssertionError('Linked account history accepted')
            except urllib.error.HTTPError as error:assert error.code==403
            owner_linked=next(row for row in get(owner_client,'/api/board')['accounts'] if row['id']=='account-a')
            assert owner_linked['office_ids']==[office_a] and owner_linked['phone_device_id']=='2' and owner_linked['computer_id']=='pc-b'
            assert post(employee_client,'/api/workspace/import-apply',dict(preview_id=pending['preview_id'],rows=[2]))[0]==403
            assert not any(row['douyin_id']=='synthetic-import-revoked' for row in get(owner_client,'/api/board')['accounts'])
            assert post(owner_client,'/api/auth/users',dict(update,office_ids=[office_a,office_b],request_id='cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            assert post(employee_client,'/api/phone-card',dict(id=1,phone='13800000001',status='正常',request_id='79797979-7979-4797-8797-797979797979'))[0]==200
            assert any(row['id']==new['id'] for row in get(employee_client,'/api/board')['accounts'])
            multi=dict(name='合成多办公室新增',computer_id='pc-b',slot=2,status='正常',office_id=office_b,request_id='c7c7c7c7-c7c7-4c7c-8c7c-c7c7c7c7c7c7')
            assert post(employee_client,'/api/account/create',multi)[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['name']=='合成多办公室新增' and row['office_ids']==[office_b] for row in get(employee_client,'/api/board')['accounts'])
            idem=dict(name='合成幂等账号',computer_id='pc-a',slot=9,status='正常',office_id=office_a,request_id='11111111-1111-4111-8111-111111111111',_expected_versions={})
            first=post(employee_client,'/api/account/create',idem)
            assert first[0]==200 and first[1].get('ok') is True and first[1].get('id'), first
            again=post(employee_client,'/api/account/create',idem)
            assert again[0]==200 and again[1].get('id')==first[1]['id'], again
            matched=[row for row in get(owner_client,'/api/board')['accounts'] if row['name']=='合成幂等账号']
            assert len(matched)==1 and matched[0]['id']==first[1]['id'] and matched[0]['office_ids']==[office_a]
            reused=post(employee_client,'/api/account/create',dict(idem,name='合成幂等被拒绝'))
            assert reused[0]==409 and reused[1].get('code')=='REQUEST_ID_REUSED', reused
            assert len([row for row in get(owner_client,'/api/board')['accounts'] if row['id']==first[1]['id']])==1
            bad_body=dict(idem,request_id='22222222-2222-4222-8222-222222222222',slot=0,name='合成幂等失败')
            with runtime.connect() as raw_db:
                inserted_before=raw_db.execute("SELECT count(*) FROM accounts WHERE name='新账号'").fetchone()[0]
            bad=post(employee_client,'/api/account/create',bad_body)
            assert bad[0]==400 and bad[1].get('error'), bad
            assert post(employee_client,'/api/account/create',bad_body)==bad
            assert not any(row['name']=='合成幂等失败' for row in get(owner_client,'/api/board')['accounts'])
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT count(*) FROM accounts WHERE name='新账号'").fetchone()[0]==inserted_before
            ready_patch=get(employee_client,'/api/board')
            patch_id=first[1]['id']
            patch_idem=dict(id=patch_id,changes={'note':'合成幂等备注'},request_id='81818181-8181-4181-8181-818181818181',_expected_versions={'account:'+patch_id:ready_patch['record_versions']['account:'+patch_id]})
            patch_first=post(employee_client,'/api/account/patch',patch_idem)
            assert patch_first[0]==200 and patch_first[1].get('ok') is True and patch_first[1].get('id')==patch_id, patch_first
            patch_again=post(employee_client,'/api/account/patch',patch_idem)
            assert patch_again[0]==200 and patch_again[1].get('id')==patch_id, patch_again
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT note FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]=='合成幂等备注'
                account_a_note=raw_db.execute('SELECT note FROM accounts WHERE id=?',('account-a',)).fetchone()[0]
            patch_reused=post(employee_client,'/api/account/patch',dict(patch_idem,id='account-a',changes={'note':'合成幂等另一备注'}))
            assert patch_reused[0]==409 and patch_reused[1].get('code')=='REQUEST_ID_REUSED', patch_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT note FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]=='合成幂等备注'
                assert raw_db.execute('SELECT note FROM accounts WHERE id=?',('account-a',)).fetchone()[0]==account_a_note
            after_patch=get(employee_client,'/api/board')
            bad_patch=dict(id=patch_id,changes={'note':'备'*2001},request_id='82828282-8282-4282-8282-828282828282',_expected_versions={'account:'+patch_id:after_patch['record_versions']['account:'+patch_id]})
            bad_patch_first=post(employee_client,'/api/account/patch',bad_patch)
            assert bad_patch_first[0]==400 and '备注不能超过2000字' in bad_patch_first[1].get('error',''), bad_patch_first
            assert post(employee_client,'/api/account/patch',bad_patch)==bad_patch_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT note FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]=='合成幂等备注'
            ready_status=get(employee_client,'/api/board')
            status_versions={'account:'+patch_id:ready_status['record_versions']['account:'+patch_id]}
            missing_status=post(employee_client,'/api/account/status',dict(id=patch_id,status='停用',_expected_versions=status_versions))
            assert missing_status[0]==400 and missing_status[1].get('code')=='REQUEST_ID_REQUIRED', missing_status
            blank_status=post(employee_client,'/api/account/status',dict(id=patch_id,status='停用',request_id='',_expected_versions=status_versions))
            assert blank_status[0]==400 and blank_status[1].get('code')=='REQUEST_ID_REQUIRED', blank_status
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]=='正常'
            status_idem=dict(id=patch_id,status='停用',request_id='83838383-8383-4383-8383-838383838383',_expected_versions=status_versions)
            status_first=post(employee_client,'/api/account/status',status_idem)
            assert status_first[0]==200 and status_first[1].get('ok') is True and status_first[1].get('id')==patch_id, status_first
            status_again=post(employee_client,'/api/account/status',status_idem)
            assert status_again[0]==200 and status_again[1].get('id')==patch_id, status_again
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]=='停用'
            status_reused=post(employee_client,'/api/account/status',dict(status_idem,status='封禁'))
            assert status_reused[0]==409 and status_reused[1].get('code')=='REQUEST_ID_REUSED', status_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]=='停用'
            after_status=get(employee_client,'/api/board')
            bad_status=dict(id=patch_id,status='不是状态',request_id='84848484-8484-4484-8484-848484848484',_expected_versions={'account:'+patch_id:after_status['record_versions']['account:'+patch_id]})
            bad_status_first=post(employee_client,'/api/account/status',bad_status)
            assert bad_status_first[0]==400 and '账号状态不正确' in bad_status_first[1].get('error',''), bad_status_first
            assert post(employee_client,'/api/account/status',bad_status)==bad_status_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]=='停用'
            status_key='account:'+patch_id
            status_race_board=get(owner_client,'/api/board')
            status_race_version=status_race_board['record_versions'][status_key]
            with runtime.connect() as raw_db:
                status_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(status_key,)).fetchone()[0]
                status_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/status' AND target=?",(patch_id,)).fetchone()[0]
            barrier_status=threading.Barrier(2, timeout=5)
            def race_status(client, state, request_id):
                barrier_status.wait()
                return post(client,'/api/account/status',dict(id=patch_id,status=state,_expected_versions={status_key:status_race_version},request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                status_raced=[future.result(timeout=20) for future in (executor.submit(race_status,owner_client,'封禁','d11d11d1-d11d-411d-811d-d11d11d11d11'),executor.submit(race_status,employee_client,'短期封禁','d22d22d2-d22d-422d-822d-d22d22d22d22'))]
            assert sorted(item[0] for item in status_raced)==[200,409], status_raced
            assert next(item for item in status_raced if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            with runtime.connect() as raw_db:
                raced_status=raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                status_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(status_key,)).fetchall()
                status_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/account/status' AND target=? ORDER BY id",(patch_id,)).fetchall()
            assert raced_status in ('封禁','短期封禁')
            status_winner='test_owner' if raced_status=='封禁' else 'synthetic_employee'
            status_loser='synthetic_employee' if status_winner=='test_owner' else 'test_owner'
            status_added=[row['actor_name'] for row in status_history[status_history_before:]]
            status_audited=[row['actor_name'] for row in status_audit[status_audit_before:]]
            assert status_added==[names[status_winner]] and names[status_loser] not in status_added, (raced_status,status_added)
            assert status_audited==[names[status_winner]] and names[status_loser] not in status_audited, (raced_status,status_audited)
            assert get(owner_client,'/api/board')['record_versions'][status_key]!=status_race_version
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute("UPDATE accounts SET status='停用' WHERE id=?",(patch_id,))
                raw_db.commit()
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute("INSERT INTO idempotency_records(restore_generation,user_id,request_id,action,request_sha256,state,result_status,result_json,created_at,completed_at) VALUES('retired-generation',?,'33333333-3333-4333-8333-333333333333','/api/account/create','synthetic-old','succeeded',200,'{\"ok\":true,\"id\":\"hidden-old\"}',1,1)",(employee,))
                raw_db.commit()
            stale_generation=post(employee_client,'/api/account/create',dict(idem,request_id='33333333-3333-4333-8333-333333333333',name='合成旧代次拒绝',slot=8))
            assert stale_generation[0]==409 and stale_generation[1].get('code')=='RESTORE_GENERATION_EXPIRED', stale_generation
            assert 'hidden-old' not in json.dumps(stale_generation[1],ensure_ascii=False)
            assert not any(row['name']=='合成旧代次拒绝' for row in get(owner_client,'/api/board')['accounts'])
            card_idem=dict(phone='19900000011',office_ids=[office_a],request_id='44444444-4444-4444-8444-444444444444',_expected_versions={})
            card_first=post(employee_client,'/api/phone-card',card_idem)
            assert card_first[0]==200 and card_first[1].get('id'), card_first
            card_again=post(employee_client,'/api/phone-card',card_idem)
            assert card_again[0]==200 and card_again[1].get('id')==card_first[1]['id'], card_again
            card_rows=[row for row in get(owner_client,'/api/board')['phone_cards']['cards'] if row['phone']=='19900000011']
            assert len(card_rows)==1 and card_rows[0]['id']==card_first[1]['id'] and office_a in card_rows[0]['office_ids']
            card_reused=post(employee_client,'/api/phone-card',dict(card_idem,phone='19900000012'))
            assert card_reused[0]==409 and card_reused[1].get('code')=='REQUEST_ID_REUSED', card_reused
            assert len([row for row in get(owner_client,'/api/board')['phone_cards']['cards'] if row['phone']=='19900000011'])==1
            ready_card=get(employee_client,'/api/board')
            card_update_id=card_first[1]['id']
            card_key='card:'+str(card_update_id)
            card_versions={card_key:ready_card['record_versions'][card_key]}
            missing_card_update=post(employee_client,'/api/phone-card',dict(id=card_update_id,phone='19900000011',label='合成幂等卡标签',office_ids=[office_a],_expected_versions=card_versions))
            assert missing_card_update[0]==400 and missing_card_update[1].get('code')=='REQUEST_ID_REQUIRED', missing_card_update
            blank_card_update=post(employee_client,'/api/phone-card',dict(id=card_update_id,phone='19900000011',label='合成幂等卡标签',office_ids=[office_a],request_id='',_expected_versions=card_versions))
            assert blank_card_update[0]==400 and blank_card_update[1].get('code')=='REQUEST_ID_REQUIRED', blank_card_update
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT label FROM phone_cards WHERE id=?',(card_update_id,)).fetchone()[0]!='合成幂等卡标签'
            card_update=dict(id=card_update_id,phone='19900000011',label='合成幂等卡标签',office_ids=[office_a],request_id='85858585-8585-4585-8585-858585858585',_expected_versions=card_versions)
            card_update_first=post(employee_client,'/api/phone-card',card_update)
            assert card_update_first[0]==200 and card_update_first[1].get('ok') is True and card_update_first[1].get('id')==card_update_id, card_update_first
            card_update_again=post(employee_client,'/api/phone-card',card_update)
            assert card_update_again[0]==200 and card_update_again[1].get('id')==card_update_id, card_update_again
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT label FROM phone_cards WHERE id=?',(card_update_id,)).fetchone()[0]=='合成幂等卡标签'
            card_update_reused=post(employee_client,'/api/phone-card',dict(card_update,label='合成幂等卡另一标签'))
            assert card_update_reused[0]==409 and card_update_reused[1].get('code')=='REQUEST_ID_REUSED', card_update_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT label,phone FROM phone_cards WHERE id=?',(card_update_id,)).fetchone()[0]=='合成幂等卡标签'
                assert raw_db.execute('SELECT phone FROM phone_cards WHERE id=?',(card_update_id,)).fetchone()[0]=='19900000011'
            after_card=get(employee_client,'/api/board')
            bad_card=dict(id=card_update_id,phone='19900000011',label='标'*101,office_ids=[office_a],request_id='86868686-8686-4686-8686-868686868686',_expected_versions={card_key:after_card['record_versions'][card_key]})
            bad_card_first=post(employee_client,'/api/phone-card',bad_card)
            assert bad_card_first[0]==400 and '手机卡字段内容过长' in bad_card_first[1].get('error',''), bad_card_first
            assert post(employee_client,'/api/phone-card',bad_card)==bad_card_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT label FROM phone_cards WHERE id=?',(card_update_id,)).fetchone()[0]=='合成幂等卡标签'
            card_race_board=get(owner_client,'/api/board')
            card_race_version=card_race_board['record_versions'][card_key]
            with runtime.connect() as raw_db:
                card_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(card_key,)).fetchone()[0]
                card_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card' AND target=?",(str(card_update_id),)).fetchone()[0]
            barrier_card=threading.Barrier(2, timeout=5)
            def race_card(client, label, request_id):
                barrier_card.wait()
                return post(client,'/api/phone-card',dict(id=card_update_id,phone='19900000011',label=label,office_ids=[office_a],_expected_versions={card_key:card_race_version},request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                card_raced=[future.result(timeout=20) for future in (executor.submit(race_card,owner_client,'合成卡并发甲','ca11ca11-ca11-4ca1-8ca1-ca11ca11ca11'),executor.submit(race_card,employee_client,'合成卡并发乙','ca22ca22-ca22-4ca2-8ca2-ca22ca22ca22'))]
            assert sorted(item[0] for item in card_raced)==[200,409], card_raced
            assert next(item for item in card_raced if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            card_after=next(row for row in get(owner_client,'/api/board')['phone_cards']['cards'] if row['id']==card_update_id)
            assert card_after['label'] in ('合成卡并发甲','合成卡并发乙') and card_after['phone']=='19900000011' and office_a in card_after['office_ids']
            assert get(owner_client,'/api/board')['record_versions'][card_key]!=card_race_version
            with runtime.connect() as raw_db:
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                card_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(card_key,)).fetchall()
                card_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/phone-card' AND target=? ORDER BY id",(str(card_update_id),)).fetchall()
            card_winner='test_owner' if card_after['label']=='合成卡并发甲' else 'synthetic_employee'
            card_loser='synthetic_employee' if card_winner=='test_owner' else 'test_owner'
            card_added=[row['actor_name'] for row in card_history[card_history_before:]]
            card_audited=[row['actor_name'] for row in card_audit[card_audit_before:]]
            assert card_added==[names[card_winner]] and names[card_loser] not in card_added, (card_after['label'],card_added)
            assert card_audited==[names[card_winner]] and names[card_loser] not in card_audited, (card_after['label'],card_audited)
            device_idem=dict(id='8',name='合成幂等手机',state='在用',office_ids=[office_a],request_id='55555555-5555-4555-8555-555555555555',_expected_versions={})
            device_first=post(employee_client,'/api/phone-device/create',device_idem)
            assert device_first[0]==200 and device_first[1].get('id')=='8', device_first
            device_again=post(employee_client,'/api/phone-device/create',device_idem)
            assert device_again[0]==200 and device_again[1].get('id')=='8', device_again
            device_rows=[row for row in get(owner_client,'/api/board')['phone_devices']['devices'] if row['id']=='8']
            assert len(device_rows)==1 and device_rows[0]['name']=='合成幂等手机' and office_a in device_rows[0]['office_ids']
            device_reused=post(employee_client,'/api/phone-device/create',dict(device_idem,name='合成幂等手机被拒绝'))
            assert device_reused[0]==409 and device_reused[1].get('code')=='REQUEST_ID_REUSED', device_reused
            assert len([row for row in get(owner_client,'/api/board')['phone_devices']['devices'] if row['id']=='8'])==1
            ready_device=get(employee_client,'/api/board')
            device_versions={'device:8':ready_device['record_versions']['device:8']}
            missing_device=post(employee_client,'/api/phone-device',dict(id='8',name='合成幂等手机已改',state='在用',office_ids=[office_a],_expected_versions=device_versions))
            assert missing_device[0]==400 and missing_device[1].get('code')=='REQUEST_ID_REQUIRED', missing_device
            blank_device=post(employee_client,'/api/phone-device',dict(id='8',name='合成幂等手机已改',state='在用',office_ids=[office_a],request_id='',_expected_versions=device_versions))
            assert blank_device[0]==400 and blank_device[1].get('code')=='REQUEST_ID_REQUIRED', blank_device
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT name FROM phone_devices WHERE id=?',('8',)).fetchone()[0]=='合成幂等手机'
            device_update=dict(id='8',name='合成幂等手机已改',state='在用',office_ids=[office_a],request_id='89898989-8989-4989-8989-898989898989',_expected_versions=device_versions)
            device_update_first=post(employee_client,'/api/phone-device',device_update)
            assert device_update_first[0]==200 and device_update_first[1].get('ok') is True and device_update_first[1].get('id')=='8', device_update_first
            device_update_again=post(employee_client,'/api/phone-device',device_update)
            assert device_update_again[0]==200 and device_update_again[1].get('id')=='8', device_update_again
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT name FROM phone_devices WHERE id=?',('8',)).fetchone()[0]=='合成幂等手机已改'
            device_update_reused=post(employee_client,'/api/phone-device',dict(device_update,name='合成幂等手机被改拒绝'))
            assert device_update_reused[0]==409 and device_update_reused[1].get('code')=='REQUEST_ID_REUSED', device_update_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT name FROM phone_devices WHERE id=?',('8',)).fetchone()[0]=='合成幂等手机已改'
            after_device=get(employee_client,'/api/board')
            bad_device=dict(id='8',name='合成幂等手机已改',state='不是状态',office_ids=[office_a],request_id='90909090-9090-4909-8909-909090909090',_expected_versions={'device:8':after_device['record_versions']['device:8']})
            bad_device_first=post(employee_client,'/api/phone-device',bad_device)
            assert bad_device_first[0]==400 and '手机设备状态不正确' in bad_device_first[1].get('error',''), bad_device_first
            assert post(employee_client,'/api/phone-device',bad_device)==bad_device_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT name,state FROM phone_devices WHERE id=?',('8',)).fetchone()[0]=='合成幂等手机已改'
                assert raw_db.execute('SELECT state FROM phone_devices WHERE id=?',('8',)).fetchone()[0]=='在用'
            device_race_board=get(owner_client,'/api/board')
            device_race_row=next(row for row in device_race_board['phone_devices']['devices'] if row['id']=='8')
            assert set(device_race_row['office_ids'])=={office_a}
            device_race_version=device_race_board['record_versions']['device:8']
            with runtime.connect() as raw_db:
                device_history_before=raw_db.execute("SELECT count(*) FROM operations_history WHERE entity='device:8'").fetchone()[0]
                device_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device' AND target='8'").fetchone()[0]
            barrier_device=threading.Barrier(2, timeout=5)
            def race_device(client, name, request_id):
                barrier_device.wait()
                return post(client,'/api/phone-device',dict(id='8',name=name,state='在用',office_ids=[office_a],_expected_versions={'device:8':device_race_version},request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                device_raced=[future.result(timeout=20) for future in (executor.submit(race_device,owner_client,'合成设备并发甲','da11da11-da11-4da1-8da1-da11da11da11'),executor.submit(race_device,employee_client,'合成设备并发乙','da22da22-da22-4da2-8da2-da22da22da22'))]
            assert sorted(item[0] for item in device_raced)==[200,409], device_raced
            assert next(item for item in device_raced if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            device_after=next(row for row in get(owner_client,'/api/board')['phone_devices']['devices'] if row['id']=='8')
            assert device_after['name'] in ('合成设备并发甲','合成设备并发乙') and device_after['state']=='在用' and set(device_after['office_ids'])=={office_a}
            assert get(owner_client,'/api/board')['record_versions']['device:8']!=device_race_version
            with runtime.connect() as raw_db:
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                device_history=raw_db.execute("SELECT actor_name FROM operations_history WHERE entity='device:8' ORDER BY id").fetchall()
                device_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/phone-device' AND target='8' ORDER BY id").fetchall()
            device_winner='test_owner' if device_after['name']=='合成设备并发甲' else 'synthetic_employee'
            device_loser='synthetic_employee' if device_winner=='test_owner' else 'test_owner'
            device_added=[row['actor_name'] for row in device_history[device_history_before:]]
            device_audited=[row['actor_name'] for row in device_audit[device_audit_before:]]
            assert device_added==[names[device_winner]] and names[device_loser] not in device_added, (device_after['name'],device_added)
            assert device_audited==[names[device_winner]] and names[device_loser] not in device_audited, (device_after['name'],device_audited)
            ready_layout=get(employee_client,'/api/board')
            layout_key='layout:synthetic-batch:pc-a:1'
            layout_version=ready_layout['record_versions'][layout_key]
            bad_assign=dict(batch='synthetic-batch',computer_id='pc-a',slot=1,action='不是操作',request_id='92929292-9292-4929-8929-929292929292',_expected_versions={layout_key:layout_version})
            bad_assign_first=post(employee_client,'/api/publishing/assignment',bad_assign)
            assert bad_assign_first[0]==400 and '位置操作不正确' in bad_assign_first[1].get('error',''), bad_assign_first
            assert post(employee_client,'/api/publishing/assignment',bad_assign)==bad_assign_first
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=1").fetchone()[0]=='account-a'
            assign_versions={layout_key:layout_version}
            missing_assign=post(employee_client,'/api/publishing/assignment',dict(batch='synthetic-batch',computer_id='pc-a',slot=1,action='remove',_expected_versions=assign_versions))
            assert missing_assign[0]==400 and missing_assign[1].get('code')=='REQUEST_ID_REQUIRED', missing_assign
            blank_assign=post(employee_client,'/api/publishing/assignment',dict(batch='synthetic-batch',computer_id='pc-a',slot=1,action='remove',request_id='',_expected_versions=assign_versions))
            assert blank_assign[0]==400 and blank_assign[1].get('code')=='REQUEST_ID_REQUIRED', blank_assign
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=1").fetchone()[0]=='account-a'
            assign_idem=dict(batch='synthetic-batch',computer_id='pc-a',slot=1,action='remove',request_id='91919191-9191-4919-8919-919191919191',_expected_versions=assign_versions)
            assign_first=post(employee_client,'/api/publishing/assignment',assign_idem)
            assert assign_first[0]==200 and assign_first[1].get('ok') is True, assign_first
            assign_again=post(employee_client,'/api/publishing/assignment',assign_idem)
            assert assign_again[0]==200 and assign_again[1].get('ok') is True, assign_again
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=1").fetchone()[0] is None
            assign_reused=post(employee_client,'/api/publishing/assignment',dict(assign_idem,action='move',target_computer='pc-a',target_slot=9))
            assert assign_reused[0]==409 and assign_reused[1].get('code')=='REQUEST_ID_REUSED', assign_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=1").fetchone()[0] is None
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=2").fetchone()[0]=='account-b'
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute("INSERT INTO publishing_layout VALUES('synthetic-batch','pc-a',3,NULL,'合成待匹配格','','待匹配3','','需核对')")
                raw_db.commit()
            link_fail=dict(batch='synthetic-batch',computer_id='pc-a',slot=3,expected_account_id=None,action='不是操作',account_id='account-a',request_id='94949494-9494-4949-8949-949494949494',_expected_versions={})
            link_fail_first=post(employee_client,'/api/publishing/link',link_fail)
            assert link_fail_first[0]==400 and '关联操作不正确' in link_fail_first[1].get('error',''), link_fail_first
            assert post(employee_client,'/api/publishing/link',link_fail)==link_fail_first
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=3").fetchone()[0] is None
                assert raw_db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=3").fetchone()[0]==0
            missing_link=post(employee_client,'/api/publishing/link',dict(batch='synthetic-batch',computer_id='pc-a',slot=3,expected_account_id=None,action='link',account_id='account-a',_expected_versions={}))
            assert missing_link[0]==400 and missing_link[1].get('code')=='REQUEST_ID_REQUIRED', missing_link
            blank_link=post(employee_client,'/api/publishing/link',dict(batch='synthetic-batch',computer_id='pc-a',slot=3,expected_account_id=None,action='link',account_id='account-a',request_id='',_expected_versions={}))
            assert blank_link[0]==400 and blank_link[1].get('code')=='REQUEST_ID_REQUIRED', blank_link
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=3").fetchone()[0] is None
                assert raw_db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=3").fetchone()[0]==0
            link_idem=dict(batch='synthetic-batch',computer_id='pc-a',slot=3,expected_account_id=None,action='link',account_id='account-a',request_id='93939393-9393-4939-8939-939393939393',_expected_versions={})
            link_first=post(employee_client,'/api/publishing/link',link_idem)
            assert link_first[0]==200 and link_first[1].get('ok') is True and link_first[1].get('account_id')=='account-a' and link_first[1].get('source_cell')=='待匹配3', link_first
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute("UPDATE publishing_layout_links SET updated_at=1 WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=3")
                raw_db.commit()
            link_again=post(employee_client,'/api/publishing/link',link_idem)
            assert link_again[0]==200 and link_again[1].get('account_id')=='account-a', link_again
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=3").fetchone()[0]=='account-a'
                assert raw_db.execute("SELECT updated_at FROM publishing_layout_links WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=3").fetchone()[0]==1
            link_reused=post(employee_client,'/api/publishing/link',dict(link_idem,action='clear'))
            assert link_reused[0]==409 and link_reused[1].get('code')=='REQUEST_ID_REUSED', link_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=3").fetchone()[0]=='account-a'
                assert raw_db.execute("SELECT updated_at FROM publishing_layout_links WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=3").fetchone()[0]==1
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=1").fetchone()[0] is None
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=2").fetchone()[0]=='account-b'
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute("INSERT INTO publishing_layout VALUES('synthetic-batch','pc-a',4,NULL,'合成并发格','','待匹配4','','需核对')")
                raw_db.commit()
                child_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成范围内配对小号'").fetchone()[0]
            layout_key='layout:synthetic-batch:pc-a:4'
            layout_before=get(owner_client,'/api/board')['record_versions'][layout_key]
            with runtime.connect() as raw_db:
                layout_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(layout_key,)).fetchone()[0]
            barrier_layout=threading.Barrier(2, timeout=5)
            def race_layout(client, account_id, request_id):
                barrier_layout.wait()
                return post(client,'/api/publishing/link',dict(batch='synthetic-batch',computer_id='pc-a',slot=4,expected_account_id=None,action='link',account_id=account_id,_expected_versions={layout_key:layout_before},request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                layout_raced=[future.result(timeout=20) for future in (executor.submit(race_layout,owner_client,new['id'],'d7d7d7d7-d7d7-4d7d-8d7d-d7d7d7d7d7d7'),executor.submit(race_layout,employee_client,child_id,'d8d8d8d8-d8d8-4d8d-8d8d-d8d8d8d8d8d8'))]
            assert sorted(item[0] for item in layout_raced)==[200,409], layout_raced
            with runtime.connect() as raw_db:
                laid=raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=4").fetchone()[0]
                assert laid in (new['id'],child_id)
                assert raw_db.execute("SELECT count(*) FROM publishing_layout WHERE batch='synthetic-batch' AND account_id=?",(new['id'] if laid==child_id else child_id,)).fetchone()[0]==0
                assert raw_db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=4").fetchone()[0]==1
                assert raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(layout_key,)).fetchone()[0]==layout_history+1
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=3").fetchone()[0]=='account-a'
            assert get(owner_client,'/api/board')['record_versions'][layout_key]!=layout_before
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute("INSERT INTO publishing_layout VALUES('synthetic-batch','pc-a',10,?,'合成位置来源','','','','')",(race_joined['id'],))
                raw_db.execute("INSERT INTO publishing_layout VALUES('synthetic-batch','pc-a',11,NULL,'','','','','')")
                raw_db.commit()
            assign_source='layout:synthetic-batch:pc-a:10'
            assign_target='layout:synthetic-batch:pc-a:11'
            assign_board=get(owner_client,'/api/board')
            assign_race_versions={assign_source:assign_board['record_versions'][assign_source],assign_target:assign_board['record_versions'][assign_target]}
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=10").fetchone()[0]==race_joined['id']
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=11").fetchone()[0] is None
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=1").fetchone()[0] is None
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=2").fetchone()[0]=='account-b'
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=3").fetchone()[0]=='account-a'
                assign_source_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(assign_source,)).fetchone()[0]
                assign_target_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(assign_target,)).fetchone()[0]
                assign_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment' AND target=''").fetchone()[0]
            barrier_assign=threading.Barrier(2, timeout=5)
            def race_assign(client, request_id):
                barrier_assign.wait()
                return post(client,'/api/publishing/assignment',dict(batch='synthetic-batch',computer_id='pc-a',slot=10,action='move',target_computer='pc-a',target_slot=11,_expected_versions=dict(assign_race_versions),request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                assign_owner,assign_employee=[future.result(timeout=20) for future in (executor.submit(race_assign,owner_client,'b911b911-b911-4b91-8b91-b911b911b911'),executor.submit(race_assign,employee_client,'b922b922-b922-4b92-8b92-b922b922b922'))]
            assert sorted(item[0] for item in (assign_owner,assign_employee))==[200,409], tuple((item[0], item[1].get('code'), item[1].get('error'), item[1].get('ok')) for item in (assign_owner,assign_employee))
            assert next(item for item in (assign_owner,assign_employee) if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            assign_move_winner='test_owner' if assign_owner[0]==200 else 'synthetic_employee'
            assign_move_loser='synthetic_employee' if assign_move_winner=='test_owner' else 'test_owner'
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=10").fetchone()[0] is None
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=11").fetchone()[0]==race_joined['id']
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=1").fetchone()[0] is None
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=2").fetchone()[0]=='account-b'
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=3").fetchone()[0]=='account-a'
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                assign_source_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(assign_source,)).fetchall()
                assign_target_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(assign_target,)).fetchall()
                assign_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/publishing/assignment' AND target='' ORDER BY id").fetchall()
            assign_source_added=[row['actor_name'] for row in assign_source_history[assign_source_history_before:]]
            assign_target_added=[row['actor_name'] for row in assign_target_history[assign_target_history_before:]]
            assign_audited=[row['actor_name'] for row in assign_audit[assign_audit_before:]]
            assert assign_source_added==[names[assign_move_winner]] and names[assign_move_loser] not in assign_source_added, assign_source_added
            assert assign_target_added==[names[assign_move_winner]] and names[assign_move_loser] not in assign_target_added, assign_target_added
            assert assign_audited==[names[assign_move_winner]] and names[assign_move_loser] not in assign_audited, assign_audited
            after_assign_race=get(owner_client,'/api/board')
            assert after_assign_race['record_versions'][assign_source]!=assign_race_versions[assign_source]
            assert after_assign_race['record_versions'][assign_target]!=assign_race_versions[assign_target]
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute("UPDATE phone_devices SET state='故障' WHERE id='8'")
                raw_db.commit()
            broken_phone=get(employee_client,'/api/board')
            phone_account='account:'+patch_id
            bad_phone=dict(id=patch_id,phone_device_id='8',request_id='96969696-9696-4969-8969-969696969696',_expected_versions={phone_account:broken_phone['record_versions'][phone_account],'device:8':broken_phone['record_versions']['device:8']})
            bad_phone_first=post(employee_client,'/api/account/phone-device',bad_phone)
            assert bad_phone_first[0]==400 and '该手机标记为故障，请先核对设备状态' in bad_phone_first[1].get('error',''), bad_phone_first
            assert post(employee_client,'/api/account/phone-device',bad_phone)==bad_phone_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT phone_device_id FROM accounts WHERE id=?',(patch_id,)).fetchone()[0] is None
                assert raw_db.execute("SELECT state FROM phone_devices WHERE id='8'").fetchone()[0]=='故障'
                raw_db.begin_write()
                raw_db.execute("UPDATE phone_devices SET state='在用' WHERE id='8'")
                raw_db.commit()
            ready_phone=get(employee_client,'/api/board')
            phone_versions={phone_account:ready_phone['record_versions'][phone_account],'device:8':ready_phone['record_versions']['device:8']}
            missing_phone=post(employee_client,'/api/account/phone-device',dict(id=patch_id,phone_device_id='8',_expected_versions=phone_versions))
            assert missing_phone[0]==400 and missing_phone[1].get('code')=='REQUEST_ID_REQUIRED', missing_phone
            blank_phone=post(employee_client,'/api/account/phone-device',dict(id=patch_id,phone_device_id='8',request_id='',_expected_versions=phone_versions))
            assert blank_phone[0]==400 and blank_phone[1].get('code')=='REQUEST_ID_REQUIRED', blank_phone
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT phone_device_id FROM accounts WHERE id=?',(patch_id,)).fetchone()[0] is None
            phone_idem=dict(id=patch_id,phone_device_id='8',request_id='95959595-9595-4959-8959-959595959595',_expected_versions=phone_versions)
            phone_first=post(employee_client,'/api/account/phone-device',phone_idem)
            assert phone_first[0]==200 and phone_first[1].get('ok') is True, phone_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT phone_device_id FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]=='8'
                raw_db.begin_write()
                raw_db.execute('UPDATE accounts SET phone_device_id=NULL WHERE id=?',(patch_id,))
                raw_db.commit()
            phone_again=post(employee_client,'/api/account/phone-device',phone_idem)
            assert phone_again[0]==200 and phone_again[1].get('ok') is True, phone_again
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT phone_device_id FROM accounts WHERE id=?',(patch_id,)).fetchone()[0] is None
            phone_reused=post(employee_client,'/api/account/phone-device',dict(phone_idem,phone_device_id=None))
            assert phone_reused[0]==409 and phone_reused[1].get('code')=='REQUEST_ID_REUSED', phone_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT phone_device_id FROM accounts WHERE id=?',(patch_id,)).fetchone()[0] is None
                assert raw_db.execute("SELECT state FROM phone_devices WHERE id='8'").fetchone()[0]=='在用'
            with runtime.connect() as raw_db:
                readiness_before=raw_db.execute('SELECT card_stability,password_state FROM accounts WHERE id=?',(patch_id,)).fetchone()
            ready_readiness=get(employee_client,'/api/board')
            readiness_key='account:'+patch_id
            bad_readiness=dict(id=patch_id,card_stability='不是状态',password_state='待确认',request_id='98989898-9898-4989-8989-989898989898',_expected_versions={readiness_key:ready_readiness['record_versions'][readiness_key]})
            bad_readiness_first=post(employee_client,'/api/account/card-readiness',bad_readiness)
            assert bad_readiness_first[0]==400 and '账号稳定或改密状态不正确' in bad_readiness_first[1].get('error',''), bad_readiness_first
            assert post(employee_client,'/api/account/card-readiness',bad_readiness)==bad_readiness_first
            with runtime.connect() as raw_db:
                stayed=raw_db.execute('SELECT card_stability,password_state FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert stayed[0]==readiness_before[0] and stayed[1]==readiness_before[1]
            after_bad_readiness=get(employee_client,'/api/board')
            readiness_versions={readiness_key:after_bad_readiness['record_versions'][readiness_key]}
            missing_readiness=post(employee_client,'/api/account/card-readiness',dict(id=patch_id,card_stability='已稳定',password_state='未修改',_expected_versions=readiness_versions))
            assert missing_readiness[0]==400 and missing_readiness[1].get('code')=='REQUEST_ID_REQUIRED', missing_readiness
            blank_readiness=post(employee_client,'/api/account/card-readiness',dict(id=patch_id,card_stability='已稳定',password_state='未修改',request_id='',_expected_versions=readiness_versions))
            assert blank_readiness[0]==400 and blank_readiness[1].get('code')=='REQUEST_ID_REQUIRED', blank_readiness
            with runtime.connect() as raw_db:
                stayed_missing=raw_db.execute('SELECT card_stability,password_state FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert stayed_missing[0]==readiness_before[0] and stayed_missing[1]==readiness_before[1]
            readiness_idem=dict(id=patch_id,card_stability='已稳定',password_state='未修改',request_id='97979797-9797-4979-8979-979797979797',_expected_versions=readiness_versions)
            readiness_first=post(employee_client,'/api/account/card-readiness',readiness_idem)
            assert readiness_first[0]==200 and readiness_first[1].get('ok') is True, readiness_first
            with runtime.connect() as raw_db:
                saved=raw_db.execute('SELECT card_stability,password_state FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert saved[0]=='已稳定' and saved[1]=='未修改'
                raw_db.begin_write()
                raw_db.execute('UPDATE accounts SET card_stability=?,password_state=? WHERE id=?',(readiness_before[0],readiness_before[1],patch_id))
                raw_db.commit()
            readiness_again=post(employee_client,'/api/account/card-readiness',readiness_idem)
            assert readiness_again[0]==200 and readiness_again[1].get('ok') is True, readiness_again
            with runtime.connect() as raw_db:
                replayed=raw_db.execute('SELECT card_stability,password_state FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert replayed[0]==readiness_before[0] and replayed[1]==readiness_before[1]
            readiness_reused=post(employee_client,'/api/account/card-readiness',dict(readiness_idem,password_state='已修改'))
            assert readiness_reused[0]==409 and readiness_reused[1].get('code')=='REQUEST_ID_REUSED', readiness_reused
            with runtime.connect() as raw_db:
                kept=raw_db.execute('SELECT card_stability,password_state FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert kept[0]==readiness_before[0] and kept[1]==readiness_before[1]
            readiness_race_board=get(owner_client,'/api/board')
            readiness_race_version=readiness_race_board['record_versions'][readiness_key]
            with runtime.connect() as raw_db:
                readiness_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(readiness_key,)).fetchone()[0]
                readiness_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/card-readiness' AND target=''").fetchone()[0]
            barrier_readiness=threading.Barrier(2, timeout=5)
            def race_readiness(client, stable, password, request_id):
                barrier_readiness.wait()
                return post(client,'/api/account/card-readiness',dict(id=patch_id,card_stability=stable,password_state=password,_expected_versions={readiness_key:readiness_race_version},request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                readiness_raced=[future.result(timeout=20) for future in (executor.submit(race_readiness,owner_client,'未稳定','未修改','ab11ab11-ab11-4ab1-8ab1-ab11ab11ab11'),executor.submit(race_readiness,employee_client,'已稳定','已修改','ab22ab22-ab22-4ab2-8ab2-ab22ab22ab22'))]
            assert sorted(item[0] for item in readiness_raced)==[200,409], readiness_raced
            assert next(item for item in readiness_raced if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            with runtime.connect() as raw_db:
                raced_state=raw_db.execute('SELECT card_stability,password_state FROM accounts WHERE id=?',(patch_id,)).fetchone()
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                readiness_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(readiness_key,)).fetchall()
                readiness_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/account/card-readiness' AND target='' ORDER BY id").fetchall()
            readiness_winner='test_owner' if (raced_state[0],raced_state[1])==('未稳定','未修改') else 'synthetic_employee'
            assert (raced_state[0],raced_state[1]) in (('未稳定','未修改'),('已稳定','已修改'))
            readiness_loser='synthetic_employee' if readiness_winner=='test_owner' else 'test_owner'
            readiness_added=[row['actor_name'] for row in readiness_history[readiness_history_before:]]
            readiness_audited=[row['actor_name'] for row in readiness_audit[readiness_audit_before:]]
            assert readiness_added==[names[readiness_winner]] and names[readiness_loser] not in readiness_added, (raced_state[0],readiness_added)
            assert readiness_audited==[names[readiness_winner]] and names[readiness_loser] not in readiness_audited, (raced_state[0],readiness_audited)
            assert get(owner_client,'/api/board')['record_versions'][readiness_key]!=readiness_race_version
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute('UPDATE accounts SET card_stability=?,password_state=? WHERE id=?',(readiness_before[0],readiness_before[1],patch_id))
                raw_db.commit()
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute("UPDATE accounts SET account_type='大号' WHERE id=?",(patch_id,))
                raw_db.commit()
                lifecycle_before=raw_db.execute('SELECT auxiliary_state FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]
            ready_lifecycle=get(employee_client,'/api/board')
            lifecycle_key='account:'+patch_id
            bad_lifecycle=dict(id=patch_id,auxiliary_state='不是状态',request_id='a2a2a2a2-a2a2-4a2a-8a2a-a2a2a2a2a2a2',_expected_versions={lifecycle_key:ready_lifecycle['record_versions'][lifecycle_key]})
            bad_lifecycle_first=post(employee_client,'/api/lifecycle-status',bad_lifecycle)
            assert bad_lifecycle_first[0]==400 and '账号类型或辅助账号注册状态不正确' in bad_lifecycle_first[1].get('error',''), bad_lifecycle_first
            assert post(employee_client,'/api/lifecycle-status',bad_lifecycle)==bad_lifecycle_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT auxiliary_state FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==lifecycle_before
            after_bad_lifecycle=get(employee_client,'/api/board')
            lifecycle_versions={lifecycle_key:after_bad_lifecycle['record_versions'][lifecycle_key]}
            missing_lifecycle=post(employee_client,'/api/lifecycle-status',dict(id=patch_id,auxiliary_state='已注册',_expected_versions=lifecycle_versions))
            assert missing_lifecycle[0]==400 and missing_lifecycle[1].get('code')=='REQUEST_ID_REQUIRED', missing_lifecycle
            blank_lifecycle=post(employee_client,'/api/lifecycle-status',dict(id=patch_id,auxiliary_state='已注册',request_id='',_expected_versions=lifecycle_versions))
            assert blank_lifecycle[0]==400 and blank_lifecycle[1].get('code')=='REQUEST_ID_REQUIRED', blank_lifecycle
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT auxiliary_state FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==lifecycle_before
            lifecycle_idem=dict(id=patch_id,auxiliary_state='已注册',request_id='a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1',_expected_versions=lifecycle_versions)
            lifecycle_first=post(employee_client,'/api/lifecycle-status',lifecycle_idem)
            assert lifecycle_first[0]==200 and lifecycle_first[1].get('ok') is True, lifecycle_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT auxiliary_state FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]=='已注册'
                raw_db.begin_write()
                raw_db.execute('UPDATE accounts SET auxiliary_state=? WHERE id=?',(lifecycle_before,patch_id))
                raw_db.commit()
            lifecycle_again=post(employee_client,'/api/lifecycle-status',lifecycle_idem)
            assert lifecycle_again[0]==200 and lifecycle_again[1].get('ok') is True, lifecycle_again
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT auxiliary_state FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==lifecycle_before
            lifecycle_reused=post(employee_client,'/api/lifecycle-status',dict(lifecycle_idem,auxiliary_state='暂无法注册'))
            assert lifecycle_reused[0]==409 and lifecycle_reused[1].get('code')=='REQUEST_ID_REUSED', lifecycle_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT auxiliary_state,account_type FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==lifecycle_before
                assert raw_db.execute('SELECT account_type FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]=='大号'
                assert raw_db.execute('SELECT 1 FROM accounts WHERE parent_account_id=?',(patch_id,)).fetchone() is None
            lifecycle_race_board=get(owner_client,'/api/board')
            lifecycle_race_version=lifecycle_race_board['record_versions'][lifecycle_key]
            with runtime.connect() as raw_db:
                lifecycle_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(lifecycle_key,)).fetchone()[0]
                lifecycle_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/lifecycle-status' AND target=''").fetchone()[0]
            barrier_lifecycle=threading.Barrier(2, timeout=5)
            def race_lifecycle(client, state, request_id):
                barrier_lifecycle.wait()
                return post(client,'/api/lifecycle-status',dict(id=patch_id,auxiliary_state=state,_expected_versions={lifecycle_key:lifecycle_race_version},request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                lifecycle_raced=[future.result(timeout=20) for future in (executor.submit(race_lifecycle,owner_client,'未注册','c11c11c1-c11c-411c-811c-c11c11c11c11'),executor.submit(race_lifecycle,employee_client,'暂无法注册','c22c22c2-c22c-422c-822c-c22c22c22c22'))]
            assert sorted(item[0] for item in lifecycle_raced)==[200,409], lifecycle_raced
            assert next(item for item in lifecycle_raced if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            with runtime.connect() as raw_db:
                raced_aux=raw_db.execute('SELECT auxiliary_state,account_type FROM accounts WHERE id=?',(patch_id,)).fetchone()
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                lifecycle_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(lifecycle_key,)).fetchall()
                lifecycle_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/lifecycle-status' AND target='' ORDER BY id").fetchall()
            assert raced_aux[1]=='大号' and raced_aux[0] in ('未注册','暂无法注册') and raced_aux[0]!=lifecycle_before
            lifecycle_winner='test_owner' if raced_aux[0]=='未注册' else 'synthetic_employee'
            lifecycle_loser='synthetic_employee' if lifecycle_winner=='test_owner' else 'test_owner'
            lifecycle_added=[row['actor_name'] for row in lifecycle_history[lifecycle_history_before:]]
            lifecycle_audited=[row['actor_name'] for row in lifecycle_audit[lifecycle_audit_before:]]
            assert lifecycle_added==[names[lifecycle_winner]] and names[lifecycle_loser] not in lifecycle_added, (raced_aux[0],lifecycle_added)
            assert lifecycle_audited==[names[lifecycle_winner]] and names[lifecycle_loser] not in lifecycle_audited, (raced_aux[0],lifecycle_audited)
            assert get(owner_client,'/api/board')['record_versions'][lifecycle_key]!=lifecycle_race_version
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute('UPDATE accounts SET auxiliary_state=? WHERE id=?',(lifecycle_before,patch_id))
                raw_db.commit()
            ready_daily=get(employee_client,'/api/board')
            daily_day='2026-10-22'
            daily_account='account:'+patch_id
            daily_key='daily:'+patch_id+':'+daily_day
            daily_versions={daily_account:ready_daily['record_versions'][daily_account],daily_key:ready_daily['record_versions'].get(daily_key)}
            missing_daily=post(employee_client,'/api/daily',dict(id=patch_id,date=daily_day,published=True,_expected_versions=daily_versions))
            assert missing_daily[0]==400 and missing_daily[1].get('code')=='REQUEST_ID_REQUIRED', missing_daily
            blank_daily=post(employee_client,'/api/daily',dict(id=patch_id,date=daily_day,published=True,request_id='',_expected_versions=daily_versions))
            assert blank_daily[0]==400 and blank_daily[1].get('code')=='REQUEST_ID_REQUIRED', blank_daily
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT 1 FROM daily WHERE account_id=? AND day=?',(patch_id,daily_day)).fetchone() is None
            bad_daily=dict(id=patch_id,date=daily_day,published=1,request_id='b2b2b2b2-b2b2-4b2b-8b2b-b2b2b2b2b2b2',_expected_versions=daily_versions)
            bad_daily_first=post(employee_client,'/api/daily',bad_daily)
            assert bad_daily_first[0]==400 and '发布标记应为布尔值' in bad_daily_first[1].get('error',''), bad_daily_first
            assert post(employee_client,'/api/daily',bad_daily)==bad_daily_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT 1 FROM daily WHERE account_id=? AND day=?',(patch_id,daily_day)).fetchone() is None
            after_bad_daily=get(employee_client,'/api/board')
            daily_idem=dict(id=patch_id,date=daily_day,published=True,request_id='b1b1b1b1-b1b1-4b1b-8b1b-b1b1b1b1b1b1',_expected_versions={daily_account:after_bad_daily['record_versions'][daily_account],daily_key:after_bad_daily['record_versions'].get(daily_key)})
            daily_first=post(employee_client,'/api/daily',daily_idem)
            assert daily_first[0]==200 and daily_first[1].get('ok') is True, daily_first
            with runtime.connect() as raw_db:
                assert int(raw_db.execute('SELECT published FROM daily WHERE account_id=? AND day=?',(patch_id,daily_day)).fetchone()[0])==1
                raw_db.begin_write()
                raw_db.execute('UPDATE daily SET published=0 WHERE account_id=? AND day=?',(patch_id,daily_day))
                raw_db.commit()
            daily_again=post(employee_client,'/api/daily',daily_idem)
            assert daily_again[0]==200 and daily_again[1].get('ok') is True, daily_again
            with runtime.connect() as raw_db:
                assert int(raw_db.execute('SELECT published FROM daily WHERE account_id=? AND day=?',(patch_id,daily_day)).fetchone()[0])==0
            daily_reused=post(employee_client,'/api/daily',dict(daily_idem,published=False))
            assert daily_reused[0]==409 and daily_reused[1].get('code')=='REQUEST_ID_REUSED', daily_reused
            with runtime.connect() as raw_db:
                assert int(raw_db.execute('SELECT published FROM daily WHERE account_id=? AND day=?',(patch_id,daily_day)).fetchone()[0])==0
                bulk_before=raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]
                assert bulk_before!='封禁', bulk_before
                account_a_status=raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]
            ready_bulk=get(employee_client,'/api/board')
            bulk_account='account:'+patch_id
            bad_bulk=dict(ids=[patch_id],status='不是状态',request_id='c2c2c2c2-c2c2-4c2c-8c2c-c2c2c2c2c2c2',_expected_versions={bulk_account:ready_bulk['record_versions'][bulk_account]})
            bad_bulk_first=post(employee_client,'/api/bulk-status',bad_bulk)
            assert bad_bulk_first[0]==400 and '请选择账号及状态' in bad_bulk_first[1].get('error',''), bad_bulk_first
            assert post(employee_client,'/api/bulk-status',bad_bulk)==bad_bulk_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==bulk_before
            after_bad_bulk=get(employee_client,'/api/board')
            bulk_versions={bulk_account:after_bad_bulk['record_versions'][bulk_account]}
            missing_bulk=post(employee_client,'/api/bulk-status',dict(ids=[patch_id],status='封禁',_expected_versions=bulk_versions))
            assert missing_bulk[0]==400 and missing_bulk[1].get('code')=='REQUEST_ID_REQUIRED', missing_bulk
            blank_bulk=post(employee_client,'/api/bulk-status',dict(ids=[patch_id],status='封禁',request_id='',_expected_versions=bulk_versions))
            assert blank_bulk[0]==400 and blank_bulk[1].get('code')=='REQUEST_ID_REQUIRED', blank_bulk
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==bulk_before
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]==account_a_status
            bulk_idem=dict(ids=[patch_id],status='封禁',request_id='c1c1c1c1-c1c1-4c1c-8c1c-c1c1c1c1c1c1',_expected_versions=bulk_versions)
            bulk_first=post(employee_client,'/api/bulk-status',bulk_idem)
            assert bulk_first[0]==200 and bulk_first[1].get('ok') is True, bulk_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]=='封禁'
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]==account_a_status
                raw_db.begin_write()
                raw_db.execute('UPDATE accounts SET status=? WHERE id=?',(bulk_before,patch_id))
                raw_db.commit()
            bulk_again=post(employee_client,'/api/bulk-status',bulk_idem)
            assert bulk_again[0]==200 and bulk_again[1].get('ok') is True, bulk_again
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==bulk_before
            bulk_reused=post(employee_client,'/api/bulk-status',dict(bulk_idem,status='正常'))
            assert bulk_reused[0]==409 and bulk_reused[1].get('code')=='REQUEST_ID_REUSED', bulk_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==bulk_before
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]==account_a_status
            assert bulk_before not in ('正常','发不了')
            bulk_race_board=get(owner_client,'/api/board')
            bulk_race_version=bulk_race_board['record_versions'][bulk_account]
            other_account='account:account-a'
            other_version=bulk_race_board['record_versions'][other_account]
            with runtime.connect() as raw_db:
                bulk_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(bulk_account,)).fetchone()[0]
                other_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(other_account,)).fetchone()[0]
                bulk_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/bulk-status' AND target=''").fetchone()[0]
            barrier_bulk=threading.Barrier(2, timeout=5)
            def race_bulk(client, state, request_id):
                barrier_bulk.wait()
                return post(client,'/api/bulk-status',dict(ids=[patch_id],status=state,_expected_versions={bulk_account:bulk_race_version},request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                bulk_raced=[future.result(timeout=20) for future in (executor.submit(race_bulk,owner_client,'正常','e11e11e1-e11e-411e-811e-e11e11e11e11'),executor.submit(race_bulk,employee_client,'发不了','e22e22e2-e22e-422e-822e-e22e22e22e22'))]
            assert sorted(item[0] for item in bulk_raced)==[200,409], bulk_raced
            assert next(item for item in bulk_raced if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            with runtime.connect() as raw_db:
                raced_bulk=raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]
                raced_other=raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                bulk_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(bulk_account,)).fetchall()
                other_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(other_account,)).fetchone()[0]
                bulk_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/bulk-status' AND target='' ORDER BY id").fetchall()
            assert raced_bulk in ('正常','发不了') and raced_bulk!=bulk_before and raced_other==account_a_status
            bulk_winner='test_owner' if raced_bulk=='正常' else 'synthetic_employee'
            bulk_loser='synthetic_employee' if bulk_winner=='test_owner' else 'test_owner'
            bulk_added=[row['actor_name'] for row in bulk_history[bulk_history_before:]]
            bulk_audited=[row['actor_name'] for row in bulk_audit[bulk_audit_before:]]
            assert bulk_added==[names[bulk_winner]] and names[bulk_loser] not in bulk_added, (raced_bulk,bulk_added)
            assert bulk_audited==[names[bulk_winner]] and names[bulk_loser] not in bulk_audited, (raced_bulk,bulk_audited)
            assert other_history==other_history_before
            after_bulk_race=get(owner_client,'/api/board')
            assert after_bulk_race['record_versions'][bulk_account]!=bulk_race_version
            assert after_bulk_race['record_versions'][other_account]==other_version
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute('UPDATE accounts SET status=? WHERE id=?',(bulk_before,patch_id))
                raw_db.commit()
            assert bulk_before!='短期封禁', bulk_before
            ready_batch=get(employee_client,'/api/board')
            batch_account='account:'+patch_id
            bad_batch=dict(kind='accounts',ids=[patch_id],patch={'status':'不是状态'},request_id='d2d2d2d2-d2d2-4d2d-8d2d-d2d2d2d2d2d2',_expected_versions={batch_account:ready_batch['record_versions'][batch_account]})
            bad_batch_first=post(employee_client,'/api/batch-edit',bad_batch)
            assert bad_batch_first[0]==400 and '账号状态不正确' in bad_batch_first[1].get('error',''), bad_batch_first
            assert post(employee_client,'/api/batch-edit',bad_batch)==bad_batch_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==bulk_before
            after_bad_batch=get(employee_client,'/api/board')
            batch_versions={batch_account:after_bad_batch['record_versions'][batch_account]}
            missing_batch=post(employee_client,'/api/batch-edit',dict(kind='accounts',ids=[patch_id],patch={'status':'短期封禁'},_expected_versions=batch_versions))
            assert missing_batch[0]==400 and missing_batch[1].get('code')=='REQUEST_ID_REQUIRED', missing_batch
            blank_batch=post(employee_client,'/api/batch-edit',dict(kind='accounts',ids=[patch_id],patch={'status':'短期封禁'},request_id='',_expected_versions=batch_versions))
            assert blank_batch[0]==400 and blank_batch[1].get('code')=='REQUEST_ID_REQUIRED', blank_batch
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==bulk_before
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]==account_a_status
            batch_idem=dict(kind='accounts',ids=[patch_id],patch={'status':'短期封禁'},request_id='d1d1d1d1-d1d1-4d1d-8d1d-d1d1d1d1d1d1',_expected_versions=batch_versions)
            batch_first=post(employee_client,'/api/batch-edit',batch_idem)
            assert batch_first[0]==200 and batch_first[1].get('ok') is True and batch_first[1].get('count')==1, batch_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]=='短期封禁'
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]==account_a_status
                raw_db.begin_write()
                raw_db.execute('UPDATE accounts SET status=? WHERE id=?',(bulk_before,patch_id))
                raw_db.commit()
            batch_again=post(employee_client,'/api/batch-edit',batch_idem)
            assert batch_again[0]==200 and batch_again[1].get('count')==1, batch_again
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==bulk_before
            batch_reused=post(employee_client,'/api/batch-edit',dict(batch_idem,patch={'status':'正常'}))
            assert batch_reused[0]==409 and batch_reused[1].get('code')=='REQUEST_ID_REUSED', batch_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==bulk_before
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]==account_a_status
            assert bulk_before not in ('待确认','发不了')
            batch_race_board=get(owner_client,'/api/board')
            batch_race_version=batch_race_board['record_versions'][batch_account]
            batch_other='account:account-a'
            batch_other_version=batch_race_board['record_versions'][batch_other]
            with runtime.connect() as raw_db:
                batch_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(batch_account,)).fetchone()[0]
                batch_other_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(batch_other,)).fetchone()[0]
                batch_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit' AND target=''").fetchone()[0]
            barrier_batch=threading.Barrier(2, timeout=5)
            def race_batch(client, state, request_id):
                barrier_batch.wait()
                return post(client,'/api/batch-edit',dict(kind='accounts',ids=[patch_id],patch={'status':state},_expected_versions={batch_account:batch_race_version},request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                batch_raced=[future.result(timeout=20) for future in (executor.submit(race_batch,owner_client,'待确认','f11f11f1-f11f-411f-811f-f11f11f11f11'),executor.submit(race_batch,employee_client,'发不了','f22f22f2-f22f-422f-822f-f22f22f22f22'))]
            assert sorted(item[0] for item in batch_raced)==[200,409], batch_raced
            assert next(item for item in batch_raced if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            with runtime.connect() as raw_db:
                raced_batch=raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]
                raced_batch_other=raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                batch_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(batch_account,)).fetchall()
                batch_other_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(batch_other,)).fetchone()[0]
                batch_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/batch-edit' AND target='' ORDER BY id").fetchall()
            assert raced_batch in ('待确认','发不了') and raced_batch!=bulk_before and raced_batch_other==account_a_status
            batch_winner='test_owner' if raced_batch=='待确认' else 'synthetic_employee'
            batch_loser='synthetic_employee' if batch_winner=='test_owner' else 'test_owner'
            batch_added=[row['actor_name'] for row in batch_history[batch_history_before:]]
            batch_audited=[row['actor_name'] for row in batch_audit[batch_audit_before:]]
            assert batch_added==[names[batch_winner]] and names[batch_loser] not in batch_added, (raced_batch,batch_added)
            assert batch_audited==[names[batch_winner]] and names[batch_loser] not in batch_audited, (raced_batch,batch_audited)
            assert batch_other_history==batch_other_history_before
            after_batch_race=get(owner_client,'/api/board')
            assert after_batch_race['record_versions'][batch_account]!=batch_race_version
            assert after_batch_race['record_versions'][batch_other]==batch_other_version
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute('UPDATE accounts SET status=? WHERE id=?',(bulk_before,patch_id))
                raw_db.commit()
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==bulk_before
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]==account_a_status
                save_row=raw_db.execute('SELECT name,status,note,computer_id,slot,douyin_id,homepage FROM accounts WHERE id=?',(patch_id,)).fetchone()
            save_note=save_row[2]
            ready_save=get(employee_client,'/api/board')
            save_account='account:'+patch_id
            bad_save=dict(id=patch_id,name=save_row[0],status='不是状态',computer_id=save_row[3],slot=int(save_row[4]),note=save_note,douyin_id=save_row[5] or '',homepage=save_row[6] or '',request_id='e2e2e2e2-e2e2-4e2e-8e2e-e2e2e2e2e2e2',_expected_versions={save_account:ready_save['record_versions'][save_account]})
            bad_save_first=post(employee_client,'/api/account',bad_save)
            assert bad_save_first[0]==400 and '账号状态不正确' in bad_save_first[1].get('error',''), bad_save_first
            assert post(employee_client,'/api/account',bad_save)==bad_save_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT note,status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==save_note
            after_bad_save=get(employee_client,'/api/board')
            save_idem=dict(id=patch_id,name=save_row[0],status=save_row[1],computer_id=save_row[3],slot=int(save_row[4]),note='合成完整保存备注',douyin_id=save_row[5] or '',homepage=save_row[6] or '',request_id='e1e1e1e1-e1e1-4e1e-8e1e-e1e1e1e1e1e1',_expected_versions={save_account:after_bad_save['record_versions'][save_account]})
            save_first=post(employee_client,'/api/account',save_idem)
            assert save_first[0]==200 and save_first[1].get('ok') is True and save_first[1].get('id')==patch_id, save_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT note FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]=='合成完整保存备注'
                assert raw_db.execute('SELECT name,status,computer_id FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==save_row[0]
                kept_slot=raw_db.execute('SELECT slot FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]
                assert int(kept_slot)==int(save_row[4])
                raw_db.begin_write()
                raw_db.execute('UPDATE accounts SET note=? WHERE id=?',(save_note,patch_id))
                raw_db.commit()
            save_again=post(employee_client,'/api/account',save_idem)
            assert save_again[0]==200 and save_again[1].get('id')==patch_id, save_again
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT note FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==save_note
            save_reused=post(employee_client,'/api/account',dict(save_idem,note='合成完整保存被拒绝'))
            assert save_reused[0]==409 and save_reused[1].get('code')=='REQUEST_ID_REUSED', save_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT note,status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==save_note
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]==account_a_status
            save_race_board=get(owner_client,'/api/board')
            save_race_version=save_race_board['record_versions'][save_account]
            save_other='account:account-a'
            save_other_version=save_race_board['record_versions'][save_other]
            with runtime.connect() as raw_db:
                save_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(save_account,)).fetchone()[0]
                save_other_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(save_other,)).fetchone()[0]
                save_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?",(patch_id,)).fetchone()[0]
            barrier_save=threading.Barrier(2, timeout=5)
            def race_save(client, note, request_id):
                barrier_save.wait()
                return post(client,'/api/account',dict(id=patch_id,name=save_row[0],status=save_row[1],computer_id=save_row[3],slot=int(save_row[4]),note=note,douyin_id=save_row[5] or '',homepage=save_row[6] or '',_expected_versions={save_account:save_race_version},request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                save_raced=[future.result(timeout=20) for future in (executor.submit(race_save,owner_client,'合成完整并发甲','ac11ac11-ac11-4ac1-8ac1-ac11ac11ac11'),executor.submit(race_save,employee_client,'合成完整并发乙','ac22ac22-ac22-4ac2-8ac2-ac22ac22ac22'))]
            assert sorted(item[0] for item in save_raced)==[200,409], save_raced
            assert next(item for item in save_raced if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            with runtime.connect() as raw_db:
                raced_save=raw_db.execute('SELECT name,status,note,computer_id,slot FROM accounts WHERE id=?',(patch_id,)).fetchone()
                raced_save_other=raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                save_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(save_account,)).fetchall()
                save_other_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(save_other,)).fetchone()[0]
                save_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/account' AND target=? ORDER BY id",(patch_id,)).fetchall()
            assert raced_save[2] in ('合成完整并发甲','合成完整并发乙') and raced_save[2]!=save_note
            assert raced_save[0]==save_row[0] and raced_save[1]==save_row[1] and raced_save[3]==save_row[3] and int(raced_save[4])==int(save_row[4])
            assert raced_save_other==account_a_status
            save_winner='test_owner' if raced_save[2]=='合成完整并发甲' else 'synthetic_employee'
            save_loser='synthetic_employee' if save_winner=='test_owner' else 'test_owner'
            save_added=[row['actor_name'] for row in save_history[save_history_before:]]
            save_audited=[row['actor_name'] for row in save_audit[save_audit_before:]]
            assert save_added==[names[save_winner]] and names[save_loser] not in save_added, (raced_save[2],save_added)
            assert save_audited==[names[save_winner]] and names[save_loser] not in save_audited, (raced_save[2],save_audited)
            assert save_other_history==save_other_history_before
            after_save_race=get(owner_client,'/api/board')
            assert after_save_race['record_versions'][save_account]!=save_race_version
            assert after_save_race['record_versions'][save_other]==save_other_version
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute('UPDATE accounts SET note=? WHERE id=?',(save_note,patch_id))
                raw_db.commit()
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT note,status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==save_note
                nurture_before=raw_db.execute('SELECT nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]
            assert nurture_before!='2026-01-02', nurture_before
            ready_nurture=get(employee_client,'/api/board')
            nurture_account='account:'+patch_id
            missing_nurture=post(employee_client,'/api/account/nurture',dict(id=patch_id,started='2026-01-02',_expected_versions={nurture_account:ready_nurture['record_versions'][nurture_account]}))
            assert missing_nurture[0]==400 and missing_nurture[1].get('code')=='REQUEST_ID_REQUIRED', missing_nurture
            blank_nurture=post(employee_client,'/api/account/nurture',dict(id=patch_id,started='2026-01-02',request_id='',_expected_versions={nurture_account:ready_nurture['record_versions'][nurture_account]}))
            assert blank_nurture[0]==400 and blank_nurture[1].get('code')=='REQUEST_ID_REQUIRED', blank_nurture
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==nurture_before
            bad_nurture=dict(id=patch_id,started='2099-01-01',request_id='f2f2f2f2-f2f2-4f2f-8f2f-f2f2f2f2f2f2',_expected_versions={nurture_account:ready_nurture['record_versions'][nurture_account]})
            bad_nurture_first=post(employee_client,'/api/account/nurture',bad_nurture)
            assert bad_nurture_first[0]==400 and '养号开始日期不能在未来' in bad_nurture_first[1].get('error',''), bad_nurture_first
            assert post(employee_client,'/api/account/nurture',bad_nurture)==bad_nurture_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==nurture_before
            after_bad_nurture=get(employee_client,'/api/board')
            nurture_idem=dict(id=patch_id,started='2026-01-02',request_id='f1f1f1f1-f1f1-4f1f-8f1f-f1f1f1f1f1f1',_expected_versions={nurture_account:after_bad_nurture['record_versions'][nurture_account]})
            nurture_first=post(employee_client,'/api/account/nurture',nurture_idem)
            assert nurture_first[0]==200 and nurture_first[1].get('ok') is True, nurture_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]=='2026-01-02'
                raw_db.begin_write()
                raw_db.execute('UPDATE accounts SET nurture_started=? WHERE id=?',(nurture_before,patch_id))
                raw_db.commit()
            nurture_again=post(employee_client,'/api/account/nurture',nurture_idem)
            assert nurture_again[0]==200 and nurture_again[1].get('ok') is True, nurture_again
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==nurture_before
            nurture_reused=post(employee_client,'/api/account/nurture',dict(nurture_idem,started='2026-01-03'))
            assert nurture_reused[0]==409 and nurture_reused[1].get('code')=='REQUEST_ID_REUSED', nurture_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==nurture_before
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]==account_a_status
            assert nurture_before not in ('2026-01-04','2026-01-05')
            nurture_race_board=get(owner_client,'/api/board')
            nurture_race_version=nurture_race_board['record_versions'][nurture_account]
            nurture_other='account:account-a'
            nurture_other_version=nurture_race_board['record_versions'][nurture_other]
            with runtime.connect() as raw_db:
                nurture_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(nurture_account,)).fetchone()[0]
                nurture_other_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(nurture_other,)).fetchone()[0]
                nurture_audit_before=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/nurture' AND target=''").fetchone()[0]
            barrier_nurture=threading.Barrier(2, timeout=5)
            def race_nurture(client, started, request_id):
                barrier_nurture.wait()
                return post(client,'/api/account/nurture',dict(id=patch_id,started=started,_expected_versions={nurture_account:nurture_race_version},request_id=request_id))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                nurture_raced=[future.result(timeout=20) for future in (executor.submit(race_nurture,owner_client,'2026-01-04','ad11ad11-ad11-4ad1-8ad1-ad11ad11ad11'),executor.submit(race_nurture,employee_client,'2026-01-05','ad22ad22-ad22-4ad2-8ad2-ad22ad22ad22'))]
            assert sorted(item[0] for item in nurture_raced)==[200,409], nurture_raced
            assert next(item for item in nurture_raced if item[0]==409)[1].get('code')!='REQUEST_ID_REUSED'
            with runtime.connect() as raw_db:
                raced_nurture=raw_db.execute('SELECT nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]
                raced_nurture_other=raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]
                names={row['username']:row['name'] for row in raw_db.execute('SELECT username,name FROM auth_users').fetchall()}
                nurture_history=raw_db.execute('SELECT actor_name FROM operations_history WHERE entity=? ORDER BY id',(nurture_account,)).fetchall()
                nurture_other_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(nurture_other,)).fetchone()[0]
                nurture_audit=raw_db.execute("SELECT actor_name FROM auth_audit WHERE action='/api/account/nurture' AND target='' ORDER BY id").fetchall()
            assert raced_nurture in ('2026-01-04','2026-01-05') and raced_nurture!=nurture_before and raced_nurture_other==account_a_status
            nurture_winner='test_owner' if raced_nurture=='2026-01-04' else 'synthetic_employee'
            nurture_loser='synthetic_employee' if nurture_winner=='test_owner' else 'test_owner'
            nurture_added=[row['actor_name'] for row in nurture_history[nurture_history_before:]]
            nurture_audited=[row['actor_name'] for row in nurture_audit[nurture_audit_before:]]
            assert nurture_added==[names[nurture_winner]] and names[nurture_loser] not in nurture_added, (raced_nurture,nurture_added)
            assert nurture_audited==[names[nurture_winner]] and names[nurture_loser] not in nurture_audited, (raced_nurture,nurture_audited)
            assert nurture_other_history==nurture_other_history_before
            after_nurture_race=get(owner_client,'/api/board')
            assert after_nurture_race['record_versions'][nurture_account]!=nurture_race_version
            assert after_nurture_race['record_versions'][nurture_other]==nurture_other_version
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute('UPDATE accounts SET nurture_started=? WHERE id=?',(nurture_before,patch_id))
                raw_db.commit()
            import_phone='19900000077'
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT 1 FROM phone_cards WHERE phone=?',(import_phone,)).fetchone() is None
                raw_db.begin_write()
                raw_db.execute('UPDATE accounts SET registration_phone=? WHERE id=?',(import_phone,patch_id))
                raw_db.commit()
            bad_import=dict(request_id='a4a4a4a4-a4a4-4a4a-8a4a-a4a4a4a4a4a4',_expected_versions={})
            bad_import_first=post(employee_client,'/api/phone-cards/import',bad_import)
            assert bad_import_first[0]==400 and '新增资料必须明确选择办公室' in bad_import_first[1].get('error',''), bad_import_first
            assert post(employee_client,'/api/phone-cards/import',bad_import)==bad_import_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT 1 FROM phone_cards WHERE phone=?',(import_phone,)).fetchone() is None
            missing_import=post(employee_client,'/api/phone-cards/import',dict(office_ids=[office_a],_expected_versions={}))
            assert missing_import[0]==400 and missing_import[1].get('code')=='REQUEST_ID_REQUIRED', missing_import
            blank_import=post(employee_client,'/api/phone-cards/import',dict(office_ids=[office_a],request_id='',_expected_versions={}))
            assert blank_import[0]==400 and blank_import[1].get('code')=='REQUEST_ID_REQUIRED', blank_import
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT 1 FROM phone_cards WHERE phone=?',(import_phone,)).fetchone() is None
            import_idem=dict(office_ids=[office_a],request_id='a3a3a3a3-a3a3-4a3a-8a3a-a3a3a3a3a3a3',_expected_versions={})
            import_first=post(employee_client,'/api/phone-cards/import',import_idem)
            assert import_first[0]==200 and import_first[1].get('ok') is True and import_first[1].get('added',0)>=1, import_first
            with runtime.connect() as raw_db:
                imported_card=raw_db.execute('SELECT id FROM phone_cards WHERE phone=?',(import_phone,)).fetchone()[0]
                raw_db.begin_write()
                raw_db.execute('DELETE FROM phone_card_offices WHERE card_id=?',(imported_card,))
                raw_db.execute('DELETE FROM phone_cards WHERE id=?',(imported_card,))
                raw_db.commit()
            import_again=post(employee_client,'/api/phone-cards/import',import_idem)
            assert import_again[0]==200 and import_again[1].get('added')==import_first[1]['added'], import_again
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT 1 FROM phone_cards WHERE phone=?',(import_phone,)).fetchone() is None
            import_reused=post(employee_client,'/api/phone-cards/import',dict(import_idem,office_ids=[office_b]))
            assert import_reused[0]==409 and import_reused[1].get('code')=='REQUEST_ID_REUSED', import_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT 1 FROM phone_cards WHERE phone=?',(import_phone,)).fetchone() is None
            bad_office=dict(name='',request_id='a6a6a6a6-a6a6-4a6a-8a6a-a6a6a6a6a6a6')
            bad_office_first=post(owner_client,'/api/auth/offices',bad_office)
            assert bad_office_first[0]==400 and '办公室名称需为1至100字' in bad_office_first[1].get('error',''), bad_office_first
            assert post(owner_client,'/api/auth/offices',bad_office)==bad_office_first
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT 1 FROM offices WHERE name=?',('',)).fetchone() is None
            office_idem=dict(name='合成幂等办公室',request_id='a5a5a5a5-a5a5-4a5a-8a5a-a5a5a5a5a5a5')
            office_first=post(owner_client,'/api/auth/offices',office_idem)
            assert office_first[0]==200 and office_first[1].get('ok') is True and office_first[1].get('id'), office_first
            office_again=post(owner_client,'/api/auth/offices',office_idem)
            assert office_again[0]==200 and office_again[1].get('id')==office_first[1]['id'], office_again
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT COUNT(*) FROM offices WHERE name=?',('合成幂等办公室',)).fetchone()[0]==1
                raw_db.begin_write()
                raw_db.execute('DELETE FROM offices WHERE id=?',(office_first[1]['id'],))
                raw_db.commit()
            office_replay=post(owner_client,'/api/auth/offices',office_idem)
            assert office_replay[0]==200 and office_replay[1].get('id')==office_first[1]['id'], office_replay
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT 1 FROM offices WHERE name=?',('合成幂等办公室',)).fetchone() is None
            office_reused=post(owner_client,'/api/auth/offices',dict(office_idem,name='合成幂等办公室被拒绝'))
            assert office_reused[0]==409 and office_reused[1].get('code')=='REQUEST_ID_REUSED', office_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT 1 FROM offices WHERE name=?',('合成幂等办公室',)).fetchone() is None
                assert raw_db.execute('SELECT 1 FROM offices WHERE name=?',('合成幂等办公室被拒绝',)).fetchone() is None
            hidden_office=post(employee_client,'/api/auth/offices',office_idem)
            assert hidden_office[0]==403 and office_first[1]['id'] not in json.dumps(hidden_office[1],ensure_ascii=False), hidden_office
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT 1 FROM offices WHERE name=?',('合成幂等办公室',)).fetchone() is None
            account_b_before=next(row for row in get(owner_client,'/api/auth/entity-scopes?kind=accounts')['entities'] if row['id']=='account-b')
            assert account_b_before['office_ids']==[office_b], account_b_before
            bad_scope=dict(kind='nope',id='account-b',expected_scope=account_b_before['scope_version'],office_ids=[office_a],request_id='a8a8a8a8-a8a8-4a8a-8a8a-a8a8a8a8a8a8')
            bad_scope_first=post(owner_client,'/api/auth/entity-scope',bad_scope)
            assert bad_scope_first[0]==400 and '资料类型不正确' in bad_scope_first[1].get('error',''), bad_scope_first
            assert post(owner_client,'/api/auth/entity-scope',bad_scope)==bad_scope_first
            with runtime.connect() as raw_db:
                assert [row[0] for row in raw_db.execute('SELECT office_id FROM account_offices WHERE account_id=?',('account-b',))]==[office_b]
            scope_idem=dict(kind='accounts',id='account-b',expected_scope=account_b_before['scope_version'],office_ids=[office_a],request_id='a7a7a7a7-a7a7-4a7a-8a7a-a7a7a7a7a7a7')
            scope_first=post(owner_client,'/api/auth/entity-scope',scope_idem)
            assert scope_first[0]==200 and scope_first[1].get('ok') is True and scope_first[1].get('office_ids')==[office_a], scope_first
            scope_again=post(owner_client,'/api/auth/entity-scope',scope_idem)
            assert scope_again[0]==200 and scope_again[1].get('office_ids')==[office_a] and scope_again[1].get('scope_version')==scope_first[1]['scope_version'], scope_again
            with runtime.connect() as raw_db:
                assert [row[0] for row in raw_db.execute('SELECT office_id FROM account_offices WHERE account_id=?',('account-b',))]==[office_a]
                raw_db.begin_write()
                raw_db.execute('DELETE FROM account_offices WHERE account_id=?',('account-b',))
                raw_db.execute('INSERT INTO account_offices(account_id,office_id) VALUES(?,?)',('account-b',office_b))
                raw_db.commit()
            scope_replay=post(owner_client,'/api/auth/entity-scope',scope_idem)
            assert scope_replay[0]==200 and scope_replay[1].get('office_ids')==[office_a], scope_replay
            with runtime.connect() as raw_db:
                assert [row[0] for row in raw_db.execute('SELECT office_id FROM account_offices WHERE account_id=?',('account-b',))]==[office_b]
            scope_reused=post(owner_client,'/api/auth/entity-scope',dict(scope_idem,office_ids=[office_b]))
            assert scope_reused[0]==409 and scope_reused[1].get('code')=='REQUEST_ID_REUSED', scope_reused
            with runtime.connect() as raw_db:
                assert [row[0] for row in raw_db.execute('SELECT office_id FROM account_offices WHERE account_id=?',('account-b',))]==[office_b]
            hidden_scope=post(employee_client,'/api/auth/entity-scope',scope_idem)
            assert hidden_scope[0]==403 and office_a not in json.dumps(hidden_scope[1],ensure_ascii=False), hidden_scope
            with runtime.connect() as raw_db:
                assert [row[0] for row in raw_db.execute('SELECT office_id FROM account_offices WHERE account_id=?',('account-b',))]==[office_b]
            def fetch(client,path):
                try:
                    with client.open(base+path,timeout=10) as response:return response.status,json.load(response)
                except urllib.error.HTTPError as error:return error.code,json.load(error)
            missing_lookup=fetch(owner_client,'/api/idempotency/result?request_id=b0b0b0b0-b0b0-4b0b-8b0b-b0b0b0b0b0b0')
            assert missing_lookup[0]==404 and missing_lookup[1].get('code')=='REQUEST_NOT_FOUND', missing_lookup
            found_lookup=fetch(owner_client,'/api/idempotency/result?request_id='+office_idem['request_id'])
            assert found_lookup[0]==200 and found_lookup[1].get('code')=='REQUEST_SUCCEEDED' and found_lookup[1].get('id')==office_first[1]['id'], found_lookup
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT 1 FROM offices WHERE name=?',('合成幂等办公室',)).fetchone() is None
            failed_lookup=fetch(owner_client,'/api/idempotency/result?request_id='+bad_office['request_id'])
            assert failed_lookup[0]==400 and failed_lookup[1].get('code')=='REQUEST_FAILED' and '办公室名称需为1至100字' in failed_lookup[1].get('error',''), failed_lookup
            with runtime.connect() as raw_db:
                sample=raw_db.execute('SELECT restore_generation,user_id FROM idempotency_records WHERE request_id=?',(office_idem['request_id'],)).fetchone()
                raw_db.begin_write()
                raw_db.execute('INSERT INTO idempotency_records(restore_generation,user_id,request_id,action,request_sha256,state,created_at) VALUES(?,?,?,?,?,?,?)',(sample[0],sample[1],'b9b9b9b9-b9b9-4b9b-8b9b-b9b9b9b9b9b9','/api/auth/offices','pending-digest','pending',int(time.time())))
                raw_db.execute("INSERT INTO idempotency_records(restore_generation,user_id,request_id,action,request_sha256,state,result_status,result_json,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",('closed-generation',sample[1],'b8b8b8b8-b8b8-4b8b-8b8b-b8b8b8b8b8b8','/api/auth/offices','old-digest','succeeded',200,'{"ok":true,"id":"stale-generation-body"}',int(time.time()),int(time.time())))
                raw_db.commit()
            pending_lookup=fetch(owner_client,'/api/idempotency/result?request_id=b9b9b9b9-b9b9-4b9b-8b9b-b9b9b9b9b9b9')
            assert pending_lookup[0]==409 and pending_lookup[1].get('code')=='REQUEST_PENDING' and pending_lookup[1].get('ok') is not True, pending_lookup
            old_lookup=fetch(owner_client,'/api/idempotency/result?request_id=b8b8b8b8-b8b8-4b8b-8b8b-b8b8b8b8b8b8')
            assert old_lookup[0]==409 and old_lookup[1].get('code')=='RESTORE_GENERATION_EXPIRED' and 'stale-generation-body' not in json.dumps(old_lookup[1],ensure_ascii=False), old_lookup
            import idempotency
            now=int(time.time())
            kept_at=now-idempotency.RESULT_RETENTION_SECONDS+86400
            expired_at=now-idempotency.RESULT_RETENTION_SECONDS-86400
            with runtime.connect() as raw_db:
                raw_db.begin_write()
                raw_db.execute('UPDATE idempotency_records SET completed_at=? WHERE request_id=?',(kept_at,'a5a5a5a5-a5a5-4a5a-8a5a-a5a5a5a5a5a5'))
                raw_db.commit()
            kept_office=post(owner_client,'/api/auth/offices',office_idem)
            assert kept_office[0]==200 and kept_office[1].get('id')==office_first[1]['id'], kept_office
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT 1 FROM offices WHERE name=?',('合成幂等办公室',)).fetchone() is None
                owner_id=raw_db.execute('SELECT id FROM auth_users WHERE username=?',('test_owner',)).fetchone()[0]
                assert raw_db.execute('SELECT result_json FROM idempotency_records WHERE user_id=? AND request_id=?',(owner_id,'a5a5a5a5-a5a5-4a5a-8a5a-a5a5a5a5a5a5')).fetchone()[0]
                raw_db.begin_write()
                raw_db.execute('UPDATE idempotency_records SET completed_at=? WHERE user_id=? AND request_id=?',(expired_at,owner_id,'a5a5a5a5-a5a5-4a5a-8a5a-a5a5a5a5a5a5'))
                raw_db.commit()
            expired_office=post(owner_client,'/api/auth/offices',office_idem)
            assert expired_office[0]==409 and expired_office[1].get('code')=='RESULT_EXPIRED', expired_office
            assert office_first[1]['id'] not in json.dumps(expired_office[1],ensure_ascii=False)
            expired_lookup=fetch(owner_client,'/api/idempotency/result?request_id='+office_idem['request_id'])
            assert expired_lookup[0]==409 and expired_lookup[1].get('code')=='RESULT_EXPIRED' and office_first[1]['id'] not in json.dumps(expired_lookup[1],ensure_ascii=False), expired_lookup
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT 1 FROM offices WHERE name=?',('合成幂等办公室',)).fetchone() is None
                owner_id=raw_db.execute('SELECT id FROM auth_users WHERE username=?',('test_owner',)).fetchone()[0]
                expired_row=raw_db.execute('SELECT state,result_json FROM idempotency_records WHERE user_id=? AND request_id=?',(owner_id,'a5a5a5a5-a5a5-4a5a-8a5a-a5a5a5a5a5a5')).fetchone()
                assert expired_row[0]=='succeeded' and expired_row[1] is None, (expired_row[0], None if expired_row[1] is None else type(expired_row[1]).__name__)
            expired_reused=post(owner_client,'/api/auth/offices',dict(office_idem,name='合成幂等办公室超期被拒绝'))
            assert expired_reused[0]==409 and expired_reused[1].get('code')=='REQUEST_ID_REUSED', expired_reused
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT 1 FROM offices WHERE name=?',('合成幂等办公室超期被拒绝',)).fetchone() is None
                raw_db.begin_write()
                raw_db.execute('UPDATE idempotency_records SET completed_at=? WHERE request_id=?',(expired_at,'a6a6a6a6-a6a6-4a6a-8a6a-a6a6a6a6a6a6'))
                raw_db.commit()
            expired_failure=post(owner_client,'/api/auth/offices',bad_office)
            assert expired_failure[0]==409 and expired_failure[1].get('code')=='RESULT_EXPIRED', expired_failure
            assert '办公室名称需为1至100字' not in json.dumps(expired_failure[1],ensure_ascii=False)
            with runtime.connect() as raw_db:
                failed_row=raw_db.execute('SELECT state,result_json FROM idempotency_records WHERE request_id=?',('a6a6a6a6-a6a6-4a6a-8a6a-a6a6a6a6a6a6',)).fetchone()
                assert failed_row[0]=='failed' and failed_row[1] is None, failed_row
            person_idem=dict(username='synthetic_idem_person',name='合成幂等人员',password=test_auth_support.TEST_PASSWORD,permissions=['account_status'],office_ids=[office_a],request_id='66666666-6666-4666-8666-666666666666')
            person_first=post(owner_client,'/api/auth/users',person_idem)
            assert person_first[0]==200 and person_first[1].get('id') and 'password' not in person_first[1], person_first
            person_again=post(owner_client,'/api/auth/users',person_idem)
            assert person_again[0]==200 and person_again[1].get('id')==person_first[1]['id'], person_again
            assert test_auth_support.TEST_PASSWORD not in json.dumps(person_again[1],ensure_ascii=False)
            people=[person for person in get(owner_client,'/api/auth/users')['users'] if person['username']=='synthetic_idem_person']
            assert len(people)==1 and people[0]['id']==person_first[1]['id'] and people[0]['name']=='合成幂等人员' and people[0]['office_ids']==[office_a]
            person_reused=post(owner_client,'/api/auth/users',dict(person_idem,name='合成幂等人员被拒绝'))
            assert person_reused[0]==409 and person_reused[1].get('code')=='REQUEST_ID_REUSED', person_reused
            assert next(person['name'] for person in get(owner_client,'/api/auth/users')['users'] if person['username']=='synthetic_idem_person')=='合成幂等人员'
            update_idem=dict(id=person_first[1]['id'],name='合成幂等人员已改',permissions=['account_status'],office_ids=[office_a],request_id='88888888-8888-4888-8888-888888888888')
            person_updated=post(owner_client,'/api/auth/users',update_idem)
            assert person_updated[0]==200 and person_updated[1].get('id')==person_first[1]['id'], person_updated
            assert post(owner_client,'/api/auth/users',update_idem)[1].get('id')==person_first[1]['id']
            assert next(person['name'] for person in get(owner_client,'/api/auth/users')['users'] if person['id']==person_first[1]['id'])=='合成幂等人员已改'
            update_reused=post(owner_client,'/api/auth/users',dict(update_idem,name='合成幂等人员被改拒绝'))
            assert update_reused[0]==409 and update_reused[1].get('code')=='REQUEST_ID_REUSED', update_reused
            assert next(person['name'] for person in get(owner_client,'/api/auth/users')['users'] if person['id']==person_first[1]['id'])=='合成幂等人员已改'
            bad_person=dict(username='synthetic_idem_bad',name='合成幂等失败人员',password='short-pw',permissions=['account_status'],office_ids=[office_a],request_id='77777777-7777-4777-8777-777777777777')
            bad_person_first=post(owner_client,'/api/auth/users',bad_person)
            assert bad_person_first[0]==400 and bad_person_first[1].get('error'), bad_person_first
            assert post(owner_client,'/api/auth/users',bad_person)==bad_person_first
            assert not any(person['username']=='synthetic_idem_bad' for person in get(owner_client,'/api/auth/users')['users'])
            with runtime.connect() as raw_db:
                stored=raw_db.execute("SELECT result_json FROM idempotency_records WHERE request_id='66666666-6666-4666-8666-666666666666'").fetchone()[0]
                assert 'password' not in stored and test_auth_support.TEST_PASSWORD not in stored
            hidden_person=post(employee_client,'/api/auth/users',person_idem)
            assert hidden_person[0]==403 and 'id' not in hidden_person[1], hidden_person
            assert test_auth_support.TEST_PASSWORD not in json.dumps(hidden_person[1],ensure_ascii=False)
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=['account_status'],office_ids=[office_a,office_b],request_id='cececece-cece-4ece-8ece-cececececece'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            hidden_lookup=fetch(employee_client,'/api/idempotency/result?request_id='+idem['request_id'])
            assert hidden_lookup[0]==403 and first[1]['id'] not in json.dumps(hidden_lookup[1],ensure_ascii=False), hidden_lookup
            hidden=post(employee_client,'/api/account/create',idem)
            assert hidden[0]==403 and first[1]['id'] not in json.dumps(hidden[1],ensure_ascii=False), hidden
            hidden_card=post(employee_client,'/api/phone-card',card_idem)
            assert hidden_card[0]==403 and str(card_first[1]['id']) not in json.dumps(hidden_card[1],ensure_ascii=False), hidden_card
            hidden_card_import=post(employee_client,'/api/phone-cards/import',import_idem)
            assert hidden_card_import[0]==403 and hidden_card_import[1].get('ok') is not True and 'added' not in hidden_card_import[1], hidden_card_import
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT 1 FROM phone_cards WHERE phone=?',(import_phone,)).fetchone() is None
            hidden_card_update=post(employee_client,'/api/phone-card',card_update)
            assert hidden_card_update[0]==403 and hidden_card_update[1].get('ok') is not True and '合成幂等卡标签' not in json.dumps(hidden_card_update[1],ensure_ascii=False), hidden_card_update
            hidden_device=post(employee_client,'/api/phone-device/create',device_idem)
            assert hidden_device[0]==403 and '合成幂等手机' not in json.dumps(hidden_device[1],ensure_ascii=False), hidden_device
            hidden_device_update=post(employee_client,'/api/phone-device',device_update)
            assert hidden_device_update[0]==403 and hidden_device_update[1].get('ok') is not True and '合成幂等手机已改' not in json.dumps(hidden_device_update[1],ensure_ascii=False), hidden_device_update
            hidden_assign=post(employee_client,'/api/publishing/assignment',assign_idem)
            assert hidden_assign[0]==403 and hidden_assign[1].get('ok') is not True, hidden_assign
            hidden_link=post(employee_client,'/api/publishing/link',link_idem)
            assert hidden_link[0]==403 and hidden_link[1].get('ok') is not True and 'account-a' not in json.dumps(hidden_link[1],ensure_ascii=False), hidden_link
            hidden_phone=post(employee_client,'/api/account/phone-device',phone_idem)
            assert hidden_phone[0]==403 and hidden_phone[1].get('ok') is not True, hidden_phone
            hidden_readiness=post(employee_client,'/api/account/card-readiness',readiness_idem)
            assert hidden_readiness[0]==403 and hidden_readiness[1].get('ok') is not True and '已稳定' not in json.dumps(hidden_readiness[1],ensure_ascii=False), hidden_readiness
            hidden_lifecycle=post(employee_client,'/api/lifecycle-status',lifecycle_idem)
            assert hidden_lifecycle[0]==403 and hidden_lifecycle[1].get('ok') is not True and '已注册' not in json.dumps(hidden_lifecycle[1],ensure_ascii=False), hidden_lifecycle
            hidden_daily=post(employee_client,'/api/daily',daily_idem)
            assert hidden_daily[0]==403 and hidden_daily[1].get('ok') is not True, hidden_daily
            with runtime.connect() as raw_db:
                assert int(raw_db.execute('SELECT published FROM daily WHERE account_id=? AND day=?',(patch_id,daily_day)).fetchone()[0])==0
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT auxiliary_state FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==lifecycle_before
            with runtime.connect() as raw_db:
                hidden_kept=raw_db.execute('SELECT card_stability,password_state FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert hidden_kept[0]==readiness_before[0] and hidden_kept[1]==readiness_before[1]
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT phone_device_id FROM accounts WHERE id=?',(patch_id,)).fetchone()[0] is None
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=3").fetchone()[0]=='account-a'
                assert raw_db.execute("SELECT updated_at FROM publishing_layout_links WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=3").fetchone()[0]==1
            hidden_import=post(employee_client,'/api/workspace/import-apply',import_idem)
            assert hidden_import[0]==403 and 'synthetic-import-idem' not in json.dumps(hidden_import[1],ensure_ascii=False), hidden_import
            hidden_upload=post(employee_client,'/api/workspace/nurture/upload',upload_idem)
            assert hidden_upload[0]==403 and image not in json.dumps(hidden_upload[1],ensure_ascii=False), hidden_upload
            hidden_join=post(employee_client,'/api/workspace/nurture/join',join_idem)
            assert hidden_join[0]==403 and hidden_join[1].get('ok') is not True, hidden_join
            hidden_start=post(employee_client,'/api/workspace/nurture/start',start_idem)
            assert hidden_start[0]==403 and hidden_start[1].get('ok') is not True, hidden_start
            hidden_verify=post(employee_client,'/api/workspace/nurture/verification',verify_idem)
            assert hidden_verify[0]==403 and '合成核对备注' not in json.dumps(hidden_verify[1],ensure_ascii=False), hidden_verify
            hidden_pause=post(employee_client,'/api/workspace/nurture/pause',pause_idem)
            assert hidden_pause[0]==403 and hidden_pause[1].get('ok') is not True, hidden_pause
            hidden_resume=post(employee_client,'/api/workspace/nurture/resume',resume_idem)
            assert hidden_resume[0]==403 and hidden_resume[1].get('ok') is not True, hidden_resume
            hidden_finish=post(employee_client,'/api/workspace/nurture/finish',finish_idem)
            assert hidden_finish[0]==403 and hidden_finish[1].get('ok') is not True, hidden_finish
            hidden_delete=post(employee_client,'/api/workspace/nurture/delete-image',delete_idem)
            assert hidden_delete[0]==403 and hidden_delete[1].get('ok') is not True, hidden_delete
            hidden_purge=post(employee_client,'/api/workspace/nurture/purge',purge_idem)
            assert hidden_purge[0]==403 and hidden_purge[1].get('ok') is not True, hidden_purge
            hidden_patch=post(employee_client,'/api/account/patch',patch_idem)
            assert hidden_patch[0]==403 and hidden_patch[1].get('ok') is not True and '合成幂等备注' not in json.dumps(hidden_patch[1],ensure_ascii=False), hidden_patch
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=['account_progress'],office_ids=[office_a,office_b],request_id='cfcfcfcf-cfcf-4fcf-8fcf-cfcfcfcfcfcf'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            hidden_status=post(employee_client,'/api/account/status',status_idem)
            assert hidden_status[0]==403 and hidden_status[1].get('ok') is not True, hidden_status
            hidden_bulk=post(employee_client,'/api/bulk-status',bulk_idem)
            assert hidden_bulk[0]==403 and hidden_bulk[1].get('ok') is not True and '封禁' not in json.dumps(hidden_bulk[1],ensure_ascii=False), hidden_bulk
            hidden_batch=post(employee_client,'/api/batch-edit',batch_idem)
            assert hidden_batch[0]==403 and hidden_batch[1].get('ok') is not True and '短期封禁' not in json.dumps(hidden_batch[1],ensure_ascii=False), hidden_batch
            hidden_save=post(employee_client,'/api/account',save_idem)
            assert hidden_save[0]==403 and hidden_save[1].get('ok') is not True and '合成完整保存备注' not in json.dumps(hidden_save[1],ensure_ascii=False), hidden_save
            hidden_nurture=post(employee_client,'/api/account/nurture',nurture_idem)
            assert hidden_nurture[0]==403 and hidden_nurture[1].get('ok') is not True and '2026-01-02' not in json.dumps(hidden_nurture[1],ensure_ascii=False), hidden_nurture
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==bulk_before
                assert raw_db.execute('SELECT note FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==save_note
                assert raw_db.execute('SELECT nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==nurture_before
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='d0d0d0d0-d0d0-4d0d-8d0d-d0d0d0d0d0d0'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admin_body=dict(username='synthetic_limited_admin',name='合成无写权限管理员',role='admin',permissions=['history_view','data_export'],office_ids=[],password=test_auth_support.TEST_PASSWORD)
            missing_admin=post(owner_client,'/api/auth/users',admin_body)
            assert missing_admin[0]==400 and missing_admin[1].get('code')=='REQUEST_ID_REQUIRED', missing_admin
            assert not any(person['username']=='synthetic_limited_admin' for person in get(owner_client,'/api/auth/users')['users'])
            admin_created=post(owner_client,'/api/auth/users',dict(admin_body,request_id='d3d3d3d3-d3d3-4d3d-8d3d-d3d3d3d3d3d3'))
            assert admin_created[0]==200,admin_created
            admin_client=test_auth_support.client(base,'synthetic_limited_admin')
            assert any(row['id']=='account-b' for row in get(admin_client,'/api/board')['accounts'])
            for image_id in ('image-account-a','image-account-b'):
                with admin_client.open(base+'/api/nurture/image?id='+image_id,timeout=10) as response:
                    image_body=response.read()
                    assert response.status==200 and response.headers.get('Content-Type')=='image/png' and image_body==png(), response.status
            history_a=get(admin_client,'/api/workspace/history?entity=account:account-a')
            history_b=get(admin_client,'/api/workspace/history?entity=account:account-b')
            history_seen=(secret in json.dumps(history_a,ensure_ascii=False)) and isinstance(history_b.get('entries'),list)
            assert history_seen is True, len(history_a.get('entries',[]))
            exported=post(admin_client,'/api/workspace/export',dict(kind='accounts',format='csv'))
            assert exported[0]==200, exported[0]
            exported_text=base64.b64decode(exported[1]['content']).decode('utf-8-sig')
            exported_values={value for row in csv.reader(io.StringIO(exported_text)) for value in row}
            exported_seen=('合成账号A' in exported_text) and ('account-b' in exported_values) and (secret in exported_text)
            assert exported[1].get('count',0)>=2 and exported_seen is True, exported[1].get('count')
            for kind,own,foreign in (('cards','13800000001','13800000002'),('devices','合成共用手机',secret)):
                exported_kind=post(admin_client,'/api/workspace/export',dict(kind=kind,format='csv'))
                assert exported_kind[0]==200, exported_kind[0]
                kind_text=base64.b64decode(exported_kind[1]['content']).decode('utf-8-sig')
                kind_values={value for row in csv.reader(io.StringIO(kind_text)) for value in row}
                kind_seen=(own in kind_values) and (foreign in kind_values)
                assert exported_kind[1].get('count',0)>=2 and kind_seen is True, (kind, exported_kind[1].get('count'))
            assert post(admin_client,'/api/daily',dict(id='account-b',date=day,published=True))[0]==403
            assert post(admin_client,'/api/auth/offices',dict(name='禁止普通管理员授权'))[0]==403
            with runtime.connect() as check_db:
                published_before=check_db.execute('SELECT published FROM daily WHERE account_id=? AND day=?',('account-b',day)).fetchone()
            reset_password='synthetic-reset-1'
            reset=post(owner_client,'/api/auth/users',dict(id=admin_created[1]['id'],name='合成无写权限管理员',role='admin',permissions=[],office_ids=[],password=reset_password,request_id='1a1a1a1a-1a1a-41a1-81a1-1a1a1a1a1a1a'))
            assert reset[0]==200 and reset[1].get('id')==admin_created[1]['id'] and reset_password not in json.dumps(reset[1],ensure_ascii=False),reset[0]
            refused_reset=post(admin_client,'/api/daily',dict(id='account-b',date=day,published=False,request_id='1b1b1b1b-1b1b-41b1-81b1-1b1b1b1b1b1b'))
            assert refused_reset[0]==401,refused_reset
            with runtime.connect() as check_db:
                assert check_db.execute('SELECT published FROM daily WHERE account_id=? AND day=?',('account-b',day)).fetchone()==published_before
                stored=check_db.execute('SELECT result_json FROM idempotency_records WHERE request_id=?',('1a1a1a1a-1a1a-41a1-81a1-1a1a1a1a1a1a',)).fetchone()[0]
                assert reset_password not in (stored or '')
                personnel_rows=check_db.execute("SELECT action, COALESCE(target,'') FROM auth_audit WHERE action IN ('新增人员','更新人员与权限')").fetchall()
                personnel_text='\n'.join((row[0] or '')+(row[1] or '') for row in personnel_rows)
                personnel_leaked=(test_auth_support.TEST_PASSWORD in personnel_text) or (reset_password in personnel_text)
                assert len(personnel_rows)>=2 and personnel_leaked is False, len(personnel_rows)
            try:
                test_auth_support.client(base,'synthetic_limited_admin')
                raise AssertionError('old password still accepted')
            except urllib.error.HTTPError as error:
                assert error.code==401
            renewed=test_auth_support.client(base,'synthetic_limited_admin',password=reset_password)
            assert any(row['id']=='account-b' for row in get(renewed,'/api/board')['accounts'])
            assert post(renewed,'/api/daily',dict(id='account-b',date=day,published=True))[0]==403
            admin_visible=sorted(row['id'] for row in get(renewed,'/api/board')['accounts'])
            assert 'account-a' in admin_visible and 'account-b' in admin_visible
            admin_logged_out=post(renewed,'/api/auth/logout',{})
            assert admin_logged_out[0]==200 and admin_logged_out[1].get('ok') is True, admin_logged_out[0]
            assert 'password' not in json.dumps(admin_logged_out[1],ensure_ascii=False)
            try:
                get(renewed,'/api/board')
                raise AssertionError('logged out admin board accepted')
            except urllib.error.HTTPError as error:
                assert error.code==401
            assert get(renewed,'/api/auth/me')['user'] is None
            with runtime.connect() as check_db:
                assert check_db.execute('SELECT count(*) FROM auth_sessions WHERE user_id=?',(admin_created[1]['id'],)).fetchone()[0]==0
                admin_row=check_db.execute('SELECT active,role FROM auth_users WHERE id=?',(admin_created[1]['id'],)).fetchone()
                assert admin_row[0]==1 and admin_row[1]=='admin'
            renewed=test_auth_support.client(base,'synthetic_limited_admin',password=reset_password)
            admin_again=get(renewed,'/api/auth/me')['user']
            assert admin_again['role']=='admin' and admin_again['username']=='synthetic_limited_admin'
            assert sorted(row['id'] for row in get(renewed,'/api/board')['accounts'])==admin_visible
            assert post(renewed,'/api/daily',dict(id='account-b',date=day,published=True))[0]==403
            assert post(renewed,'/api/auth/offices',dict(name='禁止普通管理员授权'))[0]==403
            card_board=get(employee_client,'/api/board')
            card_row=next(row for row in card_board['phone_cards']['cards'] if row['id']==card_update_id)
            assert set(card_row['office_ids'])=={office_a}
            with runtime.connect() as raw_db:
                card_before=raw_db.execute('SELECT label,phone FROM phone_cards WHERE id=?',(card_update_id,)).fetchone()
                card_offices_before=sorted(row[0] for row in raw_db.execute('SELECT office_id FROM phone_card_offices WHERE card_id=?',(card_update_id,)).fetchall())
                card_history_kept=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(card_key,)).fetchone()[0]
                card_audit_kept=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card' AND target=?",(str(card_update_id),)).fetchone()[0]
            assert card_offices_before==[office_a] and card_before[0]!='合成锁等待卡标签' and card_before[1]=='19900000011'
            card_wait_body=dict(id=card_update_id,phone='19900000011',label='合成锁等待卡标签',office_ids=[office_a],request_id='f011f011-f011-4f01-8f01-f011f011f011',_expected_versions={card_key:card_board['record_versions'][card_key]})
            card_kept=[item for item in auth.PERMISSIONS if item!='card_manage']
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/phone-card',card_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Card permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=card_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                card_waited=future.result(timeout=10)
                assert card_waited[0]==401,card_waited[0]
            card_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==card_update_id for row in get(card_reduced,'/api/board')['phone_cards']['cards'])
            refused_card=post(card_reduced,'/api/phone-card',dict(id=card_update_id,phone='19900000011',label='合成锁等待卡标签',office_ids=[office_a],request_id='f012f012-f012-4f02-8f02-f012f012f012'))
            assert refused_card[0]==403,refused_card[0]
            with runtime.connect() as check_db:
                card_after_wait=check_db.execute('SELECT label,phone FROM phone_cards WHERE id=?',(card_update_id,)).fetchone()
                assert card_after_wait[0]==card_before[0] and card_after_wait[1]==card_before[1]
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM phone_card_offices WHERE card_id=?',(card_update_id,)).fetchall())==card_offices_before
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(card_key,)).fetchone()[0]==card_history_kept
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card' AND target=?",(str(card_update_id),)).fetchone()[0]==card_audit_kept
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('f011f011-f011-4f01-8f01-f011f011f011',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_card_permissions=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'card_manage' not in stored_card_permissions and 'card_tasks' in stored_card_permissions
            assert get(card_reduced,'/api/board')['record_versions'][card_key]==card_wait_body['_expected_versions'][card_key]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='f023f023-f023-4f23-8f23-f023f023f023'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            with runtime.connect() as raw_db:
                admitted_card_before=raw_db.execute('SELECT label,phone FROM phone_cards WHERE id=?',(card_update_id,)).fetchone()
                admitted_card_offices=sorted(row[0] for row in raw_db.execute('SELECT office_id FROM phone_card_offices WHERE card_id=?',(card_update_id,)).fetchall())
                admitted_card_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(card_key,)).fetchone()[0]
                admitted_card_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card' AND target=?",(str(card_update_id),)).fetchone()[0]
            assert admitted_card_offices==[office_a] and admitted_card_before[1]=='19900000011'
            admitted_card_label='合成已准入卡标签' if admitted_card_before[0]!='合成已准入卡标签' else '合成已准入卡乙'
            def revoke_card():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=card_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_card)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Card revoke did not wait for the admitted card write')
                holder.execute('UPDATE phone_cards SET label=? WHERE id=?',(admitted_card_label,card_update_id))
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted card write')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_card_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==card_update_id for row in get(admitted_card_client,'/api/board')['phone_cards']['cards'])
            refused_card_after=post(admitted_card_client,'/api/phone-card',dict(id=card_update_id,phone='19900000011',label=admitted_card_before[0],office_ids=[office_a],request_id='f111f111-f111-4f11-8f11-f111f111f111'))
            assert refused_card_after[0]==403,refused_card_after[0]
            with runtime.connect() as check_db:
                kept_card=check_db.execute('SELECT label,phone FROM phone_cards WHERE id=?',(card_update_id,)).fetchone()
                assert kept_card[0]==admitted_card_label and kept_card[1]==admitted_card_before[1]
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM phone_card_offices WHERE card_id=?',(card_update_id,)).fetchall())==admitted_card_offices
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(card_key,)).fetchone()[0]==admitted_card_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card' AND target=?",(str(card_update_id),)).fetchone()[0]==admitted_card_audit
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_card=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'card_manage' not in stored_admitted_card and 'card_tasks' in stored_admitted_card
                check_db.execute('UPDATE phone_cards SET label=? WHERE id=?',(admitted_card_before[0],card_update_id))
                restored_card=check_db.execute('SELECT label,phone FROM phone_cards WHERE id=?',(card_update_id,)).fetchone()
                assert restored_card[0]==admitted_card_before[0] and restored_card[1]==admitted_card_before[1]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='f123f123-f123-4f23-8f23-f123f123f123'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            publish_layout='layout:synthetic-batch:pc-a:11'
            publish_board=get(employee_client,'/api/board')
            with runtime.connect() as raw_db:
                publish_slots=tuple(raw_db.execute("SELECT slot,account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot IN (1,2,3,11) ORDER BY slot").fetchall())
                publish_history_kept=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(publish_layout,)).fetchone()[0]
                publish_audit_kept=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment' AND target=''").fetchone()[0]
            assert publish_slots[0]['slot']==1 and publish_slots[0]['account_id'] is None
            assert publish_slots[1]['slot']==2 and publish_slots[1]['account_id']=='account-b'
            assert publish_slots[2]['slot']==3 and publish_slots[2]['account_id']=='account-a'
            assert publish_slots[3]['slot']==11 and publish_slots[3]['account_id']==race_joined['id']
            publish_wait_body=dict(batch='synthetic-batch',computer_id='pc-a',slot=11,action='remove',request_id='f211f211-f211-4f21-8f21-f211f211f211',_expected_versions={publish_layout:publish_board['record_versions'][publish_layout]})
            publishing_kept=[item for item in auth.PERMISSIONS if item!='publishing_manage']
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/publishing/assignment',publish_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Publishing permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=publishing_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                publish_waited=future.result(timeout=10)
                assert publish_waited[0]==401,publish_waited[0]
            publish_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==race_joined['id'] for row in get(publish_reduced,'/api/board')['accounts'])
            refused_publish=post(publish_reduced,'/api/publishing/assignment',dict(batch='synthetic-batch',computer_id='pc-a',slot=11,action='remove',request_id='f212f212-f212-4f22-8f22-f212f212f212'))
            assert refused_publish[0]==403,refused_publish[0]
            with runtime.connect() as check_db:
                publish_after=tuple(check_db.execute("SELECT slot,account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot IN (1,2,3,11) ORDER BY slot").fetchall())
                assert [(row['slot'],row['account_id']) for row in publish_after]==[(row['slot'],row['account_id']) for row in publish_slots]
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(publish_layout,)).fetchone()[0]==publish_history_kept
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment' AND target=''").fetchone()[0]==publish_audit_kept
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('f211f211-f211-4f21-8f21-f211f211f211',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_publish_permissions=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'publishing_manage' not in stored_publish_permissions and 'publish' in stored_publish_permissions
            assert get(publish_reduced,'/api/board')['record_versions'][publish_layout]==publish_wait_body['_expected_versions'][publish_layout]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='f223f223-f223-4f23-8f23-f223f223f223'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            with runtime.connect() as raw_db:
                admitted_publish_slots=tuple(raw_db.execute("SELECT slot,account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot IN (1,2,3,11) ORDER BY slot").fetchall())
                admitted_slot=raw_db.execute("SELECT account_id,label,reason FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=11").fetchone()
                admitted_link=raw_db.execute("SELECT previous_reason,actor_id,updated_at FROM publishing_layout_links WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=11").fetchone()
                admitted_publish_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(publish_layout,)).fetchone()[0]
                admitted_publish_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment' AND target=''").fetchone()[0]
            assert [(row['slot'],row['account_id']) for row in admitted_publish_slots[:3]]==[(1,None),(2,'account-b'),(3,'account-a')]
            assert admitted_slot['account_id']==race_joined['id']
            def revoke_publishing():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=publishing_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_publishing)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Publishing revoke did not wait for the admitted layout write')
                holder.execute("UPDATE publishing_layout SET account_id=NULL,label='',reason='' WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=11")
                holder.execute("DELETE FROM publishing_layout_links WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=11")
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted publishing write')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_publish_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==race_joined['id'] for row in get(admitted_publish_client,'/api/board')['accounts'])
            refused_publish_after=post(admitted_publish_client,'/api/publishing/assignment',dict(batch='synthetic-batch',computer_id='pc-a',slot=11,action='remove',request_id='f311f311-f311-4f31-8f31-f311f311f311'))
            assert refused_publish_after[0]==403,refused_publish_after[0]
            with runtime.connect() as check_db:
                kept_publish_slots=tuple(check_db.execute("SELECT slot,account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot IN (1,2,3) ORDER BY slot").fetchall())
                assert [(row['slot'],row['account_id']) for row in kept_publish_slots]==[(1,None),(2,'account-b'),(3,'account-a')]
                kept_slot=check_db.execute("SELECT account_id,label,reason FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=11").fetchone()
                assert kept_slot['account_id'] is None and kept_slot['label']=='' and kept_slot['reason']==''
                assert check_db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=11").fetchone()[0]==0
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(publish_layout,)).fetchone()[0]==admitted_publish_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment' AND target=''").fetchone()[0]==admitted_publish_audit
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_publish=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'publishing_manage' not in stored_admitted_publish and 'publish' in stored_admitted_publish
                check_db.execute("UPDATE publishing_layout SET account_id=?,label=?,reason=? WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=11",(admitted_slot['account_id'],admitted_slot['label'],admitted_slot['reason']))
                if admitted_link:check_db.execute("INSERT INTO publishing_layout_links(batch,computer_id,slot,previous_reason,actor_id,updated_at) VALUES('synthetic-batch','pc-a',11,?,?,?)",(admitted_link['previous_reason'],admitted_link['actor_id'],admitted_link['updated_at']))
                restored_slot=check_db.execute("SELECT account_id,label,reason FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=11").fetchone()
                assert restored_slot['account_id']==admitted_slot['account_id'] and restored_slot['label']==admitted_slot['label'] and restored_slot['reason']==admitted_slot['reason']
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='f323f323-f323-4f23-8f23-f323f323f323'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            nurture_account='account:account-b'
            nurture_key='nurture:account-b'
            nurture_board=get(employee_client,'/api/board')
            assert any(row['id']=='account-b' for row in nurture_board['accounts'])
            with runtime.connect() as raw_db:
                nurture_before=raw_db.execute('SELECT workflow,active_since,paused_on FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                nurture_offices=sorted(row[0] for row in raw_db.execute('SELECT office_id FROM account_offices WHERE account_id=?',('account-b',)).fetchall())
                nurture_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(nurture_account,)).fetchone()[0]
                nurture_record_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(nurture_key,)).fetchone()[0]
                nurture_audit_kept=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/pause' AND target=''").fetchone()[0]
            assert nurture_before['workflow']=='运行' and nurture_offices==[office_b]
            nurture_wait_body=dict(id='account-b',request_id='f411f411-f411-4f41-8f41-f411f411f411',_expected_versions={nurture_account:nurture_board['record_versions'][nurture_account],nurture_key:nurture_board['record_versions'][nurture_key]})
            nurture_kept=[item for item in auth.PERMISSIONS if item!='nurture_manage']
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/workspace/nurture/pause',nurture_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=nurture_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                nurture_waited=future.result(timeout=10)
                assert nurture_waited[0]==401,nurture_waited[0]
            nurture_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']=='account-b' for row in get(nurture_reduced,'/api/board')['accounts'])
            refused_nurture=post(nurture_reduced,'/api/workspace/nurture/pause',dict(id='account-b',request_id='f412f412-f412-4f42-8f42-f412f412f412'))
            assert refused_nurture[0]==403,refused_nurture[0]
            with runtime.connect() as check_db:
                nurture_after=check_db.execute('SELECT workflow,active_since,paused_on FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert (nurture_after['workflow'],nurture_after['active_since'],nurture_after['paused_on'])==(nurture_before['workflow'],nurture_before['active_since'],nurture_before['paused_on'])
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM account_offices WHERE account_id=?',('account-b',)).fetchall())==nurture_offices
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(nurture_account,)).fetchone()[0]==nurture_account_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(nurture_key,)).fetchone()[0]==nurture_record_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/pause' AND target=''").fetchone()[0]==nurture_audit_kept
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('f411f411-f411-4f41-8f41-f411f411f411',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_nurture_permissions=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'nurture_manage' not in stored_nurture_permissions and 'account_status' in stored_nurture_permissions
            reduced_nurture_board=get(nurture_reduced,'/api/board')
            assert reduced_nurture_board['record_versions'][nurture_account]==nurture_wait_body['_expected_versions'][nurture_account]
            assert reduced_nurture_board['record_versions'][nurture_key]==nurture_wait_body['_expected_versions'][nurture_key]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='f423f423-f423-4f23-8f23-f423f423f423'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            with runtime.connect() as raw_db:
                admitted_nurture=raw_db.execute('SELECT workflow,active_since,paused_on,banked_days,periods,revision FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                admitted_nurture_status=raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-b',)).fetchone()[0]
                admitted_nurture_offices=sorted(row[0] for row in raw_db.execute('SELECT office_id FROM account_offices WHERE account_id=?',('account-b',)).fetchall())
                admitted_nurture_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(nurture_account,)).fetchone()[0]
                admitted_nurture_record_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(nurture_key,)).fetchone()[0]
                admitted_nurture_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/pause' AND target=''").fetchone()[0]
            assert admitted_nurture['workflow']=='运行' and admitted_nurture['active_since'] and admitted_nurture_offices==[office_b]
            from datetime import date as nurture_date
            admitted_elapsed=admitted_nurture['banked_days']+(nurture_date.fromisoformat(day)-nurture_date.fromisoformat(admitted_nurture['active_since'])).days
            admitted_periods=json.loads(admitted_nurture['periods'])
            admitted_periods.append(dict(start=admitted_nurture['active_since'],end=day))
            admitted_periods_json=json.dumps(admitted_periods)
            def revoke_nurture():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=nurture_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_nurture)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture revoke did not wait for the admitted pause')
                holder.execute("UPDATE nurture_records SET banked_days=?,periods=?,active_since='',paused_on=?,workflow='暂停',revision=revision+1 WHERE account_id=?",(admitted_elapsed,admitted_periods_json,day,'account-b'))
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted nurture pause')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_nurture_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']=='account-b' for row in get(admitted_nurture_client,'/api/board')['accounts'])
            refused_nurture_after=post(admitted_nurture_client,'/api/workspace/nurture/pause',dict(id='account-b',request_id='f511f511-f511-4f51-8f51-f511f511f511'))
            assert refused_nurture_after[0]==403,refused_nurture_after[0]
            with runtime.connect() as check_db:
                kept_nurture=check_db.execute('SELECT workflow,active_since,paused_on,banked_days,periods,revision FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert kept_nurture['workflow']=='暂停' and kept_nurture['active_since']=='' and kept_nurture['paused_on']==day
                assert kept_nurture['banked_days']==admitted_elapsed and kept_nurture['periods']==admitted_periods_json and kept_nurture['revision']==admitted_nurture['revision']+1
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',('account-b',)).fetchone()[0]==admitted_nurture_status
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM account_offices WHERE account_id=?',('account-b',)).fetchall())==admitted_nurture_offices
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(nurture_account,)).fetchone()[0]==admitted_nurture_account_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(nurture_key,)).fetchone()[0]==admitted_nurture_record_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/pause' AND target=''").fetchone()[0]==admitted_nurture_audit
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_nurture=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'nurture_manage' not in stored_admitted_nurture and 'account_status' in stored_admitted_nurture
                check_db.execute('UPDATE nurture_records SET banked_days=?,periods=?,active_since=?,paused_on=?,workflow=?,revision=? WHERE account_id=?',(admitted_nurture['banked_days'],admitted_nurture['periods'],admitted_nurture['active_since'],admitted_nurture['paused_on'],admitted_nurture['workflow'],admitted_nurture['revision'],'account-b'))
                restored_nurture=check_db.execute('SELECT workflow,active_since,paused_on,banked_days,revision FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert restored_nurture['workflow']=='运行' and restored_nurture['active_since']==admitted_nurture['active_since'] and restored_nurture['paused_on']==admitted_nurture['paused_on']
                assert restored_nurture['banked_days']==admitted_nurture['banked_days'] and restored_nurture['revision']==admitted_nurture['revision']
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='f523f523-f523-4f23-8f23-f523f523f523'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            account_board=get(employee_client,'/api/board')
            assert any(row['id']==patch_id for row in account_board['accounts'])
            with runtime.connect() as raw_db:
                account_before=raw_db.execute('SELECT name,status,note,computer_id,slot,douyin_id,homepage FROM accounts WHERE id=?',(patch_id,)).fetchone()
                account_history_kept=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(save_account,)).fetchone()[0]
                account_audit_kept=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?",(patch_id,)).fetchone()[0]
            account_note='合成锁等待账号备注' if account_before['note']!='合成锁等待账号备注' else '合成锁等待账号备注乙'
            account_wait_body=dict(id=patch_id,name=account_before['name'],status=account_before['status'],computer_id=account_before['computer_id'],slot=int(account_before['slot']),note=account_note,douyin_id=account_before['douyin_id'] or '',homepage=account_before['homepage'] or '',request_id='f611f611-f611-4f61-8f61-f611f611f611',_expected_versions={save_account:account_board['record_versions'][save_account]})
            account_kept=[item for item in auth.PERMISSIONS if item!='account_manage']
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/account',account_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Account permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=account_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                account_waited=future.result(timeout=10)
                assert account_waited[0]==401,account_waited[0]
            account_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(account_reduced,'/api/board')['accounts'])
            refused_account=post(account_reduced,'/api/account',dict(id=patch_id,name=account_before['name'],status=account_before['status'],computer_id=account_before['computer_id'],slot=int(account_before['slot']),note=account_note,douyin_id=account_before['douyin_id'] or '',homepage=account_before['homepage'] or '',request_id='f612f612-f612-4f62-8f62-f612f612f612'))
            assert refused_account[0]==403,refused_account[0]
            with runtime.connect() as check_db:
                account_after=check_db.execute('SELECT name,status,note,computer_id,slot,douyin_id,homepage FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert (account_after['name'],account_after['status'],account_after['note'],account_after['computer_id'],account_after['slot'])==(account_before['name'],account_before['status'],account_before['note'],account_before['computer_id'],account_before['slot'])
                assert (account_after['douyin_id'],account_after['homepage'])==(account_before['douyin_id'],account_before['homepage'])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(save_account,)).fetchone()[0]==account_history_kept
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?",(patch_id,)).fetchone()[0]==account_audit_kept
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('f611f611-f611-4f61-8f61-f611f611f611',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_account_permissions=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_manage' not in stored_account_permissions and 'account_status' in stored_account_permissions
            assert get(account_reduced,'/api/board')['record_versions'][save_account]==account_wait_body['_expected_versions'][save_account]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='f623f623-f623-4f23-8f23-f623f623f623'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            with runtime.connect() as raw_db:
                admitted_account=raw_db.execute('SELECT name,status,note,computer_id,slot,douyin_id,homepage FROM accounts WHERE id=?',(patch_id,)).fetchone()
                admitted_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(save_account,)).fetchone()[0]
                admitted_account_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?",(patch_id,)).fetchone()[0]
            admitted_account_note='合成已准入账号备注' if admitted_account['note']!='合成已准入账号备注' else '合成已准入账号备注乙'
            def revoke_account():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=account_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_account)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Account revoke did not wait for the admitted account write')
                holder.execute('UPDATE accounts SET note=? WHERE id=?',(admitted_account_note,patch_id))
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted account write')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_account_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(admitted_account_client,'/api/board')['accounts'])
            refused_account_after=post(admitted_account_client,'/api/account',dict(id=patch_id,name=admitted_account['name'],status=admitted_account['status'],computer_id=admitted_account['computer_id'],slot=int(admitted_account['slot']),note=admitted_account['note'],douyin_id=admitted_account['douyin_id'] or '',homepage=admitted_account['homepage'] or '',request_id='f711f711-f711-4f71-8f71-f711f711f711'))
            assert refused_account_after[0]==403,refused_account_after[0]
            with runtime.connect() as check_db:
                kept_account=check_db.execute('SELECT name,status,note,computer_id,slot,douyin_id,homepage FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert kept_account['note']==admitted_account_note and kept_account['status']==admitted_account['status']
                assert (kept_account['name'],kept_account['computer_id'],kept_account['slot'])==(admitted_account['name'],admitted_account['computer_id'],admitted_account['slot'])
                assert (kept_account['douyin_id'],kept_account['homepage'])==(admitted_account['douyin_id'],admitted_account['homepage'])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(save_account,)).fetchone()[0]==admitted_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?",(patch_id,)).fetchone()[0]==admitted_account_audit
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_account=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_manage' not in stored_admitted_account and 'account_status' in stored_admitted_account
                check_db.execute('UPDATE accounts SET note=? WHERE id=?',(admitted_account['note'],patch_id))
                restored_account=check_db.execute('SELECT note,status FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert restored_account['note']==admitted_account['note'] and restored_account['status']==admitted_account['status']
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='f723f723-f723-4f23-8f23-f723f723f723'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            task_board=get(employee_client,'/api/board')
            task_row=next(row for row in task_board['phone_cards']['cards'] if row['id']==card_update_id)
            assert set(task_row['office_ids'])=={office_a}
            with runtime.connect() as raw_db:
                task_before=raw_db.execute('SELECT label,phone,status,code_state FROM phone_cards WHERE id=?',(card_update_id,)).fetchone()
                task_offices=sorted(row[0] for row in raw_db.execute('SELECT office_id FROM phone_card_offices WHERE card_id=?',(card_update_id,)).fetchall())
                task_history_kept=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(card_key,)).fetchone()[0]
                task_audit_kept=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card' AND target=?",(str(card_update_id),)).fetchone()[0]
            assert task_offices==[office_a] and task_before['phone']=='19900000011'
            task_status='欠费' if task_before['status']!='欠费' else '正常'
            task_wait_body=dict(id=card_update_id,phone='19900000011',label=task_before['label'],status=task_status,office_ids=[office_a],request_id='f811f811-f811-4f81-8f81-f811f811f811',_expected_versions={card_key:task_board['record_versions'][card_key]})
            task_kept=[item for item in auth.PERMISSIONS if item!='card_tasks']
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/phone-card',task_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Card task permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=task_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                task_waited=future.result(timeout=10)
                assert task_waited[0]==401,task_waited[0]
            task_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==card_update_id for row in get(task_reduced,'/api/board')['phone_cards']['cards'])
            refused_task=post(task_reduced,'/api/phone-card',dict(id=card_update_id,phone='19900000011',label=task_before['label'],status=task_status,office_ids=[office_a],request_id='f812f812-f812-4f82-8f82-f812f812f812'))
            assert refused_task[0]==403,refused_task[0]
            with runtime.connect() as check_db:
                task_after=check_db.execute('SELECT label,phone,status,code_state FROM phone_cards WHERE id=?',(card_update_id,)).fetchone()
                assert (task_after['label'],task_after['phone'],task_after['status'],task_after['code_state'])==(task_before['label'],task_before['phone'],task_before['status'],task_before['code_state'])
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM phone_card_offices WHERE card_id=?',(card_update_id,)).fetchall())==task_offices
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(card_key,)).fetchone()[0]==task_history_kept
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card' AND target=?",(str(card_update_id),)).fetchone()[0]==task_audit_kept
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('f811f811-f811-4f81-8f81-f811f811f811',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_task_permissions=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'card_tasks' not in stored_task_permissions and 'card_manage' in stored_task_permissions
            assert get(task_reduced,'/api/board')['record_versions'][card_key]==task_wait_body['_expected_versions'][card_key]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='f823f823-f823-4f23-8f23-f823f823f823'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            with runtime.connect() as raw_db:
                admitted_task=raw_db.execute('SELECT label,phone,status,code_state FROM phone_cards WHERE id=?',(card_update_id,)).fetchone()
                admitted_task_offices=sorted(row[0] for row in raw_db.execute('SELECT office_id FROM phone_card_offices WHERE card_id=?',(card_update_id,)).fetchall())
                admitted_task_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(card_key,)).fetchone()[0]
                admitted_task_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card' AND target=?",(str(card_update_id),)).fetchone()[0]
            assert admitted_task_offices==[office_a] and admitted_task['phone']=='19900000011'
            admitted_task_status='欠费' if admitted_task['status']!='欠费' else '正常'
            def revoke_card_tasks():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=task_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_card_tasks)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Card task revoke did not wait for the admitted card status write')
                holder.execute('UPDATE phone_cards SET status=? WHERE id=?',(admitted_task_status,card_update_id))
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted card status write')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_task_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==card_update_id for row in get(admitted_task_client,'/api/board')['phone_cards']['cards'])
            refused_task_after=post(admitted_task_client,'/api/phone-card',dict(id=card_update_id,phone='19900000011',label=admitted_task['label'],status=admitted_task['status'],office_ids=[office_a],request_id='f911f911-f911-4f91-8f91-f911f911f911'))
            assert refused_task_after[0]==403,refused_task_after[0]
            with runtime.connect() as check_db:
                kept_task=check_db.execute('SELECT label,phone,status,code_state FROM phone_cards WHERE id=?',(card_update_id,)).fetchone()
                assert kept_task['status']==admitted_task_status and kept_task['label']==admitted_task['label'] and kept_task['phone']==admitted_task['phone'] and kept_task['code_state']==admitted_task['code_state']
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM phone_card_offices WHERE card_id=?',(card_update_id,)).fetchall())==admitted_task_offices
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(card_key,)).fetchone()[0]==admitted_task_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card' AND target=?",(str(card_update_id),)).fetchone()[0]==admitted_task_audit
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_task=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'card_tasks' not in stored_admitted_task and 'card_manage' in stored_admitted_task
                check_db.execute('UPDATE phone_cards SET status=? WHERE id=?',(admitted_task['status'],card_update_id))
                restored_task=check_db.execute('SELECT label,phone,status FROM phone_cards WHERE id=?',(card_update_id,)).fetchone()
                assert restored_task['status']==admitted_task['status'] and restored_task['label']==admitted_task['label'] and restored_task['phone']==admitted_task['phone']
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='f923f923-f923-4f23-8f23-f923f923f923'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            import_preview=preview_csv('抖音号,昵称\nsynthetic-lock-import,合成锁等待导入\n',office_id=office_a)
            assert import_preview['rows'][0]['action']=='create'
            import_wait_body=dict(preview_id=import_preview['preview_id'],rows=[2],request_id='a311a311-a311-4a31-8a31-a311a311a311',_expected_versions={})
            import_kept=[item for item in auth.PERMISSIONS if item!='data_import']
            with runtime.connect() as raw_db:
                import_preview_kept=raw_db.execute('SELECT count(*) FROM operations_previews WHERE id=?',(import_preview['preview_id'],)).fetchone()[0]
                import_audit_kept=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/import-apply' AND target=''").fetchone()[0]
                import_accounts_before=raw_db.execute("SELECT count(*) FROM accounts WHERE douyin_id='synthetic-lock-import'").fetchone()[0]
            assert import_preview_kept==1 and import_accounts_before==0
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/workspace/import-apply',import_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Import permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=import_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                import_waited=future.result(timeout=10)
                assert import_waited[0]==401,import_waited[0]
            import_reduced=test_auth_support.client(base,'synthetic_employee')
            refused_import=post(import_reduced,'/api/workspace/import-apply',dict(preview_id=import_preview['preview_id'],rows=[2],request_id='a312a312-a312-4a32-8a32-a312a312a312',_expected_versions={}))
            assert refused_import[0]==403,refused_import[0]
            with runtime.connect() as check_db:
                assert check_db.execute('SELECT count(*) FROM operations_previews WHERE id=?',(import_preview['preview_id'],)).fetchone()[0]==1
                assert check_db.execute("SELECT count(*) FROM accounts WHERE douyin_id='synthetic-lock-import'").fetchone()[0]==0
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/import-apply' AND target=''").fetchone()[0]==import_audit_kept
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('a311a311-a311-4a31-8a31-a311a311a311',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_import_permissions=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'data_import' not in stored_import_permissions and 'data_export' in stored_import_permissions
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='a323a323-a323-4a23-8a23-a323a323a323'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_import_preview=preview_csv('抖音号,昵称\nsynthetic-admitted-import,合成已准入导入\n',office_id=office_a)
            assert admitted_import_preview['rows'][0]['action']=='create'
            admitted_import_kept=[item for item in auth.PERMISSIONS if item!='data_import']
            with runtime.connect() as raw_db:
                admitted_import_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/import-apply' AND target=''").fetchone()[0]
                assert raw_db.execute('SELECT count(*) FROM operations_previews WHERE id=?',(admitted_import_preview['preview_id'],)).fetchone()[0]==1
                assert raw_db.execute("SELECT count(*) FROM accounts WHERE douyin_id='synthetic-admitted-import'").fetchone()[0]==0
            def revoke_import():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_import_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_import)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Import revoke did not wait for the admitted import')
                admitted_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                import data_exchange
                admitted_applied=data_exchange.apply_import(holder,admitted_actor,dict(preview_id=admitted_import_preview['preview_id'],rows=[2]),day)
                assert admitted_applied['imported']==1
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted import')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_import_client=test_auth_support.client(base,'synthetic_employee')
            refused_admitted_import=post(admitted_import_client,'/api/workspace/import-apply',dict(preview_id=admitted_import_preview['preview_id'],rows=[2],request_id='a411a411-a411-4a41-8a41-a411a411a411',_expected_versions={}))
            assert refused_admitted_import[0]==403,refused_admitted_import[0]
            with runtime.connect() as check_db:
                admitted_import_row=check_db.execute("SELECT id FROM accounts WHERE douyin_id='synthetic-admitted-import'").fetchone()
                assert admitted_import_row is not None
                admitted_import_id=admitted_import_row[0]
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM account_offices WHERE account_id=?',(admitted_import_id,)).fetchall())==[office_a]
                assert check_db.execute('SELECT count(*) FROM operations_previews WHERE id=?',(admitted_import_preview['preview_id'],)).fetchone()[0]==0
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/import-apply' AND target=''").fetchone()[0]==admitted_import_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('a411a411-a411-4a41-8a41-a411a411a411',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_import=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'data_import' not in stored_admitted_import and 'data_export' in stored_admitted_import
                check_db.execute('DELETE FROM account_offices WHERE account_id=?',(admitted_import_id,))
                check_db.execute('DELETE FROM accounts WHERE id=?',(admitted_import_id,))
                assert check_db.execute("SELECT count(*) FROM accounts WHERE douyin_id='synthetic-admitted-import'").fetchone()[0]==0
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='a423a423-a423-4a23-8a23-a423a423a423'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            create_wait_body=dict(name='合成锁等待新增账号',computer_id='pc-a',slot=20,status='正常',office_id=office_a,request_id='a511a511-a511-4a51-8a51-a511a511a511',_expected_versions={})
            create_kept=[item for item in auth.PERMISSIONS if item!='account_create']
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT count(*) FROM accounts WHERE computer_id='pc-a' AND slot=20").fetchone()[0]==0
                create_audit_kept=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
                create_accounts_before=raw_db.execute("SELECT count(*) FROM accounts WHERE name='合成锁等待新增账号'").fetchone()[0]
            assert create_accounts_before==0
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/account/create',create_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Account create permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=create_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                create_waited=future.result(timeout=10)
                assert create_waited[0]==401,create_waited[0]
            create_reduced=test_auth_support.client(base,'synthetic_employee')
            refused_create=post(create_reduced,'/api/account/create',dict(name='合成锁等待新增账号',computer_id='pc-a',slot=20,status='正常',office_id=office_a,request_id='a512a512-a512-4a52-8a52-a512a512a512',_expected_versions={}))
            assert refused_create[0]==403,refused_create[0]
            with runtime.connect() as check_db:
                assert check_db.execute("SELECT count(*) FROM accounts WHERE name='合成锁等待新增账号'").fetchone()[0]==0
                assert check_db.execute("SELECT count(*) FROM accounts WHERE computer_id='pc-a' AND slot=20").fetchone()[0]==0
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]==create_audit_kept
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('a511a511-a511-4a51-8a51-a511a511a511',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_create_permissions=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_create' not in stored_create_permissions and 'account_manage' in stored_create_permissions
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='a523a523-a523-4a23-8a23-a523a523a523'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_create_kept=[item for item in auth.PERMISSIONS if item!='account_create']
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT count(*) FROM accounts WHERE computer_id='pc-a' AND slot=21").fetchone()[0]==0
                admitted_create_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
                assert raw_db.execute("SELECT count(*) FROM accounts WHERE name='合成已准入新增账号'").fetchone()[0]==0
            def revoke_account_create():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_create_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_account_create)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Account create revoke did not wait for the admitted account create')
                admitted_create_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                import account_service
                admitted_create_id=account_service.save(holder,dict(name='合成已准入新增账号',computer_id='pc-a',slot=21,status='正常',office_id=office_a),admitted_create_actor,day,create=True)
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted account create')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_create_client=test_auth_support.client(base,'synthetic_employee')
            refused_admitted_create=post(admitted_create_client,'/api/account/create',dict(name='合成已准入新增账号',computer_id='pc-a',slot=21,status='正常',office_id=office_a,request_id='a611a611-a611-4a61-8a61-a611a611a611',_expected_versions={}))
            assert refused_admitted_create[0]==403,refused_admitted_create[0]
            with runtime.connect() as check_db:
                admitted_created=check_db.execute('SELECT name,computer_id,slot,status FROM accounts WHERE id=?',(admitted_create_id,)).fetchone()
                assert admitted_created['name']=='合成已准入新增账号' and admitted_created['computer_id']=='pc-a' and admitted_created['slot']==21 and admitted_created['status']=='正常'
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM account_offices WHERE account_id=?',(admitted_create_id,)).fetchall())==[office_a]
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]==admitted_create_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('a611a611-a611-4a61-8a61-a611a611a611',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_create=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_create' not in stored_admitted_create and 'account_manage' in stored_admitted_create
                check_db.execute('DELETE FROM account_offices WHERE account_id=?',(admitted_create_id,))
                check_db.execute('DELETE FROM accounts WHERE id=?',(admitted_create_id,))
                assert check_db.execute("SELECT count(*) FROM accounts WHERE name='合成已准入新增账号'").fetchone()[0]==0
                assert check_db.execute("SELECT count(*) FROM accounts WHERE computer_id='pc-a' AND slot=21").fetchone()[0]==0
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='a623a623-a623-4a23-8a23-a623a623a623'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            card_create_body=dict(phone='19900000021',office_ids=[office_a],request_id='a711a711-a711-4a71-8a71-a711a711a711',_expected_versions={})
            card_create_kept=[item for item in auth.PERMISSIONS if item!='card_create']
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT count(*) FROM phone_cards WHERE phone='19900000021'").fetchone()[0]==0
                card_create_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/phone-card',card_create_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Card create permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=card_create_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                card_create_waited=future.result(timeout=10)
                assert card_create_waited[0]==401,card_create_waited[0]
            card_create_reduced=test_auth_support.client(base,'synthetic_employee')
            refused_card_create=post(card_create_reduced,'/api/phone-card',dict(phone='19900000021',office_ids=[office_a],request_id='a712a712-a712-4a72-8a72-a712a712a712',_expected_versions={}))
            assert refused_card_create[0]==403,refused_card_create[0]
            with runtime.connect() as check_db:
                assert check_db.execute("SELECT count(*) FROM phone_cards WHERE phone='19900000021'").fetchone()[0]==0
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]==card_create_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('a711a711-a711-4a71-8a71-a711a711a711',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_card_create=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'card_create' not in stored_card_create and 'card_manage' in stored_card_create and 'card_tasks' in stored_card_create
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='a723a723-a723-4a23-8a23-a723a723a723'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_card_create_kept=[item for item in auth.PERMISSIONS if item!='card_create']
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT count(*) FROM phone_cards WHERE phone='19900000022'").fetchone()[0]==0
                admitted_card_create_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            def revoke_card_create():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_card_create_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_card_create)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Card create revoke did not wait for the admitted card create')
                admitted_card_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_card_body=dict(phone='19900000022',office_ids=[office_a])
                auth.authorize_route(holder,admitted_card_actor,'/api/phone-card',admitted_card_body)
                import phone_cards
                admitted_card_id=phone_cards.save(holder,admitted_card_body,admitted_card_actor)
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted card create')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_card_create_client=test_auth_support.client(base,'synthetic_employee')
            refused_admitted_card_create=post(admitted_card_create_client,'/api/phone-card',dict(phone='19900000022',office_ids=[office_a],request_id='a811a811-a811-4a81-8a81-a811a811a811',_expected_versions={}))
            assert refused_admitted_card_create[0]==403,refused_admitted_card_create[0]
            with runtime.connect() as check_db:
                admitted_card_row=check_db.execute('SELECT phone FROM phone_cards WHERE id=?',(admitted_card_id,)).fetchone()
                assert admitted_card_row is not None and admitted_card_row[0]=='19900000022'
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM phone_card_offices WHERE card_id=?',(admitted_card_id,)).fetchall())==[office_a]
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]==admitted_card_create_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('a811a811-a811-4a81-8a81-a811a811a811',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_card_create=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'card_create' not in stored_admitted_card_create and 'card_manage' in stored_admitted_card_create and 'card_tasks' in stored_admitted_card_create
                check_db.execute('DELETE FROM phone_card_offices WHERE card_id=?',(admitted_card_id,))
                check_db.execute('DELETE FROM phone_cards WHERE id=?',(admitted_card_id,))
                assert check_db.execute("SELECT count(*) FROM phone_cards WHERE phone='19900000022'").fetchone()[0]==0
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='a823a823-a823-4a23-8a23-a823a823a823'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            device_create_body=dict(id='20',state='在用',office_ids=[office_a],request_id='a911a911-a911-4a91-8a91-a911a911a911',_expected_versions={})
            device_create_kept=[item for item in auth.PERMISSIONS if item!='device_create']
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT count(*) FROM phone_devices WHERE id='20'").fetchone()[0]==0
                device_create_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device/create'").fetchone()[0]
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/phone-device/create',device_create_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Device create permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=device_create_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                device_create_waited=future.result(timeout=10)
                assert device_create_waited[0]==401,device_create_waited[0]
            device_create_reduced=test_auth_support.client(base,'synthetic_employee')
            refused_device_create=post(device_create_reduced,'/api/phone-device/create',dict(id='20',state='在用',office_ids=[office_a],request_id='a912a912-a912-4a92-8a92-a912a912a912',_expected_versions={}))
            assert refused_device_create[0]==403,refused_device_create[0]
            with runtime.connect() as check_db:
                assert check_db.execute("SELECT count(*) FROM phone_devices WHERE id='20'").fetchone()[0]==0
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device/create'").fetchone()[0]==device_create_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('a911a911-a911-4a91-8a91-a911a911a911',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_device_create=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'device_create' not in stored_device_create and 'device_manage' in stored_device_create
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='a923a923-a923-4a23-8a23-a923a923a923'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_device_create_kept=[item for item in auth.PERMISSIONS if item!='device_create']
            with runtime.connect() as raw_db:
                assert raw_db.execute("SELECT count(*) FROM phone_devices WHERE id='21'").fetchone()[0]==0
                admitted_device_create_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device/create'").fetchone()[0]
            def revoke_device_create():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_device_create_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_device_create)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Device create revoke did not wait for the admitted device create')
                admitted_device_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_device_body=dict(id='21',state='在用',office_ids=[office_a])
                auth.authorize_route(holder,admitted_device_actor,'/api/phone-device/create',admitted_device_body)
                import phone_devices
                admitted_device_id=phone_devices.save(holder,admitted_device_body,create=True,actor=admitted_device_actor)
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted device create')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_device_create_client=test_auth_support.client(base,'synthetic_employee')
            refused_admitted_device_create=post(admitted_device_create_client,'/api/phone-device/create',dict(id='21',state='在用',office_ids=[office_a],request_id='c011c011-c011-4c01-8c01-c011c011c011',_expected_versions={}))
            assert refused_admitted_device_create[0]==403,refused_admitted_device_create[0]
            with runtime.connect() as check_db:
                admitted_device_row=check_db.execute('SELECT name,state FROM phone_devices WHERE id=?',(admitted_device_id,)).fetchone()
                assert admitted_device_id=='21' and admitted_device_row['state']=='在用'
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM phone_device_offices WHERE device_id=?',(admitted_device_id,)).fetchall())==[office_a]
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device/create'").fetchone()[0]==admitted_device_create_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('c011c011-c011-4c01-8c01-c011c011c011',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_device_create=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'device_create' not in stored_admitted_device_create and 'device_manage' in stored_admitted_device_create
                check_db.execute('DELETE FROM phone_device_offices WHERE device_id=?',(admitted_device_id,))
                check_db.execute('DELETE FROM phone_devices WHERE id=?',(admitted_device_id,))
                assert check_db.execute("SELECT count(*) FROM phone_devices WHERE id='21'").fetchone()[0]==0
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='c023c023-c023-4c23-8c23-c023c023c023'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            card_import_body=dict(office_ids=[office_a],request_id='c111c111-c111-4c11-8c11-c111c111c111',_expected_versions={})
            card_import_kept=[item for item in auth.PERMISSIONS if item!='card_import']
            with runtime.connect() as raw_db:
                assert raw_db.execute('SELECT registration_phone FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==import_phone
                assert raw_db.execute('SELECT 1 FROM phone_cards WHERE phone=?',(import_phone,)).fetchone() is None
                card_import_count=raw_db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                card_import_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-cards/import'").fetchone()[0]
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/phone-cards/import',card_import_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Card import permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=card_import_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                card_import_waited=future.result(timeout=10)
                assert card_import_waited[0]==401,card_import_waited[0]
            card_import_reduced=test_auth_support.client(base,'synthetic_employee')
            refused_card_import=post(card_import_reduced,'/api/phone-cards/import',dict(office_ids=[office_a],request_id='c112c112-c112-4c12-8c12-c112c112c112',_expected_versions={}))
            assert refused_card_import[0]==403,refused_card_import[0]
            with runtime.connect() as check_db:
                assert check_db.execute('SELECT 1 FROM phone_cards WHERE phone=?',(import_phone,)).fetchone() is None
                assert check_db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]==card_import_count
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-cards/import'").fetchone()[0]==card_import_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('c111c111-c111-4c11-8c11-c111c111c111',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_card_import=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'card_import' not in stored_card_import and 'card_create' in stored_card_import and 'card_manage' in stored_card_import
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='c123c123-c123-4c23-8c23-c123c123c123'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_card_import_kept=[item for item in auth.PERMISSIONS if item!='card_import']
            with runtime.connect() as raw_db:
                existing_card_ids={row[0] for row in raw_db.execute('SELECT id FROM phone_cards').fetchall()}
                assert raw_db.execute('SELECT 1 FROM phone_cards WHERE phone=?',(import_phone,)).fetchone() is None
                assert 1 in existing_card_ids
                admitted_card_import_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-cards/import'").fetchone()[0]
            def revoke_card_import():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_card_import_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_card_import)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Card import revoke did not wait for the admitted card import')
                admitted_card_import_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_card_import_body=dict(office_ids=[office_a])
                auth.authorize_route(holder,admitted_card_import_actor,'/api/phone-cards/import',admitted_card_import_body)
                import phone_cards
                admitted_card_import_added=phone_cards.import_accounts(holder,actor=admitted_card_import_actor,body=admitted_card_import_body)
                assert admitted_card_import_added>=1
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted card import')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_card_import_client=test_auth_support.client(base,'synthetic_employee')
            refused_admitted_card_import=post(admitted_card_import_client,'/api/phone-cards/import',dict(office_ids=[office_a],request_id='c211c211-c211-4c21-8c21-c211c211c211',_expected_versions={}))
            assert refused_admitted_card_import[0]==403,refused_admitted_card_import[0]
            with runtime.connect() as check_db:
                admitted_import_ids={row[0] for row in check_db.execute('SELECT id FROM phone_cards').fetchall()}
                new_card_ids=admitted_import_ids-existing_card_ids
                imported_card_id=check_db.execute('SELECT id FROM phone_cards WHERE phone=?',(import_phone,)).fetchone()[0]
                assert imported_card_id in new_card_ids and 1 not in new_card_ids and len(new_card_ids)==admitted_card_import_added
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM phone_card_offices WHERE card_id=?',(imported_card_id,)).fetchall())==[office_a]
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-cards/import'").fetchone()[0]==admitted_card_import_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('c211c211-c211-4c21-8c21-c211c211c211',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_card_import=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'card_import' not in stored_admitted_card_import and 'card_create' in stored_admitted_card_import and 'card_manage' in stored_admitted_card_import
                for new_card_id in new_card_ids:
                    check_db.execute('DELETE FROM phone_card_offices WHERE card_id=?',(new_card_id,))
                    check_db.execute('DELETE FROM phone_cards WHERE id=?',(new_card_id,))
                assert check_db.execute('SELECT 1 FROM phone_cards WHERE phone=?',(import_phone,)).fetchone() is None
                assert {row[0] for row in check_db.execute('SELECT id FROM phone_cards').fetchall()}==existing_card_ids
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='c223c223-c223-4c23-8c23-c223c223c223'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            batch_wait_board=get(employee_client,'/api/board')
            batch_wait_account='account:'+patch_id
            batch_wait_kept=[item for item in auth.PERMISSIONS if item!='account_status']
            with runtime.connect() as raw_db:
                batch_wait_status=raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]
                batch_wait_other=raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]
                batch_wait_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(batch_wait_account,)).fetchone()[0]
                batch_wait_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit' AND target=''").fetchone()[0]
            assert batch_wait_status==bulk_before and batch_wait_other==account_a_status and batch_wait_status!='短期封禁'
            batch_wait_body=dict(kind='accounts',ids=[patch_id],patch={'status':'短期封禁'},request_id='c311c311-c311-4c31-8c31-c311c311c311',_expected_versions={batch_wait_account:batch_wait_board['record_versions'][batch_wait_account]})
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/batch-edit',batch_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Batch permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=batch_wait_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                batch_waited=future.result(timeout=10)
                assert batch_waited[0]==401,batch_waited[0]
            batch_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(batch_reduced,'/api/board')['accounts'])
            refused_batch=post(batch_reduced,'/api/batch-edit',dict(kind='accounts',ids=[patch_id],patch={'status':'短期封禁'},request_id='c312c312-c312-4c32-8c32-c312c312c312',_expected_versions={batch_wait_account:batch_wait_board['record_versions'][batch_wait_account]}))
            assert refused_batch[0]==403,refused_batch[0]
            with runtime.connect() as check_db:
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==batch_wait_status
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]==batch_wait_other
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(batch_wait_account,)).fetchone()[0]==batch_wait_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit' AND target=''").fetchone()[0]==batch_wait_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('c311c311-c311-4c31-8c31-c311c311c311',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_batch_permissions=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_status' not in stored_batch_permissions and 'account_progress' in stored_batch_permissions
            assert get(batch_reduced,'/api/board')['record_versions'][batch_wait_account]==batch_wait_body['_expected_versions'][batch_wait_account]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='c323c323-c323-4c23-8c23-c323c323c323'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_batch_kept=[item for item in auth.PERMISSIONS if item!='account_status']
            with runtime.connect() as raw_db:
                admitted_batch_before=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()
                admitted_batch_other=raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]
                admitted_batch_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+patch_id,)).fetchone()[0]
                admitted_batch_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit' AND target=''").fetchone()[0]
            assert admitted_batch_before[0]==bulk_before and admitted_batch_before[0]!='短期封禁' and admitted_batch_other==account_a_status
            def revoke_batch_status():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_batch_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_batch_status)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Batch status revoke did not wait for the admitted batch write')
                admitted_batch_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_batch_body=dict(kind='accounts',ids=[patch_id],patch={'status':'短期封禁'})
                auth.authorize_route(holder,admitted_batch_actor,'/api/batch-edit',admitted_batch_body)
                import batch_edit
                admitted_batch_result=batch_edit.apply(holder,admitted_batch_actor,admitted_batch_body,day)
                assert admitted_batch_result.get('count')==1
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted batch write')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_batch_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(admitted_batch_client,'/api/board')['accounts'])
            admitted_batch_version=get(admitted_batch_client,'/api/board')['record_versions']['account:'+patch_id]
            refused_admitted_batch=post(admitted_batch_client,'/api/batch-edit',dict(kind='accounts',ids=[patch_id],patch={'status':admitted_batch_before[0]},request_id='c411c411-c411-4c41-8c41-c411c411c411',_expected_versions={'account:'+patch_id:admitted_batch_version}))
            assert refused_admitted_batch[0]==403,refused_admitted_batch[0]
            with runtime.connect() as check_db:
                kept_batch=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert kept_batch[0]=='短期封禁' and kept_batch[1]==admitted_batch_before[1]
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]==admitted_batch_other
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+patch_id,)).fetchone()[0]==admitted_batch_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit' AND target=''").fetchone()[0]==admitted_batch_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('c411c411-c411-4c41-8c41-c411c411c411',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_batch=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_status' not in stored_admitted_batch and 'account_progress' in stored_admitted_batch
                check_db.execute('UPDATE accounts SET status=? WHERE id=?',(admitted_batch_before[0],patch_id))
                restored_batch=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert restored_batch[0]==admitted_batch_before[0] and restored_batch[1]==admitted_batch_before[1]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='c423c423-c423-4c23-8c23-c423c423c423'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            bulk_wait_board=get(employee_client,'/api/board')
            bulk_wait_account='account:'+patch_id
            bulk_wait_kept=[item for item in auth.PERMISSIONS if item!='account_status']
            with runtime.connect() as raw_db:
                bulk_wait_row=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()
                bulk_wait_other=raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]
                bulk_wait_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(bulk_wait_account,)).fetchone()[0]
                bulk_wait_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/bulk-status' AND target=''").fetchone()[0]
            assert bulk_wait_row[0]==bulk_before and bulk_wait_row[0]!='封禁' and bulk_wait_other==account_a_status
            bulk_wait_body=dict(ids=[patch_id],status='封禁',request_id='c511c511-c511-4c51-8c51-c511c511c511',_expected_versions={bulk_wait_account:bulk_wait_board['record_versions'][bulk_wait_account]})
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/bulk-status',bulk_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Bulk permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=bulk_wait_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                bulk_waited=future.result(timeout=10)
                assert bulk_waited[0]==401,bulk_waited[0]
            bulk_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(bulk_reduced,'/api/board')['accounts'])
            refused_bulk=post(bulk_reduced,'/api/bulk-status',dict(ids=[patch_id],status='封禁',request_id='c512c512-c512-4c52-8c52-c512c512c512',_expected_versions={bulk_wait_account:bulk_wait_board['record_versions'][bulk_wait_account]}))
            assert refused_bulk[0]==403,refused_bulk[0]
            with runtime.connect() as check_db:
                kept_bulk_wait=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert kept_bulk_wait[0]==bulk_wait_row[0] and kept_bulk_wait[1]==bulk_wait_row[1]
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]==bulk_wait_other
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(bulk_wait_account,)).fetchone()[0]==bulk_wait_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/bulk-status' AND target=''").fetchone()[0]==bulk_wait_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('c511c511-c511-4c51-8c51-c511c511c511',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_bulk_permissions=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_status' not in stored_bulk_permissions and 'account_progress' in stored_bulk_permissions
            assert get(bulk_reduced,'/api/board')['record_versions'][bulk_wait_account]==bulk_wait_body['_expected_versions'][bulk_wait_account]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='c523c523-c523-4c23-8c23-c523c523c523'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_bulk_kept=[item for item in auth.PERMISSIONS if item!='account_status']
            with runtime.connect() as raw_db:
                admitted_bulk_before=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()
                admitted_bulk_other=raw_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]
                admitted_bulk_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+patch_id,)).fetchone()[0]
                admitted_bulk_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/bulk-status' AND target=''").fetchone()[0]
            assert admitted_bulk_before[0]==bulk_before and admitted_bulk_before[0]!='封禁' and admitted_bulk_other==account_a_status
            def revoke_bulk_status():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_bulk_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_bulk_status)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Bulk status revoke did not wait for the admitted bulk write')
                admitted_bulk_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_bulk_body=dict(ids=[patch_id],status='封禁')
                auth.authorize_route(holder,admitted_bulk_actor,'/api/bulk-status',admitted_bulk_body)
                holder.execute("UPDATE accounts SET nurture_started=CASE WHEN ?='养号7天' AND status<>'养号7天' THEN '' ELSE nurture_started END,status=? WHERE id=?",('封禁','封禁',patch_id))
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted bulk write')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_bulk_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(admitted_bulk_client,'/api/board')['accounts'])
            admitted_bulk_version=get(admitted_bulk_client,'/api/board')['record_versions']['account:'+patch_id]
            refused_admitted_bulk=post(admitted_bulk_client,'/api/bulk-status',dict(ids=[patch_id],status=admitted_bulk_before[0],request_id='c611c611-c611-4c61-8c61-c611c611c611',_expected_versions={'account:'+patch_id:admitted_bulk_version}))
            assert refused_admitted_bulk[0]==403,refused_admitted_bulk[0]
            with runtime.connect() as check_db:
                kept_admitted_bulk=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert kept_admitted_bulk[0]=='封禁' and kept_admitted_bulk[1]==admitted_bulk_before[1]
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',('account-a',)).fetchone()[0]==admitted_bulk_other
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+patch_id,)).fetchone()[0]==admitted_bulk_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/bulk-status' AND target=''").fetchone()[0]==admitted_bulk_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('c611c611-c611-4c61-8c61-c611c611c611',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_bulk=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_status' not in stored_admitted_bulk and 'account_progress' in stored_admitted_bulk
                check_db.execute('UPDATE accounts SET status=? WHERE id=?',(admitted_bulk_before[0],patch_id))
                restored_bulk=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert restored_bulk[0]==admitted_bulk_before[0] and restored_bulk[1]==admitted_bulk_before[1]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='c623c623-c623-4c23-8c23-c623c623c623'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            patch_wait_board=get(employee_client,'/api/board')
            patch_wait_account='account:'+patch_id
            patch_wait_kept=[item for item in auth.PERMISSIONS if item!='account_manage']
            with runtime.connect() as raw_db:
                patch_wait_before=raw_db.execute('SELECT name,status,note,computer_id,slot,douyin_id,homepage FROM accounts WHERE id=?',(patch_id,)).fetchone()
                patch_wait_other=raw_db.execute('SELECT note FROM accounts WHERE id=?',('account-a',)).fetchone()[0]
                patch_wait_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(patch_wait_account,)).fetchone()[0]
                patch_wait_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch' AND target=?",(patch_id,)).fetchone()[0]
            assert patch_wait_before['note']==save_note and patch_wait_before['note']!='合成锁等待局部备注'
            patch_wait_body=dict(id=patch_id,changes={'note':'合成锁等待局部备注'},request_id='c711c711-c711-4c71-8c71-c711c711c711',_expected_versions={patch_wait_account:patch_wait_board['record_versions'][patch_wait_account]})
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/account/patch',patch_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Patch permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=patch_wait_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                patch_waited=future.result(timeout=10)
                assert patch_waited[0]==401,patch_waited[0]
            patch_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(patch_reduced,'/api/board')['accounts'])
            refused_patch=post(patch_reduced,'/api/account/patch',dict(id=patch_id,changes={'note':'合成锁等待局部备注'},request_id='c712c712-c712-4c72-8c72-c712c712c712',_expected_versions={patch_wait_account:patch_wait_board['record_versions'][patch_wait_account]}))
            assert refused_patch[0]==403,refused_patch[0]
            with runtime.connect() as check_db:
                patch_after=check_db.execute('SELECT name,status,note,computer_id,slot,douyin_id,homepage FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert (patch_after['name'],patch_after['status'],patch_after['note'],patch_after['computer_id'],patch_after['slot'])==(patch_wait_before['name'],patch_wait_before['status'],patch_wait_before['note'],patch_wait_before['computer_id'],patch_wait_before['slot'])
                assert (patch_after['douyin_id'],patch_after['homepage'])==(patch_wait_before['douyin_id'],patch_wait_before['homepage'])
                assert check_db.execute('SELECT note FROM accounts WHERE id=?',('account-a',)).fetchone()[0]==patch_wait_other
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(patch_wait_account,)).fetchone()[0]==patch_wait_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch' AND target=?",(patch_id,)).fetchone()[0]==patch_wait_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('c711c711-c711-4c71-8c71-c711c711c711',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_patch_permissions=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_manage' not in stored_patch_permissions and 'account_status' in stored_patch_permissions
            assert get(patch_reduced,'/api/board')['record_versions'][patch_wait_account]==patch_wait_body['_expected_versions'][patch_wait_account]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='c723c723-c723-4c23-8c23-c723c723c723'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_patch_kept=[item for item in auth.PERMISSIONS if item!='account_manage']
            with runtime.connect() as raw_db:
                admitted_patch_before=raw_db.execute('SELECT name,status,note,computer_id,slot,douyin_id,homepage FROM accounts WHERE id=?',(patch_id,)).fetchone()
                admitted_patch_other=raw_db.execute('SELECT note FROM accounts WHERE id=?',('account-a',)).fetchone()[0]
                admitted_patch_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+patch_id,)).fetchone()[0]
                admitted_patch_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch' AND target=?",(patch_id,)).fetchone()[0]
            assert admitted_patch_before['note']==save_note and admitted_patch_before['note']!='合成已准入局部备注'
            def revoke_patch():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_patch_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_patch)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Patch revoke did not wait for the admitted patch write')
                admitted_patch_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_patch_body=dict(id=patch_id,changes={'note':'合成已准入局部备注'})
                auth.authorize_route(holder,admitted_patch_actor,'/api/account/patch',admitted_patch_body)
                import account_service
                assert account_service.patch(holder,admitted_patch_body,admitted_patch_actor,day)==patch_id
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted patch write')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_patch_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(admitted_patch_client,'/api/board')['accounts'])
            admitted_patch_version=get(admitted_patch_client,'/api/board')['record_versions']['account:'+patch_id]
            refused_admitted_patch=post(admitted_patch_client,'/api/account/patch',dict(id=patch_id,changes={'note':admitted_patch_before['note']},request_id='c811c811-c811-4c81-8c81-c811c811c811',_expected_versions={'account:'+patch_id:admitted_patch_version}))
            assert refused_admitted_patch[0]==403,refused_admitted_patch[0]
            with runtime.connect() as check_db:
                kept_patch=check_db.execute('SELECT name,status,note,computer_id,slot,douyin_id,homepage FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert kept_patch['note']=='合成已准入局部备注'
                assert (kept_patch['name'],kept_patch['status'],kept_patch['computer_id'],kept_patch['slot'])==(admitted_patch_before['name'],admitted_patch_before['status'],admitted_patch_before['computer_id'],admitted_patch_before['slot'])
                assert (kept_patch['douyin_id'],kept_patch['homepage'])==(admitted_patch_before['douyin_id'],admitted_patch_before['homepage'])
                assert check_db.execute('SELECT note FROM accounts WHERE id=?',('account-a',)).fetchone()[0]==admitted_patch_other
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+patch_id,)).fetchone()[0]==admitted_patch_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch' AND target=?",(patch_id,)).fetchone()[0]==admitted_patch_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('c811c811-c811-4c81-8c81-c811c811c811',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_patch=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_manage' not in stored_admitted_patch and 'account_status' in stored_admitted_patch
                check_db.execute('UPDATE accounts SET note=? WHERE id=?',(admitted_patch_before['note'],patch_id))
                restored_patch=check_db.execute('SELECT name,status,note,computer_id,slot FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert restored_patch['note']==admitted_patch_before['note'] and restored_patch['status']==admitted_patch_before['status']
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='c823c823-c823-4c23-8c23-c823c823c823'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            ready_wait_board=get(employee_client,'/api/board')
            ready_wait_account='account:'+patch_id
            ready_wait_kept=[item for item in auth.PERMISSIONS if item!='account_progress']
            with runtime.connect() as raw_db:
                ready_wait_before=raw_db.execute('SELECT card_stability,password_state,auxiliary_state,status FROM accounts WHERE id=?',(patch_id,)).fetchone()
                ready_wait_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(ready_wait_account,)).fetchone()[0]
                ready_wait_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/card-readiness' AND target=''").fetchone()[0]
            assert (ready_wait_before[0],ready_wait_before[1])==(readiness_before[0],readiness_before[1])
            ready_wait_stable='未稳定' if ready_wait_before[0]!='未稳定' else '已稳定'
            ready_wait_password='待确认' if ready_wait_before[1]!='待确认' else '未修改'
            ready_wait_body=dict(id=patch_id,card_stability=ready_wait_stable,password_state=ready_wait_password,request_id='c911c911-c911-4c91-8c91-c911c911c911',_expected_versions={ready_wait_account:ready_wait_board['record_versions'][ready_wait_account]})
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/account/card-readiness',ready_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Readiness permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=ready_wait_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                ready_waited=future.result(timeout=10)
                assert ready_waited[0]==401,ready_waited[0]
            ready_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(ready_reduced,'/api/board')['accounts'])
            refused_ready=post(ready_reduced,'/api/account/card-readiness',dict(id=patch_id,card_stability=ready_wait_stable,password_state=ready_wait_password,request_id='c912c912-c912-4c92-8c92-c912c912c912',_expected_versions={ready_wait_account:ready_wait_board['record_versions'][ready_wait_account]}))
            assert refused_ready[0]==403,refused_ready[0]
            with runtime.connect() as check_db:
                ready_after=check_db.execute('SELECT card_stability,password_state,auxiliary_state,status FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert (ready_after[0],ready_after[1],ready_after[2],ready_after[3])==(ready_wait_before[0],ready_wait_before[1],ready_wait_before[2],ready_wait_before[3])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(ready_wait_account,)).fetchone()[0]==ready_wait_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/card-readiness' AND target=''").fetchone()[0]==ready_wait_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('c911c911-c911-4c91-8c91-c911c911c911',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_ready_permissions=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_progress' not in stored_ready_permissions and 'account_status' in stored_ready_permissions
            assert get(ready_reduced,'/api/board')['record_versions'][ready_wait_account]==ready_wait_body['_expected_versions'][ready_wait_account]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='c923c923-c923-4c23-8c23-c923c923c923'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_ready_kept=[item for item in auth.PERMISSIONS if item!='account_progress']
            with runtime.connect() as raw_db:
                admitted_ready_before=raw_db.execute('SELECT card_stability,password_state,auxiliary_state,status FROM accounts WHERE id=?',(patch_id,)).fetchone()
                admitted_ready_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+patch_id,)).fetchone()[0]
                admitted_ready_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/card-readiness' AND target=''").fetchone()[0]
            assert (admitted_ready_before[0],admitted_ready_before[1])==(readiness_before[0],readiness_before[1])
            admitted_ready_stable='未稳定' if admitted_ready_before[0]!='未稳定' else '已稳定'
            admitted_ready_password='待确认' if admitted_ready_before[1]!='待确认' else '未修改'
            def revoke_readiness():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_ready_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_readiness)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Readiness revoke did not wait for the admitted readiness write')
                admitted_ready_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_ready_body=dict(id=patch_id,card_stability=admitted_ready_stable,password_state=admitted_ready_password)
                auth.authorize_route(holder,admitted_ready_actor,'/api/account/card-readiness',admitted_ready_body)
                import phone_cards
                phone_cards.update_readiness(holder,patch_id,admitted_ready_body)
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted readiness write')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_ready_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(admitted_ready_client,'/api/board')['accounts'])
            admitted_ready_version=get(admitted_ready_client,'/api/board')['record_versions']['account:'+patch_id]
            refused_admitted_ready=post(admitted_ready_client,'/api/account/card-readiness',dict(id=patch_id,card_stability=admitted_ready_before[0],password_state=admitted_ready_before[1],request_id='d011d011-d011-4d01-8d01-d011d011d011',_expected_versions={'account:'+patch_id:admitted_ready_version}))
            assert refused_admitted_ready[0]==403,refused_admitted_ready[0]
            with runtime.connect() as check_db:
                kept_ready=check_db.execute('SELECT card_stability,password_state,auxiliary_state,status FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert (kept_ready[0],kept_ready[1])==(admitted_ready_stable,admitted_ready_password)
                assert kept_ready[2]==admitted_ready_before[2] and kept_ready[3]==admitted_ready_before[3]
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+patch_id,)).fetchone()[0]==admitted_ready_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/card-readiness' AND target=''").fetchone()[0]==admitted_ready_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('d011d011-d011-4d01-8d01-d011d011d011',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_ready=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_progress' not in stored_admitted_ready and 'account_status' in stored_admitted_ready
                check_db.execute('UPDATE accounts SET card_stability=?,password_state=? WHERE id=?',(admitted_ready_before[0],admitted_ready_before[1],patch_id))
                restored_ready=check_db.execute('SELECT card_stability,password_state,auxiliary_state,status FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert (restored_ready[0],restored_ready[1],restored_ready[2],restored_ready[3])==(admitted_ready_before[0],admitted_ready_before[1],admitted_ready_before[2],admitted_ready_before[3])
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='d023d023-d023-4d23-8d23-d023d023d023'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            phone_wait_board=get(employee_client,'/api/board')
            phone_wait_account='account:'+patch_id
            phone_wait_kept=[item for item in auth.PERMISSIONS if item!='device_manage']
            with runtime.connect() as raw_db:
                phone_wait_account_device=raw_db.execute('SELECT phone_device_id FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]
                phone_wait_device=raw_db.execute('SELECT name,state,state_source FROM phone_devices WHERE id=?',('8',)).fetchone()
                phone_wait_offices=sorted(row[0] for row in raw_db.execute('SELECT office_id FROM phone_device_offices WHERE device_id=?',('8',)).fetchall())
                phone_wait_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(phone_wait_account,)).fetchone()[0]
                phone_wait_device_history=raw_db.execute("SELECT count(*) FROM operations_history WHERE entity='device:8'").fetchone()[0]
                phone_wait_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/phone-device' AND target=''").fetchone()[0]
            assert phone_wait_account_device is None and phone_wait_offices==[office_a] and phone_wait_device[1]=='在用'
            phone_wait_body=dict(id=patch_id,phone_device_id='8',request_id='d111d111-d111-4d11-8d11-d111d111d111',_expected_versions={phone_wait_account:phone_wait_board['record_versions'][phone_wait_account],'device:8':phone_wait_board['record_versions']['device:8']})
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/account/phone-device',phone_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Phone assign permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=phone_wait_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                phone_waited=future.result(timeout=10)
                assert phone_waited[0]==401,phone_waited[0]
            phone_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(phone_reduced,'/api/board')['accounts'])
            refused_phone=post(phone_reduced,'/api/account/phone-device',dict(id=patch_id,phone_device_id='8',request_id='d112d112-d112-4d12-8d12-d112d112d112',_expected_versions={phone_wait_account:phone_wait_board['record_versions'][phone_wait_account],'device:8':phone_wait_board['record_versions']['device:8']}))
            assert refused_phone[0]==403,refused_phone[0]
            with runtime.connect() as check_db:
                assert check_db.execute('SELECT phone_device_id FROM accounts WHERE id=?',(patch_id,)).fetchone()[0] is None
                phone_device_after=check_db.execute('SELECT name,state,state_source FROM phone_devices WHERE id=?',('8',)).fetchone()
                assert (phone_device_after[0],phone_device_after[1],phone_device_after[2])==(phone_wait_device[0],phone_wait_device[1],phone_wait_device[2])
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM phone_device_offices WHERE device_id=?',('8',)).fetchall())==phone_wait_offices
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(phone_wait_account,)).fetchone()[0]==phone_wait_history
                assert check_db.execute("SELECT count(*) FROM operations_history WHERE entity='device:8'").fetchone()[0]==phone_wait_device_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/phone-device' AND target=''").fetchone()[0]==phone_wait_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('d111d111-d111-4d11-8d11-d111d111d111',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_phone_permissions=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'device_manage' not in stored_phone_permissions and 'device_create' in stored_phone_permissions
            assert get(phone_reduced,'/api/board')['record_versions'][phone_wait_account]==phone_wait_body['_expected_versions'][phone_wait_account]
            assert get(phone_reduced,'/api/board')['record_versions']['device:8']==phone_wait_body['_expected_versions']['device:8']
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='d123d123-d123-4d23-8d23-d123d123d123'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_phone_kept=[item for item in auth.PERMISSIONS if item!='device_manage']
            with runtime.connect() as raw_db:
                admitted_phone_account=raw_db.execute('SELECT phone_device_id,phone_device_source,office_group FROM accounts WHERE id=?',(patch_id,)).fetchone()
                admitted_phone_device=raw_db.execute('SELECT name,state,state_source FROM phone_devices WHERE id=?',('8',)).fetchone()
                admitted_phone_offices=sorted(row[0] for row in raw_db.execute('SELECT office_id FROM phone_device_offices WHERE device_id=?',('8',)).fetchall())
                admitted_phone_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+patch_id,)).fetchone()[0]
                admitted_phone_device_history=raw_db.execute("SELECT count(*) FROM operations_history WHERE entity='device:8'").fetchone()[0]
                admitted_phone_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/phone-device' AND target=''").fetchone()[0]
            assert admitted_phone_account[0] is None and admitted_phone_offices==[office_a] and admitted_phone_device[1]=='在用'
            def revoke_phone_assign():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_phone_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_phone_assign)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Phone assign revoke did not wait for the admitted phone assignment')
                admitted_phone_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                auth.authorize_route(holder,admitted_phone_actor,'/api/account/phone-device',dict(id=patch_id,phone_device_id='8'))
                import phone_devices
                phone_devices.assign(holder,patch_id,'8')
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted phone assignment')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_phone_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(admitted_phone_client,'/api/board')['accounts'])
            admitted_phone_board=get(admitted_phone_client,'/api/board')
            refused_admitted_phone=post(admitted_phone_client,'/api/account/phone-device',dict(id=patch_id,phone_device_id=None,request_id='d211d211-d211-4d21-8d21-d211d211d211',_expected_versions={'account:'+patch_id:admitted_phone_board['record_versions']['account:'+patch_id],'device:8':admitted_phone_board['record_versions']['device:8']}))
            assert refused_admitted_phone[0]==403,refused_admitted_phone[0]
            with runtime.connect() as check_db:
                kept_phone=check_db.execute('SELECT phone_device_id FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]
                kept_device=check_db.execute('SELECT name,state FROM phone_devices WHERE id=?',('8',)).fetchone()
                assert kept_phone=='8' and kept_device[0]==admitted_phone_device[0] and kept_device[1]=='在用'
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM phone_device_offices WHERE device_id=?',('8',)).fetchall())==admitted_phone_offices
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+patch_id,)).fetchone()[0]==admitted_phone_history
                assert check_db.execute("SELECT count(*) FROM operations_history WHERE entity='device:8'").fetchone()[0]==admitted_phone_device_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/phone-device' AND target=''").fetchone()[0]==admitted_phone_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('d211d211-d211-4d21-8d21-d211d211d211',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_phone=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'device_manage' not in stored_admitted_phone and 'device_create' in stored_admitted_phone
                check_db.execute('UPDATE accounts SET phone_device_id=?,phone_device_source=?,office_group=? WHERE id=?',(admitted_phone_account[0],admitted_phone_account[1],admitted_phone_account[2],patch_id))
                check_db.execute('UPDATE phone_devices SET state=?,state_source=? WHERE id=?',(admitted_phone_device[1],admitted_phone_device[2],'8'))
                restored_phone=check_db.execute('SELECT phone_device_id,phone_device_source,office_group FROM accounts WHERE id=?',(patch_id,)).fetchone()
                restored_device=check_db.execute('SELECT name,state,state_source FROM phone_devices WHERE id=?',('8',)).fetchone()
                assert (restored_phone[0],restored_phone[1],restored_phone[2])==(admitted_phone_account[0],admitted_phone_account[1],admitted_phone_account[2])
                assert (restored_device[0],restored_device[1],restored_device[2])==(admitted_phone_device[0],admitted_phone_device[1],admitted_phone_device[2])
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='d223d223-d223-4d23-8d23-d223d223d223'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            nurture_wait_board=get(employee_client,'/api/board')
            nurture_wait_account='account:'+patch_id
            nurture_wait_kept=[item for item in auth.PERMISSIONS if item!='account_status']
            with runtime.connect() as raw_db:
                nurture_wait_row=raw_db.execute('SELECT nurture_started,status FROM accounts WHERE id=?',(patch_id,)).fetchone()
                nurture_wait_record=raw_db.execute('SELECT workflow,started_on FROM nurture_records WHERE account_id=?',(patch_id,)).fetchone()
                nurture_wait_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(nurture_wait_account,)).fetchone()[0]
                nurture_wait_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/nurture' AND target=''").fetchone()[0]
            assert nurture_wait_row[1]==bulk_before
            nurture_wait_started='2026-01-06' if nurture_wait_row[0]!='2026-01-06' else '2026-01-07'
            assert nurture_wait_started!=nurture_wait_row[0]
            nurture_wait_body=dict(id=patch_id,started=nurture_wait_started,request_id='d311d311-d311-4d31-8d31-d311d311d311',_expected_versions={nurture_wait_account:nurture_wait_board['record_versions'][nurture_wait_account]})
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/account/nurture',nurture_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture date permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=nurture_wait_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                nurture_waited=future.result(timeout=10)
                assert nurture_waited[0]==401,nurture_waited[0]
            nurture_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(nurture_reduced,'/api/board')['accounts'])
            refused_nurture_date=post(nurture_reduced,'/api/account/nurture',dict(id=patch_id,started=nurture_wait_started,request_id='d312d312-d312-4d32-8d32-d312d312d312',_expected_versions={nurture_wait_account:nurture_wait_board['record_versions'][nurture_wait_account]}))
            assert refused_nurture_date[0]==403,refused_nurture_date[0]
            with runtime.connect() as check_db:
                nurture_after=check_db.execute('SELECT nurture_started,status FROM accounts WHERE id=?',(patch_id,)).fetchone()
                nurture_after_record=check_db.execute('SELECT workflow,started_on FROM nurture_records WHERE account_id=?',(patch_id,)).fetchone()
                assert nurture_after[0]==nurture_wait_row[0] and nurture_after[1]==nurture_wait_row[1]
                if nurture_wait_record is None:assert nurture_after_record is None
                else:assert (nurture_after_record[0],nurture_after_record[1])==(nurture_wait_record[0],nurture_wait_record[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(nurture_wait_account,)).fetchone()[0]==nurture_wait_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/nurture' AND target=''").fetchone()[0]==nurture_wait_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('d311d311-d311-4d31-8d31-d311d311d311',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_nurture_date=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_status' not in stored_nurture_date and 'nurture_manage' in stored_nurture_date
            assert get(nurture_reduced,'/api/board')['record_versions'][nurture_wait_account]==nurture_wait_body['_expected_versions'][nurture_wait_account]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='d323d323-d323-4d23-8d23-d323d323d323'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_date_kept=[item for item in auth.PERMISSIONS if item!='account_status']
            with runtime.connect() as raw_db:
                admitted_date_before=raw_db.execute('SELECT nurture_started,status FROM accounts WHERE id=?',(patch_id,)).fetchone()
                admitted_date_record=raw_db.execute('SELECT workflow,started_on FROM nurture_records WHERE account_id=?',(patch_id,)).fetchone()
                admitted_date_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+patch_id,)).fetchone()[0]
                admitted_date_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/nurture' AND target=''").fetchone()[0]
            assert admitted_date_before[1]==bulk_before
            admitted_date_started='2026-01-06' if admitted_date_before[0]!='2026-01-06' else '2026-01-07'
            assert admitted_date_started!=admitted_date_before[0]
            def revoke_nurture_date():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_date_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_nurture_date)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture date revoke did not wait for the admitted date write')
                admitted_date_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_date_body=dict(id=patch_id,started=admitted_date_started)
                auth.authorize_route(holder,admitted_date_actor,'/api/account/nurture',admitted_date_body)
                import workspace_service
                admitted_date_result=workspace_service.post(holder,admitted_date_actor,'/api/account/nurture',admitted_date_body,None,day)
                assert admitted_date_result.get('ok') is True
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted nurture date write')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_date_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(admitted_date_client,'/api/board')['accounts'])
            admitted_date_version=get(admitted_date_client,'/api/board')['record_versions']['account:'+patch_id]
            refused_admitted_date=post(admitted_date_client,'/api/account/nurture',dict(id=patch_id,started=admitted_date_before[0],request_id='d411d411-d411-4d41-8d41-d411d411d411',_expected_versions={'account:'+patch_id:admitted_date_version}))
            assert refused_admitted_date[0]==403,refused_admitted_date[0]
            with runtime.connect() as check_db:
                kept_date=check_db.execute('SELECT nurture_started,status FROM accounts WHERE id=?',(patch_id,)).fetchone()
                kept_date_record=check_db.execute('SELECT workflow,started_on FROM nurture_records WHERE account_id=?',(patch_id,)).fetchone()
                assert kept_date[0]==admitted_date_started and kept_date[1]==admitted_date_before[1]
                if admitted_date_record is None:assert kept_date_record is None
                else:assert (kept_date_record[0],kept_date_record[1])==(admitted_date_record[0],admitted_date_record[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+patch_id,)).fetchone()[0]==admitted_date_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/nurture' AND target=''").fetchone()[0]==admitted_date_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('d411d411-d411-4d41-8d41-d411d411d411',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_date=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_status' not in stored_admitted_date and 'nurture_manage' in stored_admitted_date
                check_db.execute('UPDATE accounts SET nurture_started=? WHERE id=?',(admitted_date_before[0],patch_id))
                restored_date=check_db.execute('SELECT nurture_started,status FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert restored_date[0]==admitted_date_before[0] and restored_date[1]==admitted_date_before[1]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='d423d423-d423-4d23-8d23-d423d423d423'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            link_wait_board=get(employee_client,'/api/board')
            link_wait_key='layout:synthetic-batch:pc-a:4'
            link_wait_kept=[item for item in auth.PERMISSIONS if item!='publishing_manage']
            with runtime.connect() as raw_db:
                link_wait_cell=raw_db.execute("SELECT account_id,label,reason FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=4").fetchone()
                link_wait_manual=raw_db.execute("SELECT previous_reason,actor_id,updated_at FROM publishing_layout_links WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=4").fetchone()
                link_wait_fixed=[raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=?",(slot,)).fetchone()[0] for slot in (1,2,3)]
                link_wait_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(link_wait_key,)).fetchone()[0]
                link_wait_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/link' AND target=''").fetchone()[0]
                link_wait_named=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
                link_target_placed=raw_db.execute("SELECT 1 FROM publishing_layout WHERE batch='synthetic-batch' AND account_id=?",(patch_id,)).fetchone()
                link_target_group=raw_db.execute('SELECT office_group FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]
            assert link_wait_cell[0] and link_wait_cell[0]!=patch_id and link_wait_cell[1].strip() and link_target_placed is None and link_target_group!='three'
            assert link_wait_fixed==[None,'account-b','account-a']
            link_wait_body=dict(batch='synthetic-batch',computer_id='pc-a',slot=4,expected_account_id=link_wait_cell[0],action='link',account_id=patch_id,request_id='d511d511-d511-4d51-8d51-d511d511d511',_expected_versions={link_wait_key:link_wait_board['record_versions'][link_wait_key]})
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/publishing/link',link_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Publishing link permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=link_wait_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                link_waited=future.result(timeout=10)
                assert link_waited[0]==401,link_waited[0]
            link_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(link_reduced,'/api/board')['accounts'])
            refused_link=post(link_reduced,'/api/publishing/link',dict(batch='synthetic-batch',computer_id='pc-a',slot=4,expected_account_id=link_wait_cell[0],action='link',account_id=patch_id,request_id='d512d512-d512-4d52-8d52-d512d512d512',_expected_versions={link_wait_key:link_wait_board['record_versions'][link_wait_key]}))
            assert refused_link[0]==403,refused_link[0]
            with runtime.connect() as check_db:
                link_after=check_db.execute("SELECT account_id,label,reason FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=4").fetchone()
                link_after_manual=check_db.execute("SELECT previous_reason,actor_id,updated_at FROM publishing_layout_links WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=4").fetchone()
                link_after_fixed=[check_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=?",(slot,)).fetchone()[0] for slot in (1,2,3)]
                assert (link_after[0],link_after[1],link_after[2])==(link_wait_cell[0],link_wait_cell[1],link_wait_cell[2])
                assert (link_after_manual[0],link_after_manual[1],link_after_manual[2])==(link_wait_manual[0],link_wait_manual[1],link_wait_manual[2])
                assert link_after_fixed==link_wait_fixed
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(link_wait_key,)).fetchone()[0]==link_wait_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/link' AND target=''").fetchone()[0]==link_wait_audit
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]==link_wait_named
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('d511d511-d511-4d51-8d51-d511d511d511',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_link=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'publishing_manage' not in stored_link and 'publish' in stored_link
            assert get(link_reduced,'/api/board')['record_versions'][link_wait_key]==link_wait_body['_expected_versions'][link_wait_key]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='d523d523-d523-4d23-8d23-d523d523d523'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_link_kept=[item for item in auth.PERMISSIONS if item!='publishing_manage']
            with runtime.connect() as raw_db:
                admitted_link_cell=raw_db.execute("SELECT account_id,label,reason FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=4").fetchone()
                admitted_link_manual=raw_db.execute("SELECT previous_reason,actor_id,updated_at FROM publishing_layout_links WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=4").fetchone()
                admitted_link_fixed=[raw_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=?",(slot,)).fetchone()[0] for slot in (1,2,3)]
                admitted_link_history=raw_db.execute("SELECT count(*) FROM operations_history WHERE entity='layout:synthetic-batch:pc-a:4'").fetchone()[0]
                admitted_link_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/link' AND target=''").fetchone()[0]
                admitted_link_named=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
                admitted_link_placed=raw_db.execute("SELECT 1 FROM publishing_layout WHERE batch='synthetic-batch' AND account_id=?",(patch_id,)).fetchone()
            assert admitted_link_cell[0] and admitted_link_cell[0]!=patch_id and admitted_link_cell[1].strip() and admitted_link_placed is None
            assert admitted_link_fixed==[None,'account-b','account-a'] and admitted_link_manual is not None
            def revoke_link():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_link_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_link)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Publishing link revoke did not wait for the admitted link write')
                admitted_link_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_link_body=dict(batch='synthetic-batch',computer_id='pc-a',slot=4,expected_account_id=admitted_link_cell[0],action='link',account_id=patch_id)
                auth.authorize_route(holder,admitted_link_actor,'/api/publishing/link',admitted_link_body)
                import publishing_layout
                admitted_link_result=publishing_layout.link(holder,admitted_link_body,admitted_link_actor['id'])
                assert admitted_link_result.get('account_id')==patch_id
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted publishing link')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_link_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(admitted_link_client,'/api/board')['accounts'])
            admitted_link_version=get(admitted_link_client,'/api/board')['record_versions']['layout:synthetic-batch:pc-a:4']
            refused_admitted_link=post(admitted_link_client,'/api/publishing/link',dict(batch='synthetic-batch',computer_id='pc-a',slot=4,expected_account_id=patch_id,action='link',account_id=admitted_link_cell[0],request_id='d611d611-d611-4d61-8d61-d611d611d611',_expected_versions={'layout:synthetic-batch:pc-a:4':admitted_link_version}))
            assert refused_admitted_link[0]==403,refused_admitted_link[0]
            with runtime.connect() as check_db:
                kept_link=check_db.execute("SELECT account_id,label,reason FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=4").fetchone()
                kept_fixed=[check_db.execute("SELECT account_id FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=?",(slot,)).fetchone()[0] for slot in (1,2,3)]
                assert kept_link[0]==patch_id and kept_link[1]==admitted_link_cell[1]
                assert kept_fixed==admitted_link_fixed
                assert check_db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=4").fetchone()[0]==1
                assert check_db.execute("SELECT count(*) FROM operations_history WHERE entity='layout:synthetic-batch:pc-a:4'").fetchone()[0]==admitted_link_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/link' AND target=''").fetchone()[0]==admitted_link_audit
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]==admitted_link_named
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('d611d611-d611-4d61-8d61-d611d611d611',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_link=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'publishing_manage' not in stored_admitted_link and 'publish' in stored_admitted_link
                check_db.execute("UPDATE publishing_layout SET account_id=?,reason=? WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=4",(admitted_link_cell[0],admitted_link_cell[2]))
                check_db.execute("UPDATE publishing_layout_links SET previous_reason=?,actor_id=?,updated_at=? WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=4",(admitted_link_manual[0],admitted_link_manual[1],admitted_link_manual[2]))
                restored_link=check_db.execute("SELECT account_id,label,reason FROM publishing_layout WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=4").fetchone()
                restored_manual=check_db.execute("SELECT previous_reason,actor_id,updated_at FROM publishing_layout_links WHERE batch='synthetic-batch' AND computer_id='pc-a' AND slot=4").fetchone()
                assert (restored_link[0],restored_link[1],restored_link[2])==(admitted_link_cell[0],admitted_link_cell[1],admitted_link_cell[2])
                assert (restored_manual[0],restored_manual[1],restored_manual[2])==(admitted_link_manual[0],admitted_link_manual[1],admitted_link_manual[2])
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='d623d623-d623-4d23-8d23-d623d623d623'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            start_kept=[item for item in auth.PERMISSIONS if item!='account_status']
            with runtime.connect() as raw_db:
                start_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成多办公室新增'").fetchone()[0]
                start_account=raw_db.execute('SELECT status,nurture_started,registered_on FROM accounts WHERE id=?',(start_id,)).fetchone()
                start_record=raw_db.execute('SELECT workflow,started_on,active_since,completed_on,verification_state FROM nurture_records WHERE account_id=?',(start_id,)).fetchone()
                start_pause=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
            assert start_id not in (patch_id,joined['id'],race_joined['id']) and start_account[0]=='正常' and start_record is None
            assert not start_account[2] or start_account[2]<=day
            assert start_pause[0]=='运行' and start_pause[1]
            with runtime.connect() as raw_db:
                raw_db.execute("INSERT INTO nurture_records(account_id,verification_state) VALUES(?,'无需核对，可使用')",(start_id,))
            start_board=get(employee_client,'/api/board')
            start_account_key='account:'+start_id
            start_nurture_key='nurture:'+start_id
            with runtime.connect() as raw_db:
                start_ready=raw_db.execute('SELECT workflow,started_on,active_since,verification_state FROM nurture_records WHERE account_id=?',(start_id,)).fetchone()
                start_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(start_nurture_key,)).fetchone()[0]
                start_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(start_account_key,)).fetchone()[0]
                start_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/start' AND target=''").fetchone()[0]
            assert (start_ready[0],start_ready[1],start_ready[2])==('待开始','','') and start_ready[3]=='无需核对，可使用'
            start_body=dict(id=start_id,started=day,request_id='d711d711-d711-4d71-8d71-d711d711d711',_expected_versions={start_account_key:start_board['record_versions'][start_account_key],start_nurture_key:start_board['record_versions'][start_nurture_key]})
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/workspace/nurture/start',start_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture start permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=start_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                start_waited=future.result(timeout=10)
                assert start_waited[0]==401,start_waited[0]
            start_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==start_id for row in get(start_reduced,'/api/board')['accounts'])
            refused_start=post(start_reduced,'/api/workspace/nurture/start',dict(id=start_id,started=day,request_id='d712d712-d712-4d72-8d72-d712d712d712',_expected_versions={start_account_key:start_board['record_versions'][start_account_key],start_nurture_key:start_board['record_versions'][start_nurture_key]}))
            assert refused_start[0]==403,refused_start[0]
            with runtime.connect() as check_db:
                start_after=check_db.execute('SELECT workflow,started_on,active_since,verification_state FROM nurture_records WHERE account_id=?',(start_id,)).fetchone()
                start_after_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(start_id,)).fetchone()
                start_after_pause=check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert (start_after[0],start_after[1],start_after[2],start_after[3])==(start_ready[0],start_ready[1],start_ready[2],start_ready[3])
                assert start_after[0]=='待开始' and start_after[2]==''
                assert (start_after_account[0],start_after_account[1])==(start_account[0],start_account[1])
                assert (start_after_pause[0],start_after_pause[1])==(start_pause[0],start_pause[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(start_nurture_key,)).fetchone()[0]==start_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(start_account_key,)).fetchone()[0]==start_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/start' AND target=''").fetchone()[0]==start_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('d711d711-d711-4d71-8d71-d711d711d711',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_start=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_status' not in stored_start and 'nurture_manage' in stored_start
                check_db.execute('DELETE FROM nurture_records WHERE account_id=?',(start_id,))
                assert check_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(start_id,)).fetchone() is None
            assert get(start_reduced,'/api/board')['record_versions'][start_account_key]==start_body['_expected_versions'][start_account_key]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='d723d723-d723-4d23-8d23-d723d723d723'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_start_kept=[item for item in auth.PERMISSIONS if item!='account_status']
            with runtime.connect() as raw_db:
                admitted_start_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成多办公室新增'").fetchone()[0]
                admitted_start_account=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_start_id,)).fetchone()
                admitted_start_pause=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert raw_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(admitted_start_id,)).fetchone() is None
            assert admitted_start_id not in (patch_id,joined['id'],race_joined['id']) and admitted_start_account[0]=='正常'
            assert admitted_start_pause[0]=='运行' and admitted_start_pause[1]
            with runtime.connect() as raw_db:
                raw_db.execute("INSERT INTO nurture_records(account_id,verification_state) VALUES(?,'无需核对，可使用')",(admitted_start_id,))
                admitted_start_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+admitted_start_id,)).fetchone()[0]
                admitted_start_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+admitted_start_id,)).fetchone()[0]
                admitted_start_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/start' AND target=''").fetchone()[0]
            def revoke_nurture_start():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_start_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_nurture_start)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture start revoke did not wait for the admitted start')
                admitted_start_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_start_body=dict(id=admitted_start_id,started=day)
                auth.authorize_route(holder,admitted_start_actor,'/api/workspace/nurture/start',admitted_start_body)
                import nurture
                assert nurture.post(holder,admitted_start_actor,'/api/workspace/nurture/start',admitted_start_body,day).get('ok') is True
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted nurture start')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_start_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==admitted_start_id for row in get(admitted_start_client,'/api/board')['accounts'])
            admitted_start_board=get(admitted_start_client,'/api/board')
            refused_admitted_start=post(admitted_start_client,'/api/workspace/nurture/start',dict(id=admitted_start_id,started=day,request_id='d811d811-d811-4d81-8d81-d811d811d811',_expected_versions={'account:'+admitted_start_id:admitted_start_board['record_versions']['account:'+admitted_start_id],'nurture:'+admitted_start_id:admitted_start_board['record_versions']['nurture:'+admitted_start_id]}))
            assert refused_admitted_start[0]==403,refused_admitted_start[0]
            with runtime.connect() as check_db:
                kept_start=check_db.execute('SELECT workflow,started_on,active_since FROM nurture_records WHERE account_id=?',(admitted_start_id,)).fetchone()
                kept_start_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_start_id,)).fetchone()
                kept_start_pause=check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert (kept_start[0],kept_start[1],kept_start[2])==('运行',day,day)
                assert (kept_start_account[0],kept_start_account[1])==('养号7天',day)
                assert (kept_start_pause[0],kept_start_pause[1])==(admitted_start_pause[0],admitted_start_pause[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+admitted_start_id,)).fetchone()[0]==admitted_start_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+admitted_start_id,)).fetchone()[0]==admitted_start_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/start' AND target=''").fetchone()[0]==admitted_start_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('d811d811-d811-4d81-8d81-d811d811d811',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_start=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_status' not in stored_admitted_start and 'nurture_manage' in stored_admitted_start
                check_db.execute('UPDATE accounts SET status=?,nurture_started=? WHERE id=?',(admitted_start_account[0],admitted_start_account[1],admitted_start_id))
                check_db.execute('DELETE FROM nurture_records WHERE account_id=?',(admitted_start_id,))
                restored_start_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_start_id,)).fetchone()
                assert (restored_start_account[0],restored_start_account[1])==(admitted_start_account[0],admitted_start_account[1])
                assert check_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(admitted_start_id,)).fetchone() is None
                assert check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()[0]=='运行'
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='d823d823-d823-4d23-8d23-d823d823d823'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            verify_kept=[item for item in auth.PERMISSIONS if item!='nurture_manage']
            with runtime.connect() as raw_db:
                verify_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成多办公室新增'").fetchone()[0]
                verify_account=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(verify_id,)).fetchone()
                verify_pause=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert raw_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(verify_id,)).fetchone() is None
                verify_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+verify_id,)).fetchone()[0]
                verify_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+verify_id,)).fetchone()[0]
                verify_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/verification' AND target=''").fetchone()[0]
            assert verify_id not in (patch_id,joined['id'],race_joined['id']) and verify_account[0]=='正常'
            assert verify_pause[0]=='运行' and verify_pause[1]
            verify_board=get(employee_client,'/api/board')
            verify_body=dict(id=verify_id,verification_state='核对处理中',request_id='d911d911-d911-4d91-8d91-d911d911d911',_expected_versions={'account:'+verify_id:verify_board['record_versions']['account:'+verify_id]})
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/workspace/nurture/verification',verify_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture verification permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=verify_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                verify_waited=future.result(timeout=10)
                assert verify_waited[0]==401,verify_waited[0]
            verify_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==verify_id for row in get(verify_reduced,'/api/board')['accounts'])
            refused_verify=post(verify_reduced,'/api/workspace/nurture/verification',dict(id=verify_id,verification_state='核对处理中',request_id='d912d912-d912-4d92-8d92-d912d912d912'))
            assert refused_verify[0]==403,refused_verify[0]
            with runtime.connect() as check_db:
                assert check_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(verify_id,)).fetchone() is None
                kept_verify_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(verify_id,)).fetchone()
                kept_verify_pause=check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert (kept_verify_account[0],kept_verify_account[1])==(verify_account[0],verify_account[1])
                assert (kept_verify_pause[0],kept_verify_pause[1])==(verify_pause[0],verify_pause[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+verify_id,)).fetchone()[0]==verify_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+verify_id,)).fetchone()[0]==verify_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/verification' AND target=''").fetchone()[0]==verify_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('d911d911-d911-4d91-8d91-d911d911d911',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_verify=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'nurture_manage' not in stored_verify and 'account_status' in stored_verify
            assert get(verify_reduced,'/api/board')['record_versions']['account:'+verify_id]==verify_body['_expected_versions']['account:'+verify_id]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='d923d923-d923-4d23-8d23-d923d923d923'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_verify_kept=[item for item in auth.PERMISSIONS if item!='nurture_manage']
            with runtime.connect() as raw_db:
                admitted_verify_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成多办公室新增'").fetchone()[0]
                admitted_verify_account=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_verify_id,)).fetchone()
                admitted_verify_pause=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert raw_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(admitted_verify_id,)).fetchone() is None
                admitted_verify_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+admitted_verify_id,)).fetchone()[0]
                admitted_verify_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+admitted_verify_id,)).fetchone()[0]
                admitted_verify_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/verification' AND target=''").fetchone()[0]
            assert admitted_verify_id not in (patch_id,joined['id'],race_joined['id']) and admitted_verify_account[0]=='正常'
            assert admitted_verify_pause[0]=='运行' and admitted_verify_pause[1]
            def revoke_nurture_verification():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_verify_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_nurture_verification)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture verification revoke did not wait for the admitted verification')
                admitted_verify_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_verify_body=dict(id=admitted_verify_id,verification_state='核对处理中')
                auth.authorize_route(holder,admitted_verify_actor,'/api/workspace/nurture/verification',admitted_verify_body)
                import nurture
                assert nurture.post(holder,admitted_verify_actor,'/api/workspace/nurture/verification',admitted_verify_body,day).get('ok') is True
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted nurture verification')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_verify_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==admitted_verify_id for row in get(admitted_verify_client,'/api/board')['accounts'])
            admitted_verify_board=get(admitted_verify_client,'/api/board')
            refused_admitted_verify=post(admitted_verify_client,'/api/workspace/nurture/verification',dict(id=admitted_verify_id,verification_state='核对已通过',request_id='e011e011-e011-4e01-8e01-e011e011e011',_expected_versions={'account:'+admitted_verify_id:admitted_verify_board['record_versions']['account:'+admitted_verify_id],'nurture:'+admitted_verify_id:admitted_verify_board['record_versions']['nurture:'+admitted_verify_id]}))
            assert refused_admitted_verify[0]==403,refused_admitted_verify[0]
            with runtime.connect() as check_db:
                kept_verify=check_db.execute('SELECT workflow,active_since,verification_state FROM nurture_records WHERE account_id=?',(admitted_verify_id,)).fetchone()
                kept_verify_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_verify_id,)).fetchone()
                kept_verify_pause=check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert (kept_verify[0],kept_verify[1],kept_verify[2])==('待开始','','核对处理中')
                assert (kept_verify_account[0],kept_verify_account[1])==(admitted_verify_account[0],admitted_verify_account[1])
                assert (kept_verify_pause[0],kept_verify_pause[1])==(admitted_verify_pause[0],admitted_verify_pause[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+admitted_verify_id,)).fetchone()[0]==admitted_verify_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+admitted_verify_id,)).fetchone()[0]==admitted_verify_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/verification' AND target=''").fetchone()[0]==admitted_verify_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('e011e011-e011-4e01-8e01-e011e011e011',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_verify=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'nurture_manage' not in stored_admitted_verify and 'account_status' in stored_admitted_verify
                check_db.execute('DELETE FROM nurture_records WHERE account_id=?',(admitted_verify_id,))
                assert check_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(admitted_verify_id,)).fetchone() is None
                assert check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()[0]=='运行'
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='e023e023-e023-4e02-8e02-e023e023e023'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            resume_kept=[item for item in auth.PERMISSIONS if item!='account_status']
            with runtime.connect() as raw_db:
                resume_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成多办公室新增'").fetchone()[0]
                resume_account=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(resume_id,)).fetchone()
                resume_pause=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert raw_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(resume_id,)).fetchone() is None
            assert resume_id not in (patch_id,joined['id'],race_joined['id']) and resume_account[0]=='正常'
            assert resume_pause[0]=='运行' and resume_pause[1]
            with runtime.connect() as raw_db:
                raw_db.execute("INSERT INTO nurture_records(account_id,verification_state,workflow,started_on,active_since,paused_on) VALUES(?,'无需核对，可使用','暂停',?,'',?)",(resume_id,day,day))
                resume_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+resume_id,)).fetchone()[0]
                resume_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+resume_id,)).fetchone()[0]
                resume_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/resume' AND target=''").fetchone()[0]
            resume_board=get(employee_client,'/api/board')
            resume_body=dict(id=resume_id,request_id='e111e111-e111-4e11-8e11-e111e111e111',_expected_versions={'account:'+resume_id:resume_board['record_versions']['account:'+resume_id],'nurture:'+resume_id:resume_board['record_versions']['nurture:'+resume_id]})
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/workspace/nurture/resume',resume_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture resume permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=resume_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                resume_waited=future.result(timeout=10)
                assert resume_waited[0]==401,resume_waited[0]
            resume_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==resume_id for row in get(resume_reduced,'/api/board')['accounts'])
            refused_resume=post(resume_reduced,'/api/workspace/nurture/resume',dict(id=resume_id,request_id='e112e112-e112-4e12-8e12-e112e112e112'))
            assert refused_resume[0]==403,refused_resume[0]
            with runtime.connect() as check_db:
                kept_resume=check_db.execute('SELECT workflow,active_since,verification_state,started_on FROM nurture_records WHERE account_id=?',(resume_id,)).fetchone()
                kept_resume_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(resume_id,)).fetchone()
                kept_resume_pause=check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert (kept_resume[0],kept_resume[1],kept_resume[2],kept_resume[3])==('暂停','','无需核对，可使用',day)
                assert (kept_resume_account[0],kept_resume_account[1])==(resume_account[0],resume_account[1])
                assert (kept_resume_pause[0],kept_resume_pause[1])==(resume_pause[0],resume_pause[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+resume_id,)).fetchone()[0]==resume_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+resume_id,)).fetchone()[0]==resume_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/resume' AND target=''").fetchone()[0]==resume_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('e111e111-e111-4e11-8e11-e111e111e111',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_resume=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_status' not in stored_resume and 'nurture_manage' in stored_resume
                check_db.execute('DELETE FROM nurture_records WHERE account_id=?',(resume_id,))
                assert check_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(resume_id,)).fetchone() is None
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',(resume_id,)).fetchone()[0]=='正常'
                assert check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()[0]=='运行'
            assert get(resume_reduced,'/api/board')['record_versions']['account:'+resume_id]==resume_body['_expected_versions']['account:'+resume_id]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='e123e123-e123-4e12-8e12-e123e123e123'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_resume_kept=[item for item in auth.PERMISSIONS if item!='account_status']
            with runtime.connect() as raw_db:
                admitted_resume_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成多办公室新增'").fetchone()[0]
                admitted_resume_account=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_resume_id,)).fetchone()
                admitted_resume_pause=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert raw_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(admitted_resume_id,)).fetchone() is None
                admitted_resume_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+admitted_resume_id,)).fetchone()[0]
                admitted_resume_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+admitted_resume_id,)).fetchone()[0]
                admitted_resume_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/resume' AND target=''").fetchone()[0]
            assert admitted_resume_id not in (patch_id,joined['id'],race_joined['id']) and admitted_resume_account[0]=='正常'
            assert admitted_resume_pause[0]=='运行' and admitted_resume_pause[1]
            with runtime.connect() as raw_db:
                raw_db.execute("INSERT INTO nurture_records(account_id,verification_state,workflow,started_on,active_since,paused_on) VALUES(?,'无需核对，可使用','暂停',?,'',?)",(admitted_resume_id,day,day))
            def revoke_nurture_resume():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_resume_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_nurture_resume)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture resume revoke did not wait for the admitted resume')
                admitted_resume_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_resume_body=dict(id=admitted_resume_id)
                auth.authorize_route(holder,admitted_resume_actor,'/api/workspace/nurture/resume',admitted_resume_body)
                import nurture
                assert nurture.post(holder,admitted_resume_actor,'/api/workspace/nurture/resume',admitted_resume_body,day).get('ok') is True
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted nurture resume')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_resume_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==admitted_resume_id for row in get(admitted_resume_client,'/api/board')['accounts'])
            admitted_resume_board=get(admitted_resume_client,'/api/board')
            refused_admitted_resume=post(admitted_resume_client,'/api/workspace/nurture/resume',dict(id=admitted_resume_id,request_id='e211e211-e211-4e21-8e21-e211e211e211',_expected_versions={'account:'+admitted_resume_id:admitted_resume_board['record_versions']['account:'+admitted_resume_id],'nurture:'+admitted_resume_id:admitted_resume_board['record_versions']['nurture:'+admitted_resume_id]}))
            assert refused_admitted_resume[0]==403,refused_admitted_resume[0]
            with runtime.connect() as check_db:
                kept_admitted_resume=check_db.execute('SELECT workflow,active_since,started_on FROM nurture_records WHERE account_id=?',(admitted_resume_id,)).fetchone()
                kept_admitted_resume_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_resume_id,)).fetchone()
                kept_admitted_resume_pause=check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert (kept_admitted_resume[0],kept_admitted_resume[1],kept_admitted_resume[2])==('运行',day,day)
                assert (kept_admitted_resume_account[0],kept_admitted_resume_account[1])==('养号7天',admitted_resume_account[1])
                assert (kept_admitted_resume_pause[0],kept_admitted_resume_pause[1])==(admitted_resume_pause[0],admitted_resume_pause[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+admitted_resume_id,)).fetchone()[0]==admitted_resume_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+admitted_resume_id,)).fetchone()[0]==admitted_resume_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/resume' AND target=''").fetchone()[0]==admitted_resume_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('e211e211-e211-4e21-8e21-e211e211e211',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_resume=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_status' not in stored_admitted_resume and 'nurture_manage' in stored_admitted_resume
                check_db.execute('UPDATE accounts SET status=?,nurture_started=? WHERE id=?',(admitted_resume_account[0],admitted_resume_account[1],admitted_resume_id))
                check_db.execute('DELETE FROM nurture_records WHERE account_id=?',(admitted_resume_id,))
                restored_resume_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_resume_id,)).fetchone()
                assert (restored_resume_account[0],restored_resume_account[1])==(admitted_resume_account[0],admitted_resume_account[1])
                assert check_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(admitted_resume_id,)).fetchone() is None
                assert check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()[0]=='运行'
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='e223e223-e223-4e22-8e22-e223e223e223'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            finish_kept=[item for item in auth.PERMISSIONS if item!='account_status']
            with runtime.connect() as raw_db:
                finish_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成多办公室新增'").fetchone()[0]
                finish_account=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(finish_id,)).fetchone()
                finish_pause=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert raw_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(finish_id,)).fetchone() is None
            assert finish_id not in (patch_id,joined['id'],race_joined['id']) and finish_account[0]=='正常'
            assert finish_pause[0]=='运行' and finish_pause[1]
            with runtime.connect() as raw_db:
                raw_db.execute("INSERT INTO nurture_records(account_id,verification_state,workflow,started_on,active_since,banked_days) VALUES(?,'无需核对，可使用','运行',?,?,7)",(finish_id,day,day))
                finish_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+finish_id,)).fetchone()[0]
                finish_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+finish_id,)).fetchone()[0]
                finish_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/finish' AND target=''").fetchone()[0]
            finish_board=get(employee_client,'/api/board')
            finish_body=dict(id=finish_id,request_id='a111a111-a111-4a11-8a11-a111a111a111',_expected_versions={'account:'+finish_id:finish_board['record_versions']['account:'+finish_id],'nurture:'+finish_id:finish_board['record_versions']['nurture:'+finish_id]})
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/workspace/nurture/finish',finish_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture finish permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=finish_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                finish_waited=future.result(timeout=10)
                assert finish_waited[0]==401,finish_waited[0]
            finish_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==finish_id for row in get(finish_reduced,'/api/board')['accounts'])
            refused_finish=post(finish_reduced,'/api/workspace/nurture/finish',dict(id=finish_id,request_id='a112a112-a112-4a12-8a12-a112a112a112'))
            assert refused_finish[0]==403,refused_finish[0]
            with runtime.connect() as check_db:
                kept_finish=check_db.execute('SELECT workflow,active_since,completed_on,banked_days FROM nurture_records WHERE account_id=?',(finish_id,)).fetchone()
                kept_finish_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(finish_id,)).fetchone()
                kept_finish_pause=check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert (kept_finish[0],kept_finish[1],kept_finish[2],kept_finish[3])==('运行',day,'',7)
                assert (kept_finish_account[0],kept_finish_account[1])==(finish_account[0],finish_account[1])
                assert (kept_finish_pause[0],kept_finish_pause[1])==(finish_pause[0],finish_pause[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+finish_id,)).fetchone()[0]==finish_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+finish_id,)).fetchone()[0]==finish_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/finish' AND target=''").fetchone()[0]==finish_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('a111a111-a111-4a11-8a11-a111a111a111',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_finish=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_status' not in stored_finish and 'nurture_manage' in stored_finish
                check_db.execute('DELETE FROM nurture_records WHERE account_id=?',(finish_id,))
                assert check_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(finish_id,)).fetchone() is None
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',(finish_id,)).fetchone()[0]=='正常'
                assert check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()[0]=='运行'
            assert get(finish_reduced,'/api/board')['record_versions']['account:'+finish_id]==finish_body['_expected_versions']['account:'+finish_id]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='a123a123-a123-4a12-8a12-a123a123a123'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_finish_kept=[item for item in auth.PERMISSIONS if item!='account_status']
            with runtime.connect() as raw_db:
                admitted_finish_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成多办公室新增'").fetchone()[0]
                admitted_finish_account=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_finish_id,)).fetchone()
                admitted_finish_pause=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert raw_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(admitted_finish_id,)).fetchone() is None
                admitted_finish_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+admitted_finish_id,)).fetchone()[0]
                admitted_finish_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+admitted_finish_id,)).fetchone()[0]
                admitted_finish_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/finish' AND target=''").fetchone()[0]
            assert admitted_finish_id not in (patch_id,joined['id'],race_joined['id']) and admitted_finish_account[0]=='正常'
            assert admitted_finish_pause[0]=='运行' and admitted_finish_pause[1]
            with runtime.connect() as raw_db:
                raw_db.execute("INSERT INTO nurture_records(account_id,verification_state,workflow,started_on,active_since,banked_days) VALUES(?,'无需核对，可使用','运行',?,?,7)",(admitted_finish_id,day,day))
            def revoke_nurture_finish():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_finish_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_nurture_finish)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture finish revoke did not wait for the admitted finish')
                admitted_finish_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_finish_body=dict(id=admitted_finish_id)
                auth.authorize_route(holder,admitted_finish_actor,'/api/workspace/nurture/finish',admitted_finish_body)
                import nurture
                assert nurture.post(holder,admitted_finish_actor,'/api/workspace/nurture/finish',admitted_finish_body,day).get('ok') is True
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted nurture finish')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_finish_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==admitted_finish_id for row in get(admitted_finish_client,'/api/board')['accounts'])
            admitted_finish_board=get(admitted_finish_client,'/api/board')
            refused_admitted_finish=post(admitted_finish_client,'/api/workspace/nurture/finish',dict(id=admitted_finish_id,request_id='a211a211-a211-4a21-8a21-a211a211a211',_expected_versions={'account:'+admitted_finish_id:admitted_finish_board['record_versions']['account:'+admitted_finish_id],'nurture:'+admitted_finish_id:admitted_finish_board['record_versions']['nurture:'+admitted_finish_id]}))
            assert refused_admitted_finish[0]==403,refused_admitted_finish[0]
            with runtime.connect() as check_db:
                kept_admitted_finish=check_db.execute('SELECT workflow,active_since,completed_on,banked_days FROM nurture_records WHERE account_id=?',(admitted_finish_id,)).fetchone()
                kept_admitted_finish_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_finish_id,)).fetchone()
                kept_admitted_finish_pause=check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert (kept_admitted_finish[0],kept_admitted_finish[1],kept_admitted_finish[2],kept_admitted_finish[3])==('已结束','',day,7)
                assert (kept_admitted_finish_account[0],kept_admitted_finish_account[1])==(admitted_finish_account[0],admitted_finish_account[1])
                assert (kept_admitted_finish_pause[0],kept_admitted_finish_pause[1])==(admitted_finish_pause[0],admitted_finish_pause[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+admitted_finish_id,)).fetchone()[0]==admitted_finish_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+admitted_finish_id,)).fetchone()[0]==admitted_finish_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/finish' AND target=''").fetchone()[0]==admitted_finish_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('a211a211-a211-4a21-8a21-a211a211a211',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_finish=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'account_status' not in stored_admitted_finish and 'nurture_manage' in stored_admitted_finish
                check_db.execute('DELETE FROM nurture_records WHERE account_id=?',(admitted_finish_id,))
                assert check_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(admitted_finish_id,)).fetchone() is None
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',(admitted_finish_id,)).fetchone()[0]=='正常'
                assert check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()[0]=='运行'
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='a223a223-a223-4a22-8a22-a223a223a223'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            upload_kept=[item for item in auth.PERMISSIONS if item!='nurture_manage']
            with runtime.connect() as raw_db:
                upload_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成多办公室新增'").fetchone()[0]
                upload_account=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(upload_id,)).fetchone()
                upload_pause=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert raw_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(upload_id,)).fetchone() is None
            assert upload_id not in (patch_id,joined['id'],race_joined['id']) and upload_account[0]=='正常'
            assert upload_pause[0]=='运行' and upload_pause[1]
            with runtime.connect() as raw_db:
                raw_db.execute("INSERT INTO nurture_records(account_id,verification_state,workflow,started_on,active_since) VALUES(?,'无需核对，可使用','运行',?,?)",(upload_id,day,day))
                upload_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+upload_id,)).fetchone()[0]
                upload_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+upload_id,)).fetchone()[0]
                upload_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/upload' AND target=''").fetchone()[0]
            upload_board=get(employee_client,'/api/board')
            upload_body=dict(id=upload_id,day=day,content=image,request_id='aa11aa11-aa11-4aa1-8aa1-aa11aa11aa11',_expected_versions={'account:'+upload_id:upload_board['record_versions']['account:'+upload_id],'nurture:'+upload_id:upload_board['record_versions']['nurture:'+upload_id]})
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/workspace/nurture/upload',upload_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture upload permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=upload_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                upload_waited=future.result(timeout=10)
                assert upload_waited[0]==401,upload_waited[0]
            upload_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==upload_id for row in get(upload_reduced,'/api/board')['accounts'])
            refused_upload=post(upload_reduced,'/api/workspace/nurture/upload',dict(id=upload_id,day=day,content=image,request_id='aa12aa12-aa12-4aa1-8aa1-aa12aa12aa12'))
            assert refused_upload[0]==403,refused_upload[0]
            with runtime.connect() as check_db:
                kept_upload=check_db.execute('SELECT workflow,active_since,image_count FROM nurture_records WHERE account_id=?',(upload_id,)).fetchone()
                kept_upload_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(upload_id,)).fetchone()
                kept_upload_pause=check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert (kept_upload[0],kept_upload[1],kept_upload[2])==('运行',day,0)
                assert check_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(upload_id,)).fetchone()[0]==0
                assert (kept_upload_account[0],kept_upload_account[1])==(upload_account[0],upload_account[1])
                assert (kept_upload_pause[0],kept_upload_pause[1])==(upload_pause[0],upload_pause[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+upload_id,)).fetchone()[0]==upload_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+upload_id,)).fetchone()[0]==upload_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/upload' AND target=''").fetchone()[0]==upload_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('aa11aa11-aa11-4aa1-8aa1-aa11aa11aa11',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_upload=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'nurture_manage' not in stored_upload and 'account_status' in stored_upload
                check_db.execute('DELETE FROM nurture_images WHERE account_id=?',(upload_id,))
                check_db.execute('DELETE FROM nurture_records WHERE account_id=?',(upload_id,))
                assert check_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(upload_id,)).fetchone() is None
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',(upload_id,)).fetchone()[0]=='正常'
                assert check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()[0]=='运行'
            assert get(upload_reduced,'/api/board')['record_versions']['account:'+upload_id]==upload_body['_expected_versions']['account:'+upload_id]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='aa23aa23-aa23-4aa2-8aa2-aa23aa23aa23'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_upload_kept=[item for item in auth.PERMISSIONS if item!='nurture_manage']
            with runtime.connect() as raw_db:
                admitted_upload_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成多办公室新增'").fetchone()[0]
                admitted_upload_account=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_upload_id,)).fetchone()
                admitted_upload_pause=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert raw_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(admitted_upload_id,)).fetchone() is None
                admitted_upload_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+admitted_upload_id,)).fetchone()[0]
                admitted_upload_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+admitted_upload_id,)).fetchone()[0]
                admitted_upload_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/upload' AND target=''").fetchone()[0]
            assert admitted_upload_id not in (patch_id,joined['id'],race_joined['id']) and admitted_upload_account[0]=='正常'
            assert admitted_upload_pause[0]=='运行' and admitted_upload_pause[1]
            with runtime.connect() as raw_db:
                raw_db.execute("INSERT INTO nurture_records(account_id,verification_state,workflow,started_on,active_since) VALUES(?,'无需核对，可使用','运行',?,?)",(admitted_upload_id,day,day))
            def revoke_nurture_upload():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_upload_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_nurture_upload)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture upload revoke did not wait for the admitted upload')
                admitted_upload_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_upload_body=dict(id=admitted_upload_id,day=day,content=image)
                auth.authorize_route(holder,admitted_upload_actor,'/api/workspace/nurture/upload',admitted_upload_body)
                import nurture
                assert nurture.post(holder,admitted_upload_actor,'/api/workspace/nurture/upload',admitted_upload_body,day).get('ok') is True
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted nurture upload')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_upload_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==admitted_upload_id for row in get(admitted_upload_client,'/api/board')['accounts'])
            admitted_upload_board=get(admitted_upload_client,'/api/board')
            refused_admitted_upload=post(admitted_upload_client,'/api/workspace/nurture/upload',dict(id=admitted_upload_id,day=day,content=image,request_id='ae11ae11-ae11-4ae1-8ae1-ae11ae11ae11',_expected_versions={'account:'+admitted_upload_id:admitted_upload_board['record_versions']['account:'+admitted_upload_id],'nurture:'+admitted_upload_id:admitted_upload_board['record_versions']['nurture:'+admitted_upload_id]}))
            assert refused_admitted_upload[0]==403,refused_admitted_upload[0]
            with runtime.connect() as check_db:
                kept_admitted_upload=check_db.execute('SELECT workflow,active_since,image_count FROM nurture_records WHERE account_id=?',(admitted_upload_id,)).fetchone()
                kept_admitted_upload_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_upload_id,)).fetchone()
                kept_admitted_upload_pause=check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert (kept_admitted_upload[0],kept_admitted_upload[1],kept_admitted_upload[2])==('运行',day,1)
                assert check_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(admitted_upload_id,)).fetchone()[0]==1
                assert (kept_admitted_upload_account[0],kept_admitted_upload_account[1])==(admitted_upload_account[0],admitted_upload_account[1])
                assert (kept_admitted_upload_pause[0],kept_admitted_upload_pause[1])==(admitted_upload_pause[0],admitted_upload_pause[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+admitted_upload_id,)).fetchone()[0]==admitted_upload_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+admitted_upload_id,)).fetchone()[0]==admitted_upload_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/upload' AND target=''").fetchone()[0]==admitted_upload_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('ae11ae11-ae11-4ae1-8ae1-ae11ae11ae11',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_upload=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'nurture_manage' not in stored_admitted_upload and 'account_status' in stored_admitted_upload
                check_db.execute('DELETE FROM nurture_images WHERE account_id=?',(admitted_upload_id,))
                check_db.execute('DELETE FROM nurture_records WHERE account_id=?',(admitted_upload_id,))
                assert check_db.execute('SELECT count(*) FROM nurture_images WHERE account_id=?',(admitted_upload_id,)).fetchone()[0]==0
                assert check_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(admitted_upload_id,)).fetchone() is None
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',(admitted_upload_id,)).fetchone()[0]=='正常'
                assert check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()[0]=='运行'
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='ae23ae23-ae23-4ae2-8ae2-ae23ae23ae23'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            delete_kept=[item for item in auth.PERMISSIONS if item!='nurture_manage']
            with runtime.connect() as raw_db:
                delete_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成多办公室新增'").fetchone()[0]
                delete_account=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(delete_id,)).fetchone()
                delete_pause=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert raw_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(delete_id,)).fetchone() is None
            assert delete_id not in (patch_id,joined['id'],race_joined['id']) and delete_account[0]=='正常'
            assert delete_pause[0]=='运行' and delete_pause[1]
            with runtime.connect() as raw_db:
                raw_db.execute("INSERT INTO nurture_records(account_id,verification_state,workflow,started_on,active_since,image_count) VALUES(?,'无需核对，可使用','运行',?,?,1)",(delete_id,day,day))
                raw_db.execute("INSERT INTO nurture_images VALUES(?,?,?,?,?,?,?)",('synthetic-lock-delete-image',delete_id,1,day,'image/png',png(),'合成人员'))
                delete_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+delete_id,)).fetchone()[0]
                delete_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+delete_id,)).fetchone()[0]
                delete_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/delete-image' AND target=''").fetchone()[0]
            delete_board=get(employee_client,'/api/board')
            delete_body=dict(id=delete_id,image_id='synthetic-lock-delete-image',request_id='af11af11-af11-4af1-8af1-af11af11af11',_expected_versions={'account:'+delete_id:delete_board['record_versions']['account:'+delete_id],'nurture:'+delete_id:delete_board['record_versions']['nurture:'+delete_id]})
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/workspace/nurture/delete-image',delete_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture delete-image permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=delete_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                delete_waited=future.result(timeout=10)
                assert delete_waited[0]==401,delete_waited[0]
            delete_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==delete_id for row in get(delete_reduced,'/api/board')['accounts'])
            refused_delete=post(delete_reduced,'/api/workspace/nurture/delete-image',dict(id=delete_id,image_id='synthetic-lock-delete-image',request_id='af12af12-af12-4af1-8af1-af12af12af12'))
            assert refused_delete[0]==403,refused_delete[0]
            with runtime.connect() as check_db:
                kept_delete=check_db.execute('SELECT workflow,active_since,image_count FROM nurture_records WHERE account_id=?',(delete_id,)).fetchone()
                kept_delete_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(delete_id,)).fetchone()
                kept_delete_pause=check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert (kept_delete[0],kept_delete[1],kept_delete[2])==('运行',day,1)
                assert check_db.execute("SELECT count(*) FROM nurture_images WHERE id='synthetic-lock-delete-image' AND account_id=?",(delete_id,)).fetchone()[0]==1
                assert (kept_delete_account[0],kept_delete_account[1])==(delete_account[0],delete_account[1])
                assert (kept_delete_pause[0],kept_delete_pause[1])==(delete_pause[0],delete_pause[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+delete_id,)).fetchone()[0]==delete_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+delete_id,)).fetchone()[0]==delete_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/delete-image' AND target=''").fetchone()[0]==delete_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('af11af11-af11-4af1-8af1-af11af11af11',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_delete=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'nurture_manage' not in stored_delete and 'account_status' in stored_delete
                check_db.execute("DELETE FROM nurture_images WHERE id='synthetic-lock-delete-image'")
                check_db.execute('DELETE FROM nurture_records WHERE account_id=?',(delete_id,))
                assert check_db.execute("SELECT count(*) FROM nurture_images WHERE id='synthetic-lock-delete-image'").fetchone()[0]==0
                assert check_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(delete_id,)).fetchone() is None
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',(delete_id,)).fetchone()[0]=='正常'
                assert check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()[0]=='运行'
            assert get(delete_reduced,'/api/board')['record_versions']['account:'+delete_id]==delete_body['_expected_versions']['account:'+delete_id]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='af23af23-af23-4af2-8af2-af23af23af23'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_delete_kept=[item for item in auth.PERMISSIONS if item!='nurture_manage']
            with runtime.connect() as raw_db:
                admitted_delete_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成多办公室新增'").fetchone()[0]
                admitted_delete_account=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_delete_id,)).fetchone()
                admitted_delete_pause=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert raw_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(admitted_delete_id,)).fetchone() is None
                admitted_delete_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+admitted_delete_id,)).fetchone()[0]
                admitted_delete_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+admitted_delete_id,)).fetchone()[0]
                admitted_delete_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/delete-image' AND target=''").fetchone()[0]
            assert admitted_delete_id not in (patch_id,joined['id'],race_joined['id']) and admitted_delete_account[0]=='正常'
            assert admitted_delete_pause[0]=='运行' and admitted_delete_pause[1]
            with runtime.connect() as raw_db:
                raw_db.execute("INSERT INTO nurture_records(account_id,verification_state,workflow,started_on,active_since,image_count) VALUES(?,'无需核对，可使用','运行',?,?,1)",(admitted_delete_id,day,day))
                raw_db.execute("INSERT INTO nurture_images VALUES(?,?,?,?,?,?,?)",('synthetic-lock-admitted-image',admitted_delete_id,1,day,'image/png',png(),'合成人员'))
            def revoke_nurture_delete():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_delete_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_nurture_delete)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture delete-image revoke did not wait for the admitted delete')
                admitted_delete_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_delete_body=dict(id=admitted_delete_id,image_id='synthetic-lock-admitted-image')
                auth.authorize_route(holder,admitted_delete_actor,'/api/workspace/nurture/delete-image',admitted_delete_body)
                import nurture
                assert nurture.post(holder,admitted_delete_actor,'/api/workspace/nurture/delete-image',admitted_delete_body,day).get('ok') is True
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted nurture delete-image')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_delete_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==admitted_delete_id for row in get(admitted_delete_client,'/api/board')['accounts'])
            admitted_delete_board=get(admitted_delete_client,'/api/board')
            refused_admitted_delete=post(admitted_delete_client,'/api/workspace/nurture/delete-image',dict(id=admitted_delete_id,image_id='synthetic-lock-admitted-image',request_id='cf11cf11-cf11-4cf1-8cf1-cf11cf11cf11',_expected_versions={'account:'+admitted_delete_id:admitted_delete_board['record_versions']['account:'+admitted_delete_id],'nurture:'+admitted_delete_id:admitted_delete_board['record_versions']['nurture:'+admitted_delete_id]}))
            assert refused_admitted_delete[0]==403,refused_admitted_delete[0]
            with runtime.connect() as check_db:
                kept_admitted_delete=check_db.execute('SELECT workflow,active_since,image_count FROM nurture_records WHERE account_id=?',(admitted_delete_id,)).fetchone()
                kept_admitted_delete_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_delete_id,)).fetchone()
                kept_admitted_delete_pause=check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert (kept_admitted_delete[0],kept_admitted_delete[1],kept_admitted_delete[2])==('运行',day,0)
                assert check_db.execute("SELECT count(*) FROM nurture_images WHERE id='synthetic-lock-admitted-image'").fetchone()[0]==0
                assert (kept_admitted_delete_account[0],kept_admitted_delete_account[1])==(admitted_delete_account[0],admitted_delete_account[1])
                assert (kept_admitted_delete_pause[0],kept_admitted_delete_pause[1])==(admitted_delete_pause[0],admitted_delete_pause[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+admitted_delete_id,)).fetchone()[0]==admitted_delete_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+admitted_delete_id,)).fetchone()[0]==admitted_delete_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/delete-image' AND target=''").fetchone()[0]==admitted_delete_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('cf11cf11-cf11-4cf1-8cf1-cf11cf11cf11',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_delete=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'nurture_manage' not in stored_admitted_delete and 'account_status' in stored_admitted_delete
                check_db.execute('DELETE FROM nurture_images WHERE account_id=?',(admitted_delete_id,))
                check_db.execute('DELETE FROM nurture_records WHERE account_id=?',(admitted_delete_id,))
                assert check_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(admitted_delete_id,)).fetchone() is None
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',(admitted_delete_id,)).fetchone()[0]=='正常'
                assert check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()[0]=='运行'
            restored_delete_permissions=post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='cf23cf23-cf23-4cf2-8cf2-cf23cf23cf23'))
            assert restored_delete_permissions[0]==200,restored_delete_permissions[0]
            employee_client=test_auth_support.client(base,'synthetic_employee')
            purge_kept=[item for item in auth.PERMISSIONS if item!='nurture_manage']
            with runtime.connect() as raw_db:
                purge_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成多办公室新增'").fetchone()[0]
                purge_account=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(purge_id,)).fetchone()
                purge_pause=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert raw_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(purge_id,)).fetchone() is None
            assert purge_id not in (patch_id,joined['id'],race_joined['id']) and purge_account[0]=='正常'
            assert purge_pause[0]=='运行' and purge_pause[1]
            with runtime.connect() as raw_db:
                raw_db.execute("INSERT INTO nurture_records(account_id,verification_state,workflow,started_on,completed_on,active_since,image_count) VALUES(?,'无需核对，可使用','已结束',?,?,'',1)",(purge_id,day,day))
                raw_db.execute("INSERT INTO nurture_images VALUES(?,?,?,?,?,?,?)",('synthetic-lock-purge-image',purge_id,1,day,'image/png',png(),'合成人员'))
                purge_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+purge_id,)).fetchone()[0]
                purge_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+purge_id,)).fetchone()[0]
                purge_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/purge' AND target=''").fetchone()[0]
            purge_board=get(employee_client,'/api/board')
            purge_body=dict(id=purge_id,request_id='df11df11-df11-4df1-8df1-df11df11df11',_expected_versions={'account:'+purge_id:purge_board['record_versions']['account:'+purge_id],'nurture:'+purge_id:purge_board['record_versions']['nurture:'+purge_id]})
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/workspace/nurture/purge',purge_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture purge permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=purge_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                purge_waited=future.result(timeout=10)
                assert purge_waited[0]==401,purge_waited[0]
            purge_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==purge_id for row in get(purge_reduced,'/api/board')['accounts'])
            refused_purge=post(purge_reduced,'/api/workspace/nurture/purge',dict(id=purge_id,request_id='df12df12-df12-4df1-8df1-df12df12df12'))
            assert refused_purge[0]==403,refused_purge[0]
            with runtime.connect() as check_db:
                kept_purge=check_db.execute('SELECT workflow,active_since,completed_on,image_count FROM nurture_records WHERE account_id=?',(purge_id,)).fetchone()
                kept_purge_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(purge_id,)).fetchone()
                kept_purge_pause=check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert (kept_purge[0],kept_purge[1],kept_purge[2],kept_purge[3])==('已结束','',day,1)
                assert check_db.execute("SELECT count(*) FROM nurture_images WHERE id='synthetic-lock-purge-image' AND account_id=?",(purge_id,)).fetchone()[0]==1
                assert (kept_purge_account[0],kept_purge_account[1])==(purge_account[0],purge_account[1])
                assert (kept_purge_pause[0],kept_purge_pause[1])==(purge_pause[0],purge_pause[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+purge_id,)).fetchone()[0]==purge_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+purge_id,)).fetchone()[0]==purge_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/purge' AND target=''").fetchone()[0]==purge_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('df11df11-df11-4df1-8df1-df11df11df11',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_purge=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'nurture_manage' not in stored_purge and 'account_status' in stored_purge
                check_db.execute("DELETE FROM nurture_images WHERE id='synthetic-lock-purge-image'")
                check_db.execute('DELETE FROM nurture_records WHERE account_id=?',(purge_id,))
                assert check_db.execute("SELECT count(*) FROM nurture_images WHERE id='synthetic-lock-purge-image'").fetchone()[0]==0
                assert check_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(purge_id,)).fetchone() is None
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',(purge_id,)).fetchone()[0]=='正常'
                assert check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()[0]=='运行'
            assert get(purge_reduced,'/api/board')['record_versions']['account:'+purge_id]==purge_body['_expected_versions']['account:'+purge_id]
            restored_purge_permissions=post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='df23df23-df23-4df2-8df2-df23df23df23'))
            assert restored_purge_permissions[0]==200,restored_purge_permissions[0]
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_purge_kept=[item for item in auth.PERMISSIONS if item!='nurture_manage']
            with runtime.connect() as raw_db:
                admitted_purge_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成多办公室新增'").fetchone()[0]
                admitted_purge_account=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_purge_id,)).fetchone()
                admitted_purge_pause=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert raw_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(admitted_purge_id,)).fetchone() is None
                admitted_purge_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+admitted_purge_id,)).fetchone()[0]
                admitted_purge_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+admitted_purge_id,)).fetchone()[0]
                admitted_purge_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/purge' AND target=''").fetchone()[0]
            assert admitted_purge_id not in (patch_id,joined['id'],race_joined['id']) and admitted_purge_account[0]=='正常'
            assert admitted_purge_pause[0]=='运行' and admitted_purge_pause[1]
            with runtime.connect() as raw_db:
                raw_db.execute("INSERT INTO nurture_records(account_id,verification_state,workflow,started_on,completed_on,active_since,image_count) VALUES(?,'无需核对，可使用','已结束',?,?,'',1)",(admitted_purge_id,day,day))
                raw_db.execute("INSERT INTO nurture_images VALUES(?,?,?,?,?,?,?)",('synthetic-lock-admitted-purge-image',admitted_purge_id,1,day,'image/png',png(),'合成人员'))
            def revoke_nurture_purge():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_purge_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_nurture_purge)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture purge revoke did not wait for the admitted purge')
                admitted_purge_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_purge_body=dict(id=admitted_purge_id)
                auth.authorize_route(holder,admitted_purge_actor,'/api/workspace/nurture/purge',admitted_purge_body)
                import nurture
                assert nurture.post(holder,admitted_purge_actor,'/api/workspace/nurture/purge',admitted_purge_body,day).get('ok') is True
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted nurture purge')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_purge_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==admitted_purge_id for row in get(admitted_purge_client,'/api/board')['accounts'])
            admitted_purge_board=get(admitted_purge_client,'/api/board')
            refused_admitted_purge=post(admitted_purge_client,'/api/workspace/nurture/purge',dict(id=admitted_purge_id,request_id='ef11ef11-ef11-4ef1-8ef1-ef11ef11ef11',_expected_versions={'account:'+admitted_purge_id:admitted_purge_board['record_versions']['account:'+admitted_purge_id],'nurture:'+admitted_purge_id:admitted_purge_board['record_versions']['nurture:'+admitted_purge_id]}))
            assert refused_admitted_purge[0]==403,refused_admitted_purge[0]
            with runtime.connect() as check_db:
                kept_admitted_purge=check_db.execute('SELECT workflow,active_since,completed_on,image_count FROM nurture_records WHERE account_id=?',(admitted_purge_id,)).fetchone()
                kept_admitted_purge_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_purge_id,)).fetchone()
                kept_admitted_purge_pause=check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert (kept_admitted_purge[0],kept_admitted_purge[1],kept_admitted_purge[2],kept_admitted_purge[3])==('已结束','',day,0)
                assert check_db.execute("SELECT count(*) FROM nurture_images WHERE id='synthetic-lock-admitted-purge-image'").fetchone()[0]==0
                assert (kept_admitted_purge_account[0],kept_admitted_purge_account[1])==(admitted_purge_account[0],admitted_purge_account[1])
                assert (kept_admitted_purge_pause[0],kept_admitted_purge_pause[1])==(admitted_purge_pause[0],admitted_purge_pause[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+admitted_purge_id,)).fetchone()[0]==admitted_purge_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+admitted_purge_id,)).fetchone()[0]==admitted_purge_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/purge' AND target=''").fetchone()[0]==admitted_purge_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('ef11ef11-ef11-4ef1-8ef1-ef11ef11ef11',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_purge=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'nurture_manage' not in stored_admitted_purge and 'account_status' in stored_admitted_purge
                check_db.execute('DELETE FROM nurture_images WHERE account_id=?',(admitted_purge_id,))
                check_db.execute('DELETE FROM nurture_records WHERE account_id=?',(admitted_purge_id,))
                assert check_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(admitted_purge_id,)).fetchone() is None
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',(admitted_purge_id,)).fetchone()[0]=='正常'
                assert check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()[0]=='运行'
            restored_admitted_purge_permissions=post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='ef23ef23-ef23-4ef2-8ef2-ef23ef23ef23'))
            assert restored_admitted_purge_permissions[0]==200,restored_admitted_purge_permissions[0]
            employee_client=test_auth_support.client(base,'synthetic_employee')
            join_kept=[item for item in auth.PERMISSIONS if item!='nurture_manage']
            with runtime.connect() as raw_db:
                join_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成多办公室新增'").fetchone()[0]
                join_account=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(join_id,)).fetchone()
                join_pause=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert raw_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(join_id,)).fetchone() is None
                join_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+join_id,)).fetchone()[0]
                join_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+join_id,)).fetchone()[0]
                join_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/join' AND target=''").fetchone()[0]
            assert join_id not in (patch_id,joined['id'],race_joined['id']) and join_account[0]=='正常'
            assert join_pause[0]=='运行' and join_pause[1]
            join_board=get(employee_client,'/api/board')
            join_body=dict(id=join_id,request_id='ff11ff11-ff11-4ff1-8ff1-ff11ff11ff11',_expected_versions={'account:'+join_id:join_board['record_versions']['account:'+join_id]})
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/workspace/nurture/join',join_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture join permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=join_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                join_waited=future.result(timeout=10)
                assert join_waited[0]==401,join_waited[0]
            join_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==join_id for row in get(join_reduced,'/api/board')['accounts'])
            refused_join=post(join_reduced,'/api/workspace/nurture/join',dict(id=join_id,request_id='ff12ff12-ff12-4ff1-8ff1-ff12ff12ff12'))
            assert refused_join[0]==403,refused_join[0]
            with runtime.connect() as check_db:
                kept_join_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(join_id,)).fetchone()
                kept_join_pause=check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert check_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(join_id,)).fetchone() is None
                assert (kept_join_account[0],kept_join_account[1])==(join_account[0],join_account[1])
                assert (kept_join_pause[0],kept_join_pause[1])==(join_pause[0],join_pause[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+join_id,)).fetchone()[0]==join_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+join_id,)).fetchone()[0]==join_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/join' AND target=''").fetchone()[0]==join_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('ff11ff11-ff11-4ff1-8ff1-ff11ff11ff11',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_join=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'nurture_manage' not in stored_join and 'account_status' in stored_join
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',(join_id,)).fetchone()[0]=='正常'
                assert check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()[0]=='运行'
            assert get(join_reduced,'/api/board')['record_versions']['account:'+join_id]==join_body['_expected_versions']['account:'+join_id]
            restored_join_permissions=post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='ff23ff23-ff23-4ff2-8ff2-ff23ff23ff23'))
            assert restored_join_permissions[0]==200,restored_join_permissions[0]
            employee_client=test_auth_support.client(base,'synthetic_employee')
            admitted_join_kept=[item for item in auth.PERMISSIONS if item!='nurture_manage']
            with runtime.connect() as raw_db:
                admitted_join_id=raw_db.execute("SELECT id FROM accounts WHERE name='合成多办公室新增'").fetchone()[0]
                admitted_join_account=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_join_id,)).fetchone()
                admitted_join_pause=raw_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert raw_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(admitted_join_id,)).fetchone() is None
                admitted_join_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+admitted_join_id,)).fetchone()[0]
                admitted_join_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+admitted_join_id,)).fetchone()[0]
                admitted_join_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/join' AND target=''").fetchone()[0]
            assert admitted_join_id not in (patch_id,joined['id'],race_joined['id']) and admitted_join_account[0]=='正常'
            assert admitted_join_pause[0]=='运行' and admitted_join_pause[1]
            def revoke_nurture_join():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=admitted_join_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_nurture_join)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Nurture join revoke did not wait for the admitted join')
                admitted_join_actor=auth.public_user(holder.execute('SELECT * FROM auth_users WHERE id=?',(employee,)).fetchone())
                admitted_join_body=dict(id=admitted_join_id)
                auth.authorize_route(holder,admitted_join_actor,'/api/workspace/nurture/join',admitted_join_body)
                import nurture
                assert nurture.post(holder,admitted_join_actor,'/api/workspace/nurture/join',admitted_join_body,day).get('ok') is True
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted nurture join')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_join_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==admitted_join_id for row in get(admitted_join_client,'/api/board')['accounts'])
            admitted_join_board=get(admitted_join_client,'/api/board')
            refused_admitted_join=post(admitted_join_client,'/api/workspace/nurture/join',dict(id=admitted_join_id,request_id='0f110f11-0f11-40f1-80f1-0f110f110f11',_expected_versions={'account:'+admitted_join_id:admitted_join_board['record_versions']['account:'+admitted_join_id],'nurture:'+admitted_join_id:admitted_join_board['record_versions']['nurture:'+admitted_join_id]}))
            assert refused_admitted_join[0]==403,refused_admitted_join[0]
            with runtime.connect() as check_db:
                kept_admitted_join=check_db.execute('SELECT workflow,active_since,completed_on FROM nurture_records WHERE account_id=?',(admitted_join_id,)).fetchone()
                kept_admitted_join_account=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(admitted_join_id,)).fetchone()
                kept_admitted_join_pause=check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()
                assert (kept_admitted_join[0],kept_admitted_join[1],kept_admitted_join[2])==('待开始','','')
                assert (kept_admitted_join_account[0],kept_admitted_join_account[1])==(admitted_join_account[0],admitted_join_account[1])
                assert (kept_admitted_join_pause[0],kept_admitted_join_pause[1])==(admitted_join_pause[0],admitted_join_pause[1])
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('nurture:'+admitted_join_id,)).fetchone()[0]==admitted_join_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:'+admitted_join_id,)).fetchone()[0]==admitted_join_account_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/workspace/nurture/join' AND target=''").fetchone()[0]==admitted_join_audit
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('0f110f11-0f11-40f1-80f1-0f110f110f11',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                stored_admitted_join=json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                assert 'nurture_manage' not in stored_admitted_join and 'account_status' in stored_admitted_join
                check_db.execute('DELETE FROM nurture_records WHERE account_id=?',(admitted_join_id,))
                assert check_db.execute('SELECT 1 FROM nurture_records WHERE account_id=?',(admitted_join_id,)).fetchone() is None
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',(admitted_join_id,)).fetchone()[0]=='正常'
                assert check_db.execute('SELECT workflow,active_since FROM nurture_records WHERE account_id=?',('account-b',)).fetchone()[0]=='运行'
            restored_admitted_join_permissions=post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='0f230f23-0f23-40f2-80f2-0f230f230f23'))
            assert restored_admitted_join_permissions[0]==200,restored_admitted_join_permissions[0]
            employee_client=test_auth_support.client(base,'synthetic_employee')
            device_board=get(employee_client,'/api/board')
            device_row=next(row for row in device_board['phone_devices']['devices'] if row['id']=='8')
            assert set(device_row['office_ids'])=={office_a}
            with runtime.connect() as raw_db:
                device_before=raw_db.execute('SELECT name,state FROM phone_devices WHERE id=?',('8',)).fetchone()
                device_offices_before=sorted(row[0] for row in raw_db.execute('SELECT office_id FROM phone_device_offices WHERE device_id=?',('8',)).fetchall())
                device_history_kept=raw_db.execute("SELECT count(*) FROM operations_history WHERE entity='device:8'").fetchone()[0]
                device_audit_kept=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device' AND target='8'").fetchone()[0]
            assert device_offices_before==[office_a] and device_before[0]!='合成锁等待设备名'
            device_wait_body=dict(id='8',name='合成锁等待设备名',state=device_before[1],office_ids=[office_a],request_id='e811e811-e811-4e81-8e81-e811e811e811',_expected_versions={'device:8':device_board['record_versions']['device:8']})
            device_kept=[item for item in auth.PERMISSIONS if item!='device_manage']
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/phone-device',device_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Device permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=device_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                device_waited=future.result(timeout=10)
                assert device_waited[0]==401,device_waited[0]
            device_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']=='8' for row in get(device_reduced,'/api/board')['phone_devices']['devices'])
            refused_device=post(device_reduced,'/api/phone-device',dict(id='8',name='合成锁等待设备名',state=device_before[1],office_ids=[office_a],request_id='e812e812-e812-4e82-8e82-e812e812e812'))
            assert refused_device[0]==403,refused_device[0]
            with runtime.connect() as check_db:
                device_after_wait=check_db.execute('SELECT name,state FROM phone_devices WHERE id=?',('8',)).fetchone()
                assert device_after_wait[0]==device_before[0] and device_after_wait[1]==device_before[1]
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM phone_device_offices WHERE device_id=?',('8',)).fetchall())==device_offices_before
                assert check_db.execute("SELECT count(*) FROM operations_history WHERE entity='device:8'").fetchone()[0]==device_history_kept
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device' AND target='8'").fetchone()[0]==device_audit_kept
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('e811e811-e811-4e81-8e81-e811e811e811',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                assert 'device_manage' not in json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
            assert get(device_reduced,'/api/board')['record_versions']['device:8']==device_wait_body['_expected_versions']['device:8']
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='e823e823-e823-4e23-8e23-e823e823e823'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            with runtime.connect() as raw_db:
                admitted_device_before=raw_db.execute('SELECT name,state FROM phone_devices WHERE id=?',('8',)).fetchone()
                admitted_device_offices=sorted(row[0] for row in raw_db.execute('SELECT office_id FROM phone_device_offices WHERE device_id=?',('8',)).fetchall())
                admitted_device_history=raw_db.execute("SELECT count(*) FROM operations_history WHERE entity='device:8'").fetchone()[0]
                admitted_device_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device' AND target='8'").fetchone()[0]
            assert admitted_device_offices==[office_a]
            admitted_device_name='合成已准入设备名' if admitted_device_before[0]!='合成已准入设备名' else '合成已准入设备乙'
            def revoke_device():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=device_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_device)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Device revoke did not wait for the admitted device write')
                holder.execute('UPDATE phone_devices SET name=? WHERE id=?',(admitted_device_name,'8'))
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted device write')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_device_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']=='8' for row in get(admitted_device_client,'/api/board')['phone_devices']['devices'])
            refused_device_after=post(admitted_device_client,'/api/phone-device',dict(id='8',name=admitted_device_before[0],state=admitted_device_before[1],office_ids=[office_a],request_id='e911e911-e911-4e91-8e91-e911e911e911'))
            assert refused_device_after[0]==403,refused_device_after[0]
            with runtime.connect() as check_db:
                kept_device=check_db.execute('SELECT name,state FROM phone_devices WHERE id=?',('8',)).fetchone()
                assert kept_device[0]==admitted_device_name and kept_device[1]==admitted_device_before[1]
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM phone_device_offices WHERE device_id=?',('8',)).fetchall())==admitted_device_offices
                assert check_db.execute("SELECT count(*) FROM operations_history WHERE entity='device:8'").fetchone()[0]==admitted_device_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device' AND target='8'").fetchone()[0]==admitted_device_audit
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                assert 'device_manage' not in json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                check_db.execute('UPDATE phone_devices SET name=? WHERE id=?',(admitted_device_before[0],'8'))
                restored_device=check_db.execute('SELECT name,state FROM phone_devices WHERE id=?',('8',)).fetchone()
                assert restored_device[0]==admitted_device_before[0] and restored_device[1]==admitted_device_before[1]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='e923e923-e923-4e23-8e23-e923e923e923'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            progress_board=get(employee_client,'/api/board')
            progress_account='account:'+patch_id
            with runtime.connect() as raw_db:
                progress_before=raw_db.execute('SELECT auxiliary_state,account_type FROM accounts WHERE id=?',(patch_id,)).fetchone()
                progress_history_kept=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(progress_account,)).fetchone()[0]
                progress_audit_kept=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/lifecycle-status' AND target=''").fetchone()[0]
            progress_attempt='已注册' if progress_before[0]!='已注册' else '待确认'
            progress_wait_body=dict(id=patch_id,auxiliary_state=progress_attempt,request_id='e611e611-e611-4e61-8e61-e611e611e611',_expected_versions={progress_account:progress_board['record_versions'][progress_account]})
            progress_kept=[item for item in auth.PERMISSIONS if item!='account_progress']
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/lifecycle-status',progress_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Progress permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=progress_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                progress_waited=future.result(timeout=10)
                assert progress_waited[0]==401,progress_waited[0]
            progress_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(progress_reduced,'/api/board')['accounts'])
            refused_progress=post(progress_reduced,'/api/lifecycle-status',dict(id=patch_id,auxiliary_state=progress_attempt,request_id='e612e612-e612-4e62-8e62-e612e612e612'))
            assert refused_progress[0]==403,refused_progress[0]
            with runtime.connect() as check_db:
                progress_kept_row=check_db.execute('SELECT auxiliary_state,account_type FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert progress_kept_row[0]==progress_before[0] and progress_kept_row[1]==progress_before[1]
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(progress_account,)).fetchone()[0]==progress_history_kept
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/lifecycle-status' AND target=''").fetchone()[0]==progress_audit_kept
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('e611e611-e611-4e61-8e61-e611e611e611',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                assert 'account_progress' not in json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
            assert get(progress_reduced,'/api/board')['record_versions'][progress_account]==progress_wait_body['_expected_versions'][progress_account]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='e623e623-e623-4e23-8e23-e623e623e623'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            with runtime.connect() as raw_db:
                admitted_progress_before=raw_db.execute('SELECT auxiliary_state,account_type FROM accounts WHERE id=?',(patch_id,)).fetchone()
                admitted_progress_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(progress_account,)).fetchone()[0]
                admitted_progress_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/lifecycle-status' AND target=''").fetchone()[0]
            admitted_progress='已注册' if admitted_progress_before[0]!='已注册' else '待确认'
            def revoke_progress():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=progress_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_progress)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Progress revoke did not wait for the admitted progress write')
                holder.execute('UPDATE accounts SET auxiliary_state=? WHERE id=?',(admitted_progress,patch_id))
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted progress write')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_progress_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(admitted_progress_client,'/api/board')['accounts'])
            refused_progress_after=post(admitted_progress_client,'/api/lifecycle-status',dict(id=patch_id,auxiliary_state=admitted_progress_before[0],request_id='e711e711-e711-4e71-8e71-e711e711e711'))
            assert refused_progress_after[0]==403,refused_progress_after[0]
            with runtime.connect() as check_db:
                kept_progress=check_db.execute('SELECT auxiliary_state,account_type FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert kept_progress[0]==admitted_progress and kept_progress[1]==admitted_progress_before[1]
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(progress_account,)).fetchone()[0]==admitted_progress_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/lifecycle-status' AND target=''").fetchone()[0]==admitted_progress_audit
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                assert 'account_progress' not in json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                check_db.execute('UPDATE accounts SET auxiliary_state=? WHERE id=?',(admitted_progress_before[0],patch_id))
                restored_progress=check_db.execute('SELECT auxiliary_state,account_type FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert restored_progress[0]==admitted_progress_before[0] and restored_progress[1]==admitted_progress_before[1]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='e723e723-e723-4e23-8e23-e723e723e723'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            status_board=get(employee_client,'/api/board')
            status_account='account:'+patch_id
            with runtime.connect() as raw_db:
                status_before=raw_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]
                status_history_kept=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(status_account,)).fetchone()[0]
                status_audit_kept=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/status' AND target=?",(patch_id,)).fetchone()[0]
            status_attempt='正常' if status_before!='正常' else '停用'
            status_wait_body=dict(id=patch_id,status=status_attempt,request_id='e411e411-e411-4e41-8e41-e411e411e411',_expected_versions={status_account:status_board['record_versions'][status_account]})
            status_kept=[item for item in auth.PERMISSIONS if item!='account_status']
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/account/status',status_wait_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Status permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=status_kept,office_ids=[office_a,office_b],active=True))
                holder.commit()
                status_waited=future.result(timeout=10)
                assert status_waited[0]==401,status_waited[0]
            status_reduced=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(status_reduced,'/api/board')['accounts'])
            refused_status=post(status_reduced,'/api/account/status',dict(id=patch_id,status=status_attempt,request_id='e412e412-e412-4e42-8e42-e412e412e412'))
            assert refused_status[0]==403,refused_status[0]
            with runtime.connect() as check_db:
                assert check_db.execute('SELECT status FROM accounts WHERE id=?',(patch_id,)).fetchone()[0]==status_before
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(status_account,)).fetchone()[0]==status_history_kept
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/status' AND target=?",(patch_id,)).fetchone()[0]==status_audit_kept
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('e411e411-e411-4e41-8e41-e411e411e411',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                assert 'account_status' not in json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
            assert get(status_reduced,'/api/board')['record_versions'][status_account]==status_wait_body['_expected_versions'][status_account]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='e423e423-e423-4e23-8e23-e423e423e423'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            with runtime.connect() as raw_db:
                admitted_status_before=raw_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()
                admitted_status_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(status_account,)).fetchone()[0]
                admitted_status_audit=raw_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/status' AND target=?",(patch_id,)).fetchone()[0]
            admitted_status='正常' if admitted_status_before[0]!='正常' else '停用'
            def revoke_account_status():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=status_kept,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_account_status)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Account-status revoke did not wait for the admitted status write')
                holder.execute('UPDATE accounts SET status=? WHERE id=?',(admitted_status,patch_id))
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted status write')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_status_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==patch_id for row in get(admitted_status_client,'/api/board')['accounts'])
            refused_status_after=post(admitted_status_client,'/api/account/status',dict(id=patch_id,status=admitted_status_before[0],request_id='e511e511-e511-4e51-8e51-e511e511e511'))
            assert refused_status_after[0]==403,refused_status_after[0]
            with runtime.connect() as check_db:
                kept_status=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert kept_status[0]==admitted_status and kept_status[1]==admitted_status_before[1]
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(status_account,)).fetchone()[0]==admitted_status_history
                assert check_db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/status' AND target=?",(patch_id,)).fetchone()[0]==admitted_status_audit
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                assert 'account_status' not in json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                check_db.execute('UPDATE accounts SET status=? WHERE id=?',(admitted_status_before[0],patch_id))
                restored_status=check_db.execute('SELECT status,nurture_started FROM accounts WHERE id=?',(patch_id,)).fetchone()
                assert restored_status[0]==admitted_status_before[0] and restored_status[1]==admitted_status_before[1]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='e523e523-e523-4e23-8e23-e523e523e523'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            permission_board=get(employee_client,'/api/board')
            permission_daily='daily:'+new['id']+':'+day
            permission_account='account:'+new['id']
            permission_body=dict(id=new['id'],date=day,published=True,request_id='e311e311-e311-4e31-8e31-e311e311e311',_expected_versions={
                permission_account:permission_board['record_versions'][permission_account],
                permission_daily:permission_board['record_versions'].get(permission_daily)})
            with runtime.connect() as raw_db:
                permission_daily_before=raw_db.execute('SELECT count(*) FROM daily WHERE account_id=? AND day=?',(new['id'],day)).fetchone()[0]
                permission_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(permission_daily,)).fetchone()[0]
                permission_account_history_before=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(permission_account,)).fetchone()[0]
            kept_permissions=[item for item in auth.PERMISSIONS if item!='publish']
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/daily',permission_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Permission revoke writer did not reach a real lock wait')
                auth.save_user(holder,owner,dict(id=employee,name='合成员工',permissions=kept_permissions,office_ids=[office_a,office_b],active=True))
                holder.commit()
                permission_waited=future.result(timeout=10)
                assert permission_waited[0]==401,permission_waited[0]
            reduced_client=test_auth_support.client(base,'synthetic_employee')
            assert any(row['id']==new['id'] for row in get(reduced_client,'/api/board')['accounts'])
            refused_publish=post(reduced_client,'/api/daily',dict(id=new['id'],date=day,published=True,request_id='e312e312-e312-4e32-8e32-e312e312e312'))
            assert refused_publish[0]==403,refused_publish[0]
            with runtime.connect() as check_db:
                assert check_db.execute('SELECT count(*) FROM daily WHERE account_id=? AND day=?',(new['id'],day)).fetchone()[0]==permission_daily_before
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(permission_daily,)).fetchone()[0]==permission_history_before
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(permission_account,)).fetchone()[0]==permission_account_history_before
                assert check_db.execute('SELECT 1 FROM idempotency_records WHERE request_id=?',('e311e311-e311-4e31-8e31-e311e311e311',)).fetchone() is None
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                assert 'publish' not in json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
            reduced_versions=get(reduced_client,'/api/board')['record_versions']
            assert reduced_versions.get(permission_daily)==permission_body['_expected_versions'][permission_daily]
            assert reduced_versions[permission_account]==permission_body['_expected_versions'][permission_account]
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='e322e322-e322-4e32-8e32-e322e322e322'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            with runtime.connect() as raw_db:
                admitted_before=raw_db.execute('SELECT published FROM daily WHERE account_id=? AND day=?',('account-b',day)).fetchone()
                admitted_daily_key='daily:account-b:'+day
                admitted_daily_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(admitted_daily_key,)).fetchone()[0]
                admitted_account_history=raw_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:account-b',)).fetchone()[0]
            admitted_published=0 if admitted_before is not None and int(admitted_before[0])==1 else 1
            def revoke_publish():
                with runtime.connect() as revoker:
                    revoker.begin_write()
                    auth.save_user(revoker,owner,dict(id=employee,name='合成员工',permissions=kept_permissions,office_ids=[office_a,office_b],active=True))
                    revoker.commit()
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(revoke_publish)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Permission revoke did not wait for the admitted daily write')
                holder.execute('INSERT INTO daily VALUES(?,?,?) ON CONFLICT(account_id,day) DO UPDATE SET published=excluded.published',('account-b',day,admitted_published))
                holder.commit()
                future.result(timeout=10)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('old session still accepted after admitted write')
            except urllib.error.HTTPError as error:
                assert error.code==401
            admitted_client=test_auth_support.client(base,'synthetic_employee')
            refused_after=post(admitted_client,'/api/daily',dict(id='account-b',date=day,published=admitted_published!=1,request_id='e334e334-e334-4e34-8e34-e334e334e334'))
            assert refused_after[0]==403,refused_after[0]
            with runtime.connect() as check_db:
                assert int(check_db.execute('SELECT published FROM daily WHERE account_id=? AND day=?',('account-b',day)).fetchone()[0])==admitted_published
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',(admitted_daily_key,)).fetchone()[0]==admitted_daily_history
                assert check_db.execute('SELECT count(*) FROM operations_history WHERE entity=?',('account:account-b',)).fetchone()[0]==admitted_account_history
                assert sorted(row[0] for row in check_db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(employee,)).fetchall())==sorted([office_a,office_b])
                assert 'publish' not in json.loads(check_db.execute('SELECT permissions FROM auth_users WHERE id=?',(employee,)).fetchone()[0])
                if admitted_before is None:
                    check_db.execute('DELETE FROM daily WHERE account_id=? AND day=?',('account-b',day))
                else:
                    check_db.execute('UPDATE daily SET published=? WHERE account_id=? AND day=?',(int(admitted_before[0]),'account-b',day))
            assert post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_a,office_b],request_id='e333e333-e333-4e33-8e33-e333e333e333'))[0]==200
            employee_client=test_auth_support.client(base,'synthetic_employee')
            latest=get(employee_client,'/api/board')
            request_body=dict(id=new['id'],date=day,published=True,_expected_versions={
                'account:'+new['id']:latest['record_versions']['account:'+new['id']],
                'daily:'+new['id']+':'+day:latest['record_versions'].get('daily:'+new['id']+':'+day)})
            with runtime.connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                holder.begin_write()
                future=executor.submit(post,employee_client,'/api/daily',request_body)
                deadline=time.monotonic()+4
                while time.monotonic()<deadline:
                    holder.execute('SELECT pg_stat_clear_snapshot()')
                    if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-board-runtime' AND wait_event_type='Lock'").fetchone():break
                    time.sleep(.02)
                else:raise AssertionError('Scoped HTTP writer did not reach a real lock wait')
                scope.grant_user(holder,owner,employee,[office_b]);holder.commit()
                waited=future.result(timeout=10)
                assert waited[0]==401,waited
            employee_client=test_auth_support.client(base,'synthetic_employee')
            assert not any(row['id']==new['id'] for row in get(employee_client,'/api/board')['accounts'])
            assert post(employee_client,'/api/daily',dict(id=new['id'],date=day,published=True))[0]==403
            with runtime.connect() as check_db:
                assert not check_db.execute('SELECT 1 FROM daily WHERE account_id=? AND day=?',(new['id'],day)).fetchone()
                note_before=check_db.execute("SELECT note FROM accounts WHERE id='account-b'").fetchone()[0]
            visible_before=sorted(row['id'] for row in get(employee_client,'/api/board')['accounts'])
            assert 'account-b' in visible_before and 'account-a' not in visible_before and new['id'] not in visible_before
            employee_logged_out=post(employee_client,'/api/auth/logout',{})
            assert employee_logged_out[0]==200 and employee_logged_out[1].get('ok') is True, employee_logged_out[0]
            assert 'password' not in json.dumps(employee_logged_out[1],ensure_ascii=False)
            try:
                get(employee_client,'/api/board')
                raise AssertionError('logged out employee board accepted')
            except urllib.error.HTTPError as error:
                assert error.code==401
            assert get(employee_client,'/api/auth/me')['user'] is None
            with runtime.connect() as check_db:
                assert check_db.execute('SELECT count(*) FROM auth_sessions WHERE user_id=?',(employee,)).fetchone()[0]==0
                assert check_db.execute('SELECT active FROM auth_users WHERE id=?',(employee,)).fetchone()[0]==1
            employee_client=test_auth_support.client(base,'synthetic_employee')
            employee_again=get(employee_client,'/api/auth/me')['user']
            assert employee_again['role']=='employee' and employee_again['username']=='synthetic_employee'
            assert sorted(row['id'] for row in get(employee_client,'/api/board')['accounts'])==visible_before
            disabled=post(owner_client,'/api/auth/users',dict(id=employee,name='合成员工',permissions=list(auth.PERMISSIONS),office_ids=[office_b],active=False,request_id='dadadada-dada-4ada-8ada-dadadadadada'))
            assert disabled[0]==200,disabled
            refused=post(employee_client,'/api/account/patch',dict(id='account-b',changes={'note':'停用后不得写入'},request_id='dbdbdbdb-dbdb-4dbd-8dbd-dbdbdbdbdbdb'))
            assert refused[0]==401,refused
            with runtime.connect() as check_db:
                assert check_db.execute("SELECT note FROM accounts WHERE id='account-b'").fetchone()[0]==note_before
                assert check_db.execute('SELECT active FROM auth_users WHERE id=?',(employee,)).fetchone()[0]==0
            try:
                test_auth_support.client(base,'synthetic_employee')
                raise AssertionError('disabled employee login accepted')
            except urllib.error.HTTPError as error:
                assert error.code==401
            with runtime.connect() as check_db:
                owner_before=check_db.execute('SELECT role,active,name,password_hash FROM auth_users WHERE id=?',(owner['id'],)).fetchone()
                owner_sessions=check_db.execute('SELECT count(*) FROM auth_sessions WHERE user_id=?',(owner['id'],)).fetchone()[0]
            owner_password='synthetic-owner-reset-1'
            owner_attempts=(
                dict(id=owner['id'],name=owner['name'],active=False,request_id='2a2a2a2a-2a2a-42a2-82a2-2a2a2a2a2a2a'),
                dict(id=owner['id'],name=owner['name'],role='viewer',permissions=[],active=True,request_id='2b2b2b2b-2b2b-42b2-82b2-2b2b2b2b2b2b'),
                dict(id=owner['id'],name=owner['name'],password=owner_password,request_id='2c2c2c2c-2c2c-42c2-82c2-2c2c2c2c2c2c'))
            for attempt in owner_attempts:
                refused_owner=post(owner_client,'/api/auth/users',attempt)
                assert refused_owner[0]==403 and refused_owner[1].get('error')=='总管理员不能被修改角色、停用或由人员管理重置密码', refused_owner[0]
                assert owner_password not in json.dumps(refused_owner[1],ensure_ascii=False)
            with runtime.connect() as check_db:
                owner_after=check_db.execute('SELECT role,active,name,password_hash FROM auth_users WHERE id=?',(owner['id'],)).fetchone()
                assert [owner_after[i] for i in range(4)]==[owner_before[i] for i in range(4)]
                assert check_db.execute('SELECT count(*) FROM auth_sessions WHERE user_id=?',(owner['id'],)).fetchone()[0]==owner_sessions
            assert get(owner_client,'/api/auth/me')['user']['role']=='owner'
            other_owner=test_auth_support.client(base,'test_owner')
            with runtime.connect() as check_db:
                viewer_row=check_db.execute("SELECT id,name FROM auth_users WHERE username='synthetic_viewer'").fetchone()
                viewer_daily=check_db.execute('SELECT count(*) FROM daily WHERE account_id=?',('account-a',)).fetchone()[0]
            granted_viewer=post(owner_client,'/api/auth/users',dict(id=viewer_row['id'],name=viewer_row['name'],role='viewer',permissions=[],office_ids=[office_a],active=True,request_id='3a3a3a3a-3a3a-43a3-83a3-3a3a3a3a3a3a'))
            assert granted_viewer[0]==200,granted_viewer[0]
            viewer_client=test_auth_support.client(base,'synthetic_viewer')
            assert any(row['id']=='account-a' for row in get(viewer_client,'/api/board')['accounts'])
            refused_viewer=post(viewer_client,'/api/daily',dict(id='account-a',date=day,published=True,request_id='3b3b3b3b-3b3b-43b3-83b3-3b3b3b3b3b3b'))
            assert refused_viewer[0]==403,refused_viewer[0]
            with runtime.connect() as check_db:
                assert check_db.execute('SELECT count(*) FROM daily WHERE account_id=?',('account-a',)).fetchone()[0]==viewer_daily
            logged_out=post(viewer_client,'/api/auth/logout',{})
            assert logged_out[0]==200 and logged_out[1].get('ok') is True, logged_out[0]
            assert 'password' not in json.dumps(logged_out[1],ensure_ascii=False)
            try:
                get(viewer_client,'/api/board')
                raise AssertionError('logged out viewer board accepted')
            except urllib.error.HTTPError as error:
                assert error.code==401
            assert get(viewer_client,'/api/auth/me')['user'] is None
            with runtime.connect() as check_db:
                assert check_db.execute('SELECT count(*) FROM auth_sessions WHERE user_id=?',(viewer_row['id'],)).fetchone()[0]==0
            viewer_again=test_auth_support.client(base,'synthetic_viewer')
            again=get(viewer_again,'/api/auth/me')['user']
            assert again['role']=='viewer' and again['username']=='synthetic_viewer'
            assert any(row['id']=='account-a' for row in get(viewer_again,'/api/board')['accounts'])
            refused_again=post(viewer_again,'/api/daily',dict(id='account-a',date=day,published=True,request_id='4a4a4a4a-4a4a-44a4-84a4-4a4a4a4a4a4a'))
            assert refused_again[0]==403,refused_again[0]
            with runtime.connect() as check_db:
                assert check_db.execute('SELECT count(*) FROM daily WHERE account_id=?',('account-a',)).fetchone()[0]==viewer_daily
            owner_visible=sorted(row['id'] for row in get(owner_client,'/api/board')['accounts'])
            assert 'account-a' in owner_visible and 'account-b' in owner_visible
            with runtime.connect() as check_db:
                owner_session_count=check_db.execute('SELECT count(*) FROM auth_sessions WHERE user_id=?',(owner['id'],)).fetchone()[0]
            assert owner_session_count>=2
            owner_logged_out=post(owner_client,'/api/auth/logout',{})
            assert owner_logged_out[0]==200 and owner_logged_out[1].get('ok') is True, owner_logged_out[0]
            assert 'password' not in json.dumps(owner_logged_out[1],ensure_ascii=False)
            try:
                get(owner_client,'/api/board')
                raise AssertionError('logged out owner board accepted')
            except urllib.error.HTTPError as error:
                assert error.code==401
            assert get(owner_client,'/api/auth/me')['user'] is None
            with runtime.connect() as check_db:
                assert check_db.execute('SELECT count(*) FROM auth_sessions WHERE user_id=?',(owner['id'],)).fetchone()[0]==owner_session_count-1
                owner_row=check_db.execute('SELECT active,role FROM auth_users WHERE id=?',(owner['id'],)).fetchone()
                assert owner_row[0]==1 and owner_row[1]=='owner'
            kept_owner=get(other_owner,'/api/auth/me')['user']
            assert kept_owner['role']=='owner' and kept_owner['username']=='test_owner'
            assert sorted(row['id'] for row in get(other_owner,'/api/board')['accounts'])==owner_visible
            owner_again=test_auth_support.client(base,'test_owner')
            again_owner=get(owner_again,'/api/auth/me')['user']
            assert again_owner['role']=='owner' and again_owner['username']=='test_owner'
            assert sorted(row['id'] for row in get(owner_again,'/api/board')['accounts'])==owner_visible
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        assert not thread.is_alive()
    checks=[
        'board-office-filter','related-parent-device-computer-masked','shared-entity-filtered-counts',
        'publishing-and-nurture-filter','version-keys-no-foreign-records','no-scope-empty-board','no-scope-image-history-export-hidden',
        'owner-cross-office-visible','cross-office-image-403-own-image-bytes','collection-status-scoped']
    if write_checks:checks+=['no-scope-permitted-history-export-empty','http-user-office-grant','cross-office-write-403','mixed-batch-no-partial-write',
        'shared-write-requires-all-offices','create-explicit-authorized-office','new-card-device-office',
        'relation-does-not-change-scope','linked-account-stays-hidden','failed-office-change-rolls-back-fields','no-out-of-scope-nurture-sync','history-scope-stamped']
    if write_checks:checks+=['history-unknown-admin-only','foreign-history-denied','export-relations-masked','mixed-export-denied','shared-card-device-export-scoped']
    if write_checks:checks+=['import-explicit-office','import-second-row-rolls-back-first','import-new-scope-persisted','import-foreign-preview-redacted','import-relation-error-redacted','import-rechecks-revoked-scope']
    if write_checks:checks+=['personnel-scope-readback','owner-office-create-employee-denied']
    if write_checks:checks+=['pending-entity-scope-assignment','stale-scope-version-conflict','single-office-constraint-rollback','scope-inventory-owner-only']
    if write_checks:checks+=['single-office-create-survives-relogin','multi-office-create-survives-relogin','admin-global-read-action-limited','admin-cross-office-image-bytes','admin-cross-office-history-export','admin-cross-office-card-device-export','admin-cannot-grant','personnel-audit-omits-password','scope-revoked-during-real-lock-wait','permission-revoked-during-real-lock-wait','status-permission-revoked-during-real-lock-wait','lifecycle-permission-revoked-during-real-lock-wait','device-permission-revoked-during-real-lock-wait','card-permission-revoked-during-real-lock-wait','admitted-card-commits-before-permission-revoke','publishing-permission-revoked-during-real-lock-wait','admitted-publishing-commits-before-permission-revoke','nurture-permission-revoked-during-real-lock-wait','admitted-nurture-commits-before-permission-revoke','account-permission-revoked-during-real-lock-wait','admitted-account-commits-before-permission-revoke','card-task-permission-revoked-during-real-lock-wait','admitted-card-task-commits-before-permission-revoke','import-permission-revoked-during-real-lock-wait','admitted-import-commits-before-permission-revoke','account-create-permission-revoked-during-real-lock-wait','admitted-account-create-commits-before-permission-revoke','card-create-permission-revoked-during-real-lock-wait','admitted-card-create-commits-before-permission-revoke','device-create-permission-revoked-during-real-lock-wait','admitted-device-create-commits-before-permission-revoke','card-import-permission-revoked-during-real-lock-wait','admitted-card-import-commits-before-permission-revoke','batch-permission-revoked-during-real-lock-wait','admitted-batch-commits-before-permission-revoke','bulk-permission-revoked-during-real-lock-wait','admitted-bulk-commits-before-permission-revoke','patch-permission-revoked-during-real-lock-wait','admitted-patch-commits-before-permission-revoke','readiness-permission-revoked-during-real-lock-wait','admitted-readiness-commits-before-permission-revoke','phone-assign-permission-revoked-during-real-lock-wait','admitted-phone-assign-commits-before-permission-revoke','nurture-date-permission-revoked-during-real-lock-wait','admitted-nurture-date-commits-before-permission-revoke','link-permission-revoked-during-real-lock-wait','admitted-link-commits-before-permission-revoke','nurture-start-permission-revoked-during-real-lock-wait','admitted-nurture-start-commits-before-permission-revoke','nurture-verification-permission-revoked-during-real-lock-wait','admitted-nurture-verification-commits-before-permission-revoke','nurture-resume-permission-revoked-during-real-lock-wait','admitted-nurture-resume-commits-before-permission-revoke','nurture-finish-permission-revoked-during-real-lock-wait','admitted-nurture-finish-commits-before-permission-revoke','nurture-upload-permission-revoked-during-real-lock-wait','admitted-nurture-upload-commits-before-permission-revoke','nurture-delete-image-permission-revoked-during-real-lock-wait','admitted-nurture-delete-image-commits-before-permission-revoke','nurture-purge-permission-revoked-during-real-lock-wait','admitted-nurture-purge-commits-before-permission-revoke','nurture-join-permission-revoked-during-real-lock-wait','admitted-nurture-join-commits-before-permission-revoke','admitted-device-commits-before-permission-revoke','admitted-lifecycle-commits-before-permission-revoke','admitted-status-commits-before-permission-revoke','admitted-daily-commits-before-permission-revoke','fresh-session-rechecks-current-office','disabled-employee-write-rejected','password-reset-old-session-rejected','owner-personnel-save-rejected','viewer-office-write-rejected','viewer-logout-invalidates-session','employee-logout-invalidates-session','admin-logout-invalidates-session','owner-logout-invalidates-session']
    if write_checks:checks+=['inferred-foreign-parent-rejected-and-rolled-back','inferred-authorized-parent-still-supported']
    if write_checks:checks+=['atomic-patch-without-account-manage','late-field-failure-rolls-back','unauthorized-patch-field-rejects-all','unknown-patch-field-rejected','stale-patch-conflict','omitted-null-fields-preserved','same-record-patch-conflict','card-same-version-conflict','device-same-version-conflict','readiness-same-version-conflict','lifecycle-same-version-conflict','account-status-same-version-conflict','bulk-status-same-version-conflict','batch-edit-same-version-conflict','account-save-same-version-conflict','nurture-date-same-version-conflict','nurture-join-same-version-conflict','nurture-start-same-version-conflict','nurture-verification-same-version-conflict','nurture-pause-same-version-conflict','nurture-resume-same-version-conflict','nurture-finish-same-version-conflict','nurture-upload-same-version-conflict','nurture-delete-image-same-version-conflict','nurture-purge-same-version-conflict','publishing-assignment-same-version-conflict','different-record-patches-both-commit','create-same-request-replays','create-request-id-reuse-rejected','create-failed-request-replays','create-insert-rolls-back-before-name','create-old-generation-rejected','create-replay-rechecks-permission','card-create-same-request-replays','card-create-request-id-reuse-rejected','card-update-same-request-replays','card-update-request-id-reuse-rejected','card-update-failed-request-replays','card-update-replay-rechecks-permission','device-create-same-request-replays','device-create-request-id-reuse-rejected','device-update-same-request-replays','device-update-request-id-reuse-rejected','device-update-failed-request-replays','device-update-replay-rechecks-permission','assignment-same-request-replays','assignment-request-id-reuse-rejected','assignment-failed-request-replays','assignment-replay-rechecks-permission','link-same-request-replays','link-request-id-reuse-rejected','link-failed-request-replays','link-replay-rechecks-permission','phone-assign-same-request-replays','phone-assign-request-id-reuse-rejected','phone-assign-failed-request-replays','phone-assign-replay-rechecks-permission','readiness-same-request-replays','readiness-request-id-reuse-rejected','readiness-failed-request-replays','readiness-replay-rechecks-permission','lifecycle-same-request-replays','lifecycle-request-id-reuse-rejected','lifecycle-failed-request-replays','lifecycle-replay-rechecks-permission','daily-same-request-replays','daily-request-id-reuse-rejected','daily-failed-request-replays','daily-replay-rechecks-permission','bulk-status-same-request-replays','bulk-status-request-id-reuse-rejected','bulk-status-failed-request-replays','bulk-status-replay-rechecks-permission','batch-edit-same-request-replays','batch-edit-request-id-reuse-rejected','batch-edit-failed-request-replays','batch-edit-replay-rechecks-permission','account-save-same-request-replays','account-save-request-id-reuse-rejected','account-save-failed-request-replays','account-save-replay-rechecks-permission','nurture-date-same-request-replays','nurture-date-request-id-reuse-rejected','nurture-date-failed-request-replays','nurture-date-replay-rechecks-permission','card-import-same-request-replays','card-import-request-id-reuse-rejected','card-import-missing-office-rejected','card-import-replay-rechecks-permission','office-create-same-request-replays','office-create-request-id-reuse-rejected','office-create-failed-request-replays','office-create-replay-rechecks-owner','entity-scope-same-request-replays','entity-scope-request-id-reuse-rejected','entity-scope-failed-request-replays','entity-scope-replay-rechecks-owner','idempotency-result-within-retention-replays','idempotency-result-expired-not-replayed','idempotency-expired-tombstone-rejects-reuse','idempotency-failure-expired-not-replayed','idempotency-lookup-missing','idempotency-lookup-succeeded','idempotency-lookup-failed','idempotency-lookup-pending','idempotency-lookup-old-generation','idempotency-lookup-expired','idempotency-lookup-rechecks-permission','card-device-replay-rechecks-permission','personnel-save-same-request-replays','personnel-save-request-id-reuse-rejected','personnel-update-same-request-replays','personnel-failed-request-replays','personnel-replay-rechecks-owner','import-apply-same-request-replays','import-apply-request-id-reuse-rejected','import-apply-failed-request-replays','import-apply-replay-rechecks-permission','nurture-upload-same-request-replays','nurture-upload-request-id-reuse-rejected','nurture-upload-failed-request-replays','nurture-upload-replay-rechecks-permission','nurture-join-same-request-replays','nurture-join-request-id-reuse-rejected','nurture-join-failed-request-replays','nurture-join-replay-rechecks-permission','nurture-start-same-request-replays','nurture-start-request-id-reuse-rejected','nurture-start-failed-request-replays','nurture-start-replay-rechecks-permission','nurture-verification-same-request-replays','nurture-verification-request-id-reuse-rejected','nurture-verification-failed-request-replays','nurture-verification-replay-rechecks-permission','nurture-pause-same-request-replays','nurture-pause-request-id-reuse-rejected','nurture-pause-failed-request-replays','nurture-pause-replay-rechecks-permission','nurture-resume-same-request-replays','nurture-resume-request-id-reuse-rejected','nurture-resume-failed-request-replays','nurture-resume-replay-rechecks-permission','nurture-finish-same-request-replays','nurture-finish-request-id-reuse-rejected','nurture-finish-failed-request-replays','nurture-finish-replay-rechecks-permission','nurture-delete-image-same-request-replays','nurture-delete-image-request-id-reuse-rejected','nurture-delete-image-failed-request-replays','nurture-delete-image-replay-rechecks-permission','nurture-purge-same-request-replays','nurture-purge-request-id-reuse-rejected','nurture-purge-failed-request-replays','nurture-purge-replay-rechecks-permission','account-patch-same-request-replays','account-patch-request-id-reuse-rejected','account-patch-failed-request-replays','account-patch-replay-rechecks-permission','account-status-same-request-replays','account-status-request-id-reuse-rejected','account-status-failed-request-replays','account-status-replay-rechecks-permission']
    print(json.dumps({'passed':True,'kind':'core-scope-http-not-complete-export-history-acceptance','checks':checks},ensure_ascii=True))


if __name__=='__main__':main('--writes' in sys.argv)
