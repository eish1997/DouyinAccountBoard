"""Real multi-session checks against a cluster created by probe_postgresql only."""
import concurrent.futures
import json
import os
from pathlib import Path
import threading
import time
import psycopg
from test_isolation import checked_root
from pg_write_protocol import write_transaction, AdmissionRejected


def main():
    root = checked_root()
    if os.environ.get('PGHOST') != '127.0.0.1' or os.environ.get('PGDATABASE') != 'synthetic_probe' or os.environ.get('PGUSER') != 'synthetic_owner':
        raise ValueError('Explicit synthetic PostgreSQL endpoint required')
    def connect():
        conn = psycopg.connect(autocommit=True, connect_timeout=5)
        actual = Path(conn.execute('SHOW data_directory').fetchone()[0]).resolve()
        marker = conn.execute('SELECT kind FROM board_app.environment').fetchone()
        if actual != root / 'cluster' or marker != ('synthetic-only',):
            conn.close()
            raise ValueError('Synthetic cluster marker or location mismatch')
        return conn
    generation = 'synthetic-generation-1'
    def wait_for_lock(observer, pid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            observer.execute('SELECT pg_stat_clear_snapshot()')
            row = observer.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s', (pid,)).fetchone()
            if row and row[0] == 'Lock':
                return
            time.sleep(0.02)
        raise AssertionError('No database lock wait was observed')
    with connect() as conn:
        conn.execute("""CREATE TABLE board_app.runtime_control(id integer PRIMARY KEY CHECK(id=1),
          mode text NOT NULL CHECK(mode IN ('maintenance','open')),writer_epoch bigint NOT NULL,
          restore_generation text NOT NULL);
          INSERT INTO board_app.runtime_control VALUES(1,'open',1,'synthetic-generation-1');
          CREATE TABLE board_app.protocol_record(id integer PRIMARY KEY,version integer NOT NULL,value text NOT NULL);
          INSERT INTO board_app.protocol_record VALUES(1,1,'initial');
          CREATE TABLE board_app.protocol_audit(actor integer NOT NULL,value text NOT NULL);
          CREATE TABLE board_app.protocol_user(id integer PRIMARY KEY,active boolean NOT NULL);
          INSERT INTO board_app.protocol_user VALUES(1,true);""")
    barrier = threading.Barrier(2, timeout=10)
    def compete(actor):
        with connect() as conn:
            observed = conn.execute('SELECT version FROM board_app.protocol_record WHERE id=1').fetchone()[0]
            barrier.wait()
            try:
                with write_transaction(conn, writer_epoch=1, restore_generation=generation):
                    changed = conn.execute('UPDATE board_app.protocol_record SET version=version+1,value=%s WHERE id=1 AND version=%s RETURNING id', (str(actor), observed)).fetchone()
                    if not changed:
                        raise AdmissionRejected('VERSION_CONFLICT')
                    conn.execute('INSERT INTO board_app.protocol_audit VALUES(%s,%s)', (actor, str(actor)))
                return 'success'
            except AdmissionRejected as error:
                return error.code
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(compete, actor) for actor in (1, 2)]
        outcomes = sorted(future.result(timeout=20) for future in futures)
    if outcomes != ['VERSION_CONFLICT', 'success']:
        raise AssertionError('Concurrent old-version writes were not exclusive')
    with connect() as conn:
        record = conn.execute('SELECT version,value FROM board_app.protocol_record WHERE id=1').fetchone()
        audit = conn.execute('SELECT actor,value FROM board_app.protocol_audit').fetchall()
        if len(audit) != 1 or record != (2, audit[0][1]) or str(audit[0][0]) != audit[0][1]:
            raise AssertionError('Concurrent audit belongs to a different writer')
    # Holder locks admission before the waiting writer can check authorization.
    started = threading.Event()
    backend = []
    def waiting_writer():
        with connect() as conn:
            backend.append(conn.info.backend_pid)
            started.set()
            try:
                with write_transaction(conn, writer_epoch=1, restore_generation=generation):
                    if not conn.execute('SELECT active FROM board_app.protocol_user WHERE id=1').fetchone()[0]:
                        raise AdmissionRejected('USER_DISABLED')
                    conn.execute("UPDATE board_app.protocol_record SET value='unauthorized' WHERE id=1")
                return 'unexpected-success'
            except AdmissionRejected as error:
                return error.code
    with connect() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        with holder.transaction():
            holder.execute('SELECT id FROM board_app.runtime_control WHERE id=1 FOR UPDATE')
            future = executor.submit(waiting_writer)
            if not started.wait(5):
                raise AssertionError('Waiting writer did not start')
            wait_for_lock(holder, backend[0])
            holder.execute('UPDATE board_app.protocol_user SET active=false WHERE id=1')
        if future.result(timeout=10) != 'USER_DISABLED':
            raise AssertionError('Waiting writer reused authorization')
    # Reverse ordering: an admitted writer completes before revocation commits.
    with connect() as conn:
        conn.execute('UPDATE board_app.protocol_user SET active=true WHERE id=1')
    revoker_started = threading.Event()
    revoker_backend = []
    def revoke_after_writer():
        with connect() as conn:
            revoker_backend.append(conn.info.backend_pid)
            revoker_started.set()
            with conn.transaction():
                conn.execute("SET LOCAL lock_timeout='5s'")
                conn.execute('SELECT id FROM board_app.runtime_control WHERE id=1 FOR UPDATE')
                conn.execute('UPDATE board_app.protocol_user SET active=false WHERE id=1')
        return 'revoked'
    with connect() as writer, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        with write_transaction(writer, writer_epoch=1, restore_generation=generation):
            if not writer.execute('SELECT active FROM board_app.protocol_user WHERE id=1').fetchone()[0]:
                raise AssertionError('Synthetic writer was not authorized')
            writer.execute("UPDATE board_app.protocol_record SET value='authorized-before-revoke' WHERE id=1")
            future = executor.submit(revoke_after_writer)
            if not revoker_started.wait(5):
                raise AssertionError('Revoker did not start')
            wait_for_lock(writer, revoker_backend[0])
        if future.result(timeout=10) != 'revoked':
            raise AssertionError('Revocation failed to finish')
    with connect() as conn:
        for epoch, gen, code in ((0, generation, 'WRITER_VERSION_RETIRED'), (1, 'old-generation', 'RESTORE_GENERATION_EXPIRED')):
            try:
                with write_transaction(conn, writer_epoch=epoch, restore_generation=gen):
                    raise AssertionError('Stale admission unexpectedly allowed')
            except AdmissionRejected as error:
                if error.code != code:
                    raise
        conn.execute("UPDATE board_app.runtime_control SET mode='maintenance' WHERE id=1")
    with connect() as conn:
        try:
            with write_transaction(conn, writer_epoch=1, restore_generation=generation):
                raise AssertionError('Maintenance unexpectedly allowed writing')
        except AdmissionRejected as error:
            if error.code != 'MAINTENANCE':
                raise
        if conn.execute('SELECT value FROM board_app.protocol_record WHERE id=1').fetchone()[0] == 'unauthorized':
            raise AssertionError('Revoked write was persisted')
        if conn.execute('SELECT active FROM board_app.protocol_user WHERE id=1').fetchone()[0]:
            raise AssertionError('Revocation was not committed after admitted writer')
        if conn.execute('SELECT value FROM board_app.protocol_record WHERE id=1').fetchone()[0] != 'authorized-before-revoke':
            raise AssertionError('Admitted writer did not finish before revocation')
    print(json.dumps({'kind': 'protocol-primitives-not-http-acceptance', 'passed': True,
          'checks': ['barrier-CAS-one-winner', 'audit-ownership', 'observed-lock-wait-revocation',
                     'admitted-writer-before-revocation', 'old-writer-epoch', 'old-restore-generation', 'maintenance-across-connections']}))


if __name__ == '__main__':
    main()
