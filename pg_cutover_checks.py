"""Synthetic first switch preserves credentials/data while revoking old tokens."""
import hashlib
import json
import sqlite3
import psycopg
from test_isolation import checked_root, install
from pg_synthetic_migration import connect_synthetic, install_schema, first_cutover_synthetic
from pg_service_adapter import ServiceConnection


def main():
    root=checked_root()
    if (root/'data').exists():raise ValueError('Fresh source required')
    install(root)
    import app
    import auth
    from test_release_core import png
    app.initialize()
    password='Synthetic-cutover-password-81'
    with sqlite3.connect(app.DB) as source:
        source.row_factory=sqlite3.Row
        setup=auth.mint_setup(source)
        old_token=auth.setup_owner(source,dict(username='synthetic_owner',name='合成迁移管理员',password=password),'board_setup='+setup)
        owner=auth.session(source,'board_session='+old_token)
        source.execute("INSERT INTO auth_setup VALUES('synthetic-old-setup',9999999999)")
        source.execute("INSERT INTO operations_previews VALUES('synthetic-preview',?,9999999999,'import','{}')",(owner['id'],))
        source.execute("INSERT INTO collection_jobs(account_id,homepage,token,state) VALUES('synth-account-1','','synthetic-old-worker-token','running')")
        source.execute("UPDATE collection_worker SET seen=123,status='synthetic-online',command='synthetic-command'")
        image=png()
        source.execute("INSERT INTO nurture_images VALUES('synthetic-image','synth-account-1',1,'2026-10-21','image/png',?,'合成人员')",(image,))
        source.commit()
        before=hashlib.sha256(app.DB.read_bytes()).hexdigest()
        with connect_synthetic() as target:
            install_schema(target)
            target.execute("ALTER TABLE board_candidate.runtime_control ADD CONSTRAINT synthetic_cutover_failure CHECK(restore_generation='uninitialized')")
            try:
                first_cutover_synthetic(target,source)
                raise AssertionError('Expected final-stage cutover failure')
            except psycopg.errors.CheckViolation:
                pass
            for table in ('accounts','auth_users','auth_sessions','collection_jobs','nurture_images'):
                assert target.execute('SELECT count(*) FROM board_candidate.'+table).fetchone()[0]==0
            assert target.execute('SELECT mode,restore_generation FROM board_candidate.runtime_control').fetchone()==('maintenance','uninitialized')
            target.execute('ALTER TABLE board_candidate.runtime_control DROP CONSTRAINT synthetic_cutover_failure')
            result=first_cutover_synthetic(target,source)
            assert result['removed_sessions']==1 and result['removed_setup_tokens']==1 and result['removed_previews']==1
            target.execute('SET search_path=board_candidate,pg_catalog')
            db=ServiceConnection(target)
            assert auth.session(db,'board_session='+old_token) is None
            assert db.execute('SELECT count(*) FROM auth_setup').fetchone()[0]==0
            assert db.execute('SELECT count(*) FROM operations_previews').fetchone()[0]==0
            job=db.execute('SELECT state,token FROM collection_jobs').fetchone()
            assert job[0]=='running' and job[1]==''
            worker=db.execute('SELECT seen,status,command FROM collection_worker').fetchone()
            assert tuple(worker.values)==(0.0,'','')
            assert db.execute('SELECT content FROM nurture_images').fetchone()[0]==image
            assert db.execute('SELECT password_hash FROM auth_users').fetchone()[0]==source.execute('SELECT password_hash FROM auth_users').fetchone()[0]
            assert db.execute('SELECT count(*) FROM auth_audit').fetchone()[0]==source.execute('SELECT count(*) FROM auth_audit').fetchone()[0]
            assert auth.session(source,'board_session='+old_token)['id']==owner['id']
            with target.transaction():
                new_token=auth.login(db,dict(username='synthetic_owner',password=password),'127.0.0.1')
                assert new_token and auth.session(db,'board_session='+new_token)['id']==owner['id']
            assert db.execute('SELECT mode FROM runtime_control').fetchone()[0]=='maintenance'
            assert result['restore_generation']!='uninitialized'
        assert hashlib.sha256(app.DB.read_bytes()).hexdigest()==before
    print(json.dumps({'passed':True,'kind':'synthetic-first-cutover-not-production-migration','summary':result,'checks':[
        'final-stage-failure-rolls-back-copy-and-invalidation','source-file-unchanged','old-session-rejected','setup-and-preview-revoked','password-hash-compatible-new-login',
        'task-history-retained-worker-token-invalidated','worker-heartbeat-command-cleared',
        'image-bytes-and-auth-audit-preserved','new-generation-maintenance-retained']},ensure_ascii=True))


if __name__=='__main__':main()
