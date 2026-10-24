"""Reviewed subprocess harness: only web_server and the freshly generated cluster.

The parent intentionally does not install the no-subprocess audit hook. Each web
child installs it before importing the application. No existing config is read.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from test_isolation import PROJECT, checked_root
from pg_synthetic_migration import connect_synthetic, install_schema
from pg_service_adapter import ServiceConnection
from pg_admin import manage_owner
import account_service
import auth
import test_auth_support


def main():
    root=checked_root()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    database=dict(host='127.0.0.1',port=int(os.environ['PGPORT']),database='synthetic_probe',user='synthetic_owner',
                  schema='board_candidate',writer_epoch=1,restore_generation='synthetic-process-1',connection_limit=3,synthetic_root=str(root))
    config=dict(enabled=True,backend='postgresql',host='127.0.0.1',port=65534,database=database)
    config_path=root/'synthetic-web-config.json'
    config_path.write_text(json.dumps(config),encoding='utf-8')
    with connect_synthetic() as conn:
        install_schema(conn)
        manage_owner(conn,operation='initialize-owner',username='test_owner',name='合成管理员',password=test_auth_support.TEST_PASSWORD,
                     operator='synthetic-process-check',reason='synthetic-init')
        conn.execute('SET search_path=board_candidate,pg_catalog')
        db=ServiceConnection(conn)
        with conn.transaction():
            owner=auth.public_user(db.execute("SELECT * FROM auth_users WHERE role='owner'").fetchone())
            db.execute("INSERT INTO computers(id,name) VALUES('synthetic-pc','合成电脑')")
            account=account_service.save(db,dict(name='合成进程账号',computer_id='synthetic-pc',slot=1,status='正常'),owner,'2026-10-21',create=True)
            db.execute("UPDATE runtime_control SET mode='open',restore_generation='synthetic-process-1'")
    environment=dict(os.environ,PORT=str(port),DOUYIN_BOARD_DATABASE_PASSWORD=os.environ['PGPASSWORD'])
    base='http://127.0.0.1:'+str(port)
    processes=[]
    logs=[]
    def start(environment):
        log_path=root/('web-process-'+str(len(processes))+'.log')
        with log_path.open('wb') as output:
            process=subprocess.Popen([sys.executable,'-B',str(PROJECT/'web_server.py'),'--config',str(config_path)],
                cwd=PROJECT,env=environment,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        processes.append(process);logs.append(log_path)
        return process
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=10)
    def ready(process):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if process.poll() is not None:raise AssertionError('Synthetic web child exited before readiness')
            try:
                with urllib.request.urlopen(base+'/api/health',timeout=1) as response:
                    if json.load(response)['shared']:return
            except (urllib.error.URLError,TimeoutError):pass
            time.sleep(.05)
        raise AssertionError('Synthetic web child readiness timed out')
    def board(client):
        with client.open(base+'/api/board',timeout=10) as response:return json.load(response)
    def publish(client,value,day,versions=None):
        request_id='b4b4b4b4-b4b4-4b4b-8b4b-b4b4b4b4b4b4' if value else 'b5b5b5b5-b5b5-4b5b-8b5b-b5b5b5b5b5b5'
        payload=dict(id=account,date=day,published=value,request_id=request_id)
        if versions is not None:payload['_expected_versions']=versions
        request=urllib.request.Request(base+'/api/daily',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
        with client.open(request,timeout=10) as response:assert json.load(response)['ok']
    def counts():
        with connect_synthetic() as conn:
            conn.execute('SET search_path=board_candidate,pg_catalog')
            return (conn.execute("SELECT COUNT(*) FROM auth_audit WHERE action='/api/daily'").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM idempotency_records WHERE request_id='b4b4b4b4-b4b4-4b4b-8b4b-b4b4b4b4b4b4'").fetchone()[0],
                conn.execute('SELECT COUNT(*) FROM daily').fetchone()[0])
    try:
        first=start(environment);ready(first)
        client=test_auth_support.client(base)
        initial=board(client);day=initial['today']
        versions={key:initial['record_versions'].get(key) for key in ('account:'+account,'daily:'+account+':'+day)}
        publish(client,True,day,versions)
        stop(first)
        second=start(environment);ready(second)
        current=board(client)  # Same cookie and CSRF, no new login after process replacement.
        assert current['accounts'][0]['published']==1
        assert current['accounts'][0]['id']==account
        before=counts()
        publish(client,True,day,versions)
        assert board(client)['accounts'][0]['published']==1
        after=counts()
        assert before==(1,1,1) and after==before
        publish(client,False,day)
        assert board(client)['accounts'][0]['published']==0
        stop(second)
        missing=dict(environment);missing.pop('DOUYIN_BOARD_DATABASE_PASSWORD')
        rejected=start(missing)
        assert rejected.wait(timeout=15)!=0
        assert not (root/'data'/'board.db').exists()
        for log_path in logs:
            log=log_path.read_text(encoding='utf-8')
            assert environment['DOUYIN_BOARD_DATABASE_PASSWORD'] not in log
            assert test_auth_support.TEST_PASSWORD not in log
            assert 'board_session' not in log
            assert '"published"' not in log
    finally:
        for process in processes:stop(process)
    print(json.dumps({'passed':True,'processes_stopped':all(p.poll() is not None for p in processes),
        'kind':'local-process-replacement-not-cloud-container-acceptance','checks':[
        'real-web-server-cli-start','platform-port-override','pg-saved-value-after-process-termination',
        'session-and-csrf-preserved-after-restart','same-request-replays-after-restart','write-after-restart','missing-secret-startup-rejected',
        'no-sqlite-fallback-file','no-password-in-process-logs','request-body-and-cookie-absent-from-process-logs']},ensure_ascii=True))


if __name__=='__main__':main()
