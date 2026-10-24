"""Synthetic-only browser harness. Like pg_process_checks, parent owns children;
the web child installs isolation before importing application code.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from test_isolation import PROJECT, checked_root
from pg_synthetic_migration import connect_synthetic, install_schema
from pg_service_adapter import ServiceConnection
from pg_admin import manage_owner
import auth
import office_scope
import test_auth_support


def main():
    root=checked_root()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    generation='synthetic-browser-1'
    database=dict(host='127.0.0.1',port=int(os.environ['PGPORT']),database='synthetic_probe',user='synthetic_owner',schema='board_candidate',writer_epoch=1,restore_generation=generation,connection_limit=6,synthetic_root=str(root))
    config=root/'synthetic-web-config.json'
    config.write_text(json.dumps(dict(enabled=True,backend='postgresql',host='127.0.0.1',port=port,database=database)),encoding='utf-8')
    with connect_synthetic() as conn:
        install_schema(conn)
        manage_owner(conn,operation='initialize-owner',username='test_owner',name='合成管理员',password=test_auth_support.TEST_PASSWORD,operator='synthetic-browser',reason='synthetic-only')
        conn.execute('SET search_path=board_candidate,pg_catalog')
        db=ServiceConnection(conn,writer_epoch=1,restore_generation=generation,schema='board_candidate')
        with conn.transaction():
            owner=auth.public_user(db.execute("SELECT * FROM auth_users WHERE role='owner'").fetchone())
            employee=auth.save_user(db,owner,dict(username='synthetic_employee',name='合成浏览器员工',password=test_auth_support.TEST_PASSWORD,permissions=list(auth.PERMISSIONS)))
            for ident,name in [('pc-a','合成A电脑'),('pc-b','范围外合成电脑')]:db.execute('INSERT INTO computers(id,name) VALUES(?,?)',(ident,name))
            for ident,name,computer in [('account-a','合成A账号','pc-a'),('account-b','范围外合成账号','pc-b'),('account-c','合成待关联账号','pc-a')]:db.execute("INSERT INTO accounts(id,name,computer_id,status) VALUES(?,?,?,'正常')",(ident,name,computer))
            db.execute("INSERT INTO publishing_layout_state VALUES(1,'synthetic-browser')")
            db.execute("INSERT INTO publishing_layout VALUES('synthetic-browser','pc-a',1,'account-a','合成格子A','','','','')")
            db.execute("INSERT INTO publishing_layout VALUES('synthetic-browser','pc-a',2,NULL,'合成待匹配格','','待匹配2','','需核对')")
            db.execute("UPDATE accounts SET registration_phone='19900000009',account_type='大号' WHERE id='account-a'")
            db.execute("UPDATE accounts SET registration_phone='19900000010' WHERE id='account-c'")
            db.execute("UPDATE runtime_control SET mode='open',restore_generation=?",(generation,))
        db.begin_write()
        office_a=office_scope.create_office(db,owner,'合成办公室A');office_b=office_scope.create_office(db,owner,'范围外合成办公室B')
        for kind,ident,office in [('accounts','account-a',office_a),('accounts','account-b',office_b),('accounts','account-c',office_a),('computers','pc-a',office_a),('computers','pc-b',office_b)]:office_scope.assign_entity(db,owner,kind,ident,[office])
        office_scope.grant_user(db,owner,employee,[office_a]);db.commit()
    base='http://127.0.0.1:'+str(port)
    environment=dict(os.environ,PORT=str(port),DOUYIN_BOARD_DATABASE_PASSWORD=os.environ['PGPASSWORD'])
    log_path=root/'browser-web.log'
    with log_path.open('wb') as log:
        server=subprocess.Popen([sys.executable,'-B',str(PROJECT/'web_server.py'),'--config',str(config)],cwd=PROJECT,env=environment,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        deadline=time.monotonic()+20
        while True:
            if server.poll() is not None:raise AssertionError('Synthetic browser server exited')
            try:
                with urllib.request.urlopen(base+'/api/health',timeout=1) as response:
                    if json.load(response)['shared']:break
            except (urllib.error.URLError,TimeoutError):pass
            if time.monotonic()>deadline:raise AssertionError('Synthetic browser server readiness timeout')
            time.sleep(.05)
        temporary=root/'browser-temporary';temporary.mkdir()
        browser_environment={key:value for key,value in os.environ.items() if not key.upper().startswith('PG') and key!='DOUYIN_BOARD_DATABASE_PASSWORD'}
        browser_environment.update(SYNTHETIC_URL=base,SYNTHETIC_PASSWORD=test_auth_support.TEST_PASSWORD,SYNTHETIC_OFFICE=office_a,SYNTHETIC_EMPLOYEE=str(employee),SYNTHETIC_ROOT=str(root),TEMP=str(temporary),TMP=str(temporary))
        child=subprocess.Popen(['node',str(PROJECT/'browser_scope_checks.cjs')],cwd=PROJECT,env=browser_environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:output,error=child.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            # Only the still-owned test child and its descendants, never a
            # name-based scan or the user's existing browser processes.
            subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],capture_output=True,timeout=15,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            child.communicate(timeout=10)
            raise RuntimeError('Synthetic browser timed out; owned process tree stopped') from None
        if child.returncode:raise RuntimeError(error.decode('utf-8',errors='replace').replace(test_auth_support.TEST_PASSWORD,'[redacted]')[-4000:])
        report=json.loads(output)
        assert test_auth_support.TEST_PASSWORD not in log_path.read_text(encoding='utf-8')
    finally:
        if server.poll() is None:
            server.terminate()
            try:server.wait(timeout=10)
            except subprocess.TimeoutExpired:server.kill();server.wait(timeout=10)
    report['server_stopped']=server.poll() is not None
    print(json.dumps(report,ensure_ascii=True))


if __name__=='__main__':main()
