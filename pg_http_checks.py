"""Existing HTTP handler with an injected synthetic PG connection factory.

This is not a production configuration or a CloudBase deployment entrypoint.
"""
from contextlib import contextmanager
import concurrent.futures
import datetime
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch
from test_isolation import checked_root, install
from pg_synthetic_migration import connect_synthetic, install_schema
from pg_service_adapter import ServiceConnection


def main():
    install(checked_root())
    import app
    import auth
    import account_service
    import idempotency
    import test_auth_support
    with connect_synthetic() as conn:
        install_schema(conn)
        conn.execute('SET search_path=board_candidate,pg_catalog')
        db = ServiceConnection(conn)
        with conn.transaction():
            setup = auth.mint_setup(db)
            token = auth.setup_owner(db, dict(username='test_owner', password=test_auth_support.TEST_PASSWORD), 'board_setup=' + setup)
            owner = auth.session(db, 'board_session=' + token)
            employee = auth.save_user(db, owner, dict(username='synthetic_employee', password=test_auth_support.TEST_PASSWORD))
            auth.save_user(db, owner, dict(username='synthetic_peer', password=test_auth_support.TEST_PASSWORD))
            db.execute("INSERT INTO computers(id,name) VALUES('synthetic-pc','合成电脑')")
            account = account_service.save(db, dict(computer_id='synthetic-pc', slot=1, name='合成账号', status='正常'), owner, app.today(), create=True)
            db.execute('INSERT INTO collection_settings(id,auto_enabled) VALUES(1,0)')
            db.execute('INSERT INTO collection_worker(id) VALUES(1)')
            db.execute("UPDATE runtime_control SET mode='open',restore_generation='synthetic-http-1'")

    @contextmanager
    def connect():
        with connect_synthetic() as conn:
            conn.execute('SET search_path=board_candidate,pg_catalog')
            conn.execute("SET application_name='synthetic-http-check'")
            db = ServiceConnection(conn, writer_epoch=1, restore_generation='synthetic-http-1', schema='board_candidate')
            try:
                yield db
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    website_logs = []
    class QuietHandler(app.Handler):
        def log_message(self, *args):
            if not args:
                return
            message = args[0] % args[1:] if len(args) > 1 else str(args[0])
            website_logs.append(str(self.client_address[0]) + ' ' + message)

    with patch.object(app, 'connect', connect):
        server = app.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            base = 'http://127.0.0.1:' + str(server.server_port)
            owner_client = test_auth_support.client(base)
            employee_client = test_auth_support.client(base, 'synthetic_employee')
            def get_board(client):
                with client.open(base + '/api/board', timeout=10) as response:
                    return json.load(response)
            def post(client, body):
                request = urllib.request.Request(base + '/api/daily', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with client.open(request, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            board = get_board(owner_client)
            assert board['accounts'][0]['name'] == '合成账号'
            key = 'account:' + account
            day = app.today()
            body = dict(id=account, date=day, published=True, request_id='b3b3b3b3-b3b3-4b3b-8b3b-b3b3b3b3b3b3', _expected_versions={key: board['record_versions'][key], 'daily:' + account + ':' + day: None})
            barrier = threading.Barrier(2, timeout=5)
            def race(client):
                barrier.wait()
                return post(client, body)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(race, client) for client in (owner_client, employee_client)]
                results = [future.result(timeout=15) for future in futures]
            assert sorted(result[0] for result in results) == [200, 409], results
            assert get_board(employee_client)['accounts'][0]['published'] == 1
            with connect() as db:
                assert db.execute("SELECT count(*) FROM operations_history WHERE entity LIKE 'daily:%'").fetchone()[0] == 1
            # Observe a real PG lock wait before revoking the waiting user's session.
            board = get_board(employee_client)
            body = dict(id=account, date=day, published=False, _expected_versions={key: board['record_versions'][key], 'daily:' + account + ':' + day: board['record_versions']['daily:' + account + ':' + day]})
            with connect_synthetic() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                with holder.transaction():
                    holder.execute('SELECT * FROM board_candidate.runtime_control WHERE id=1 FOR UPDATE')
                    future = executor.submit(post, employee_client, body)
                    deadline = time.monotonic() + 4
                    while time.monotonic() < deadline:
                        holder.execute('SELECT pg_stat_clear_snapshot()')
                        if holder.execute("SELECT 1 FROM pg_stat_activity WHERE application_name='synthetic-http-check' AND wait_event_type='Lock'").fetchone():
                            break
                        time.sleep(0.02)
                    else:
                        raise AssertionError('HTTP writer did not enter database lock wait')
                    holder.execute('UPDATE board_candidate.auth_users SET active=0 WHERE id=%s', (employee,))
                    holder.execute('DELETE FROM board_candidate.auth_sessions WHERE user_id=%s', (employee,))
                result = future.result(timeout=10)
                assert result[0] == 401, result
            assert get_board(owner_client)['accounts'][0]['published'] == 1
            # Each fence rejects the actual HTTP request before business mutation.
            for assignment, code, status in [
                ("mode='maintenance'", 'MAINTENANCE', 503),
                ("mode='open',writer_epoch=2", 'WRITER_VERSION_RETIRED', 409),
                ("writer_epoch=1,restore_generation='synthetic-http-2'", 'RESTORE_GENERATION_EXPIRED', 409),
            ]:
                with connect_synthetic() as admin:
                    admin.execute('UPDATE board_candidate.runtime_control SET ' + assignment)
                result = post(owner_client, body)
                assert result[0] == status and result[1]['code'] == code, result
            with connect_synthetic() as admin:
                admin.execute("UPDATE board_candidate.runtime_control SET restore_generation='synthetic-http-1'")
            def get_json(path):
                try:
                    with urllib.request.urlopen(base + path, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            ready = get_json('/api/ready')
            assert ready == (200, {'ready': True}), ready
            assert get_json('/api/health')[0] == 200
            with connect_synthetic() as admin:
                admin.execute("UPDATE board_candidate.runtime_control SET mode='maintenance'")
            held = get_json('/api/ready')
            assert held == (503, {'ready': False, 'code': 'NOT_READY'}), held
            assert get_json('/api/health')[0] == 200
            with connect_synthetic() as admin:
                admin.execute("UPDATE board_candidate.runtime_control SET mode='open'")
            # A real lock timeout must yield a stable, non-SQL diagnostic and no mutation.
            with connect_synthetic() as holder, concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                with holder.transaction():
                    holder.execute('SELECT * FROM board_candidate.runtime_control WHERE id=1 FOR UPDATE')
                    result = executor.submit(post, owner_client, body).result(timeout=10)
                    assert result[0] == 503 and result[1]['code'] == 'DB_BUSY', result
                    assert 'SELECT' not in json.dumps(result[1])
            assert get_board(owner_client)['accounts'][0]['published'] == 1
            import psycopg
            diagnostic = 'connection to 127.0.0.1:1 failed: SELECT password_hash FROM auth_users'
            @contextmanager
            def unreachable():
                raise psycopg.OperationalError(diagnostic)
                yield
            unreachable_id = 'c3c3c3c3-c3c3-4c3c-8c3c-c3c3c3c3c3c3'
            with patch.object(app, 'connect', unreachable):
                refused = post(owner_client, dict(body, request_id=unreachable_id))
                rejected = post(owner_client, dict(body, request_id='not-a-uuid'))
                down = get_json('/api/ready')
                assert get_json('/api/health')[0] == 200
            assert refused[0] == 503 and refused[1]['code'] == 'DB_UNAVAILABLE' and refused[1]['request_id'] == unreachable_id, refused
            assert rejected[0] == 503 and rejected[1]['code'] == 'DB_UNAVAILABLE' and 'request_id' not in rejected[1], rejected
            assert down[0] == 503 and down[1]['code'] == 'DB_UNAVAILABLE', down
            leaked = json.dumps([refused[1], rejected[1], down[1]], ensure_ascii=False)
            assert 'SELECT' not in leaked and 'password_hash' not in leaked and '127.0.0.1:1' not in leaked
            assert get_board(owner_client)['accounts'][0]['published'] == 1
            import subprocess
            import workspace_service
            deadlines = []
            def expire_worker(command, *args, **kwargs):
                if not any(str(part).endswith('workspace-export.mjs') for part in command):
                    raise AssertionError('unexpected export worker')
                deadlines.append(kwargs.get('timeout'))
                raise subprocess.TimeoutExpired(command, kwargs.get('timeout'))
            def post_export(payload):
                request = urllib.request.Request(base + '/api/workspace/export', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with owner_client.open(request, timeout=15) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            export_id = 'd4d4d4d4-d4d4-4d4d-8d4d-d4d4d4d4d4d4'
            csv_id = 'd5d5d5d5-d5d5-4d5d-8d5d-d5d5d5d5d5d5'
            with connect() as db:
                audits_before = db.execute("SELECT count(*) FROM auth_audit WHERE action='导出业务资料'").fetchone()[0]
            with patch.object(workspace_service, 'EXPORT_WORKER_TIMEOUT_SECONDS', 1), patch.object(workspace_service.subprocess, 'run', expire_worker):
                timed = post_export(dict(kind='accounts', format='xlsx', request_id=export_id))
                invalid = post_export(dict(kind='accounts', format='xlsx', request_id='not-a-uuid'))
            assert deadlines == [1, 1], deadlines
            assert timed[0] == 503 and timed[1]['code'] == 'EXPORT_TIMEOUT' and timed[1]['request_id'] == export_id, timed
            assert invalid[0] == 503 and invalid[1]['code'] == 'EXPORT_TIMEOUT' and 'request_id' not in invalid[1], invalid
            for payload in (timed[1], invalid[1]):
                assert 'content' not in payload and 'filename' not in payload and 'mime' not in payload
            leaked = json.dumps([timed[1], invalid[1]], ensure_ascii=False)
            assert 'workspace-export' not in leaked and 'SELECT' not in leaked and 'TimeoutExpired' not in leaked
            csv_result = post_export(dict(kind='accounts', format='csv', request_id=csv_id))
            assert csv_result[0] == 200 and csv_result[1].get('content') and 'request_id' not in csv_result[1], csv_result
            with connect() as db:
                assert db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (export_id,)).fetchone()[0] == 0
                assert db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (csv_id,)).fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM auth_audit WHERE action='导出业务资料'").fetchone()[0] == audits_before + 1
            assert get_board(owner_client)['accounts'][0]['published'] == 1
            import pg_write_protocol
            statement_id = 'd7d7d7d7-d7d7-4d7d-8d7d-d7d7d7d7d7d7'
            daily_key = 'daily:' + account + ':' + day
            board = get_board(owner_client)
            statement_body = dict(id=account, date=day, published=False, request_id=statement_id, _expected_versions={
                key: board['record_versions'][key], daily_key: board['record_versions'].get(daily_key)})
            original_execute = ServiceConnection.execute
            def timeout_daily(self, query, parameters=None):
                if isinstance(query, str) and 'INSERT INTO daily' in query:
                    original_execute(self, 'SELECT pg_sleep(2)')
                return original_execute(self, query, parameters)
            real_acquire = pg_write_protocol.acquire_control
            def short_statement(connection, **kwargs):
                real_acquire(connection, **kwargs)
                connection.execute("SET LOCAL statement_timeout = '500ms'")
            with patch.object(pg_write_protocol, 'acquire_control', short_statement), patch.object(ServiceConnection, 'execute', timeout_daily):
                statement = post(owner_client, statement_body)
            assert statement[0] == 503 and statement[1]['code'] == 'DB_TIMEOUT' and statement[1]['request_id'] == statement_id, statement
            statement_text = json.dumps(statement[1], ensure_ascii=False)
            assert 'pg_sleep' not in statement_text and 'canceling' not in statement_text and 'SELECT' not in statement_text
            assert get_board(owner_client)['accounts'][0]['published'] == 1
            with connect() as db:
                pending = db.execute('SELECT state FROM idempotency_records WHERE request_id=?', (statement_id,)).fetchone()
                assert pending and pending[0] == 'pending', pending
                assert int(db.execute('SELECT published FROM daily WHERE account_id=? AND day=?', (account, day)).fetchone()[0]) == 1
            from pathlib import Path
            disk_id = 'd8d8d8d8-d8d8-4d8d-8d8d-d8d8d8d8d8d8'
            worker_id = 'd9d9d9d9-d9d9-4d9d-8d9d-d9d9d9d9d9d9'
            original_write_text = Path.write_text
            def fail_input(self, *args, **kwargs):
                if self.name == 'input.json':
                    raise OSError(1, 'SELECT secret-disk-path', 'E:\\secret-disk-path')
                return original_write_text(self, *args, **kwargs)
            def fail_worker(command, *args, **kwargs):
                if not any(str(part).endswith('workspace-export.mjs') for part in command):
                    raise AssertionError('unexpected export worker')
                return subprocess.CompletedProcess(command, 1, stderr=b'SELECT secret-worker-output')
            with connect() as db:
                file_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='导出业务资料'").fetchone()[0]
            with patch.object(Path, 'write_text', fail_input):
                disk = post_export(dict(kind='accounts', format='xlsx', request_id=disk_id))
            with patch.object(workspace_service.subprocess, 'run', fail_worker):
                failed = post_export(dict(kind='accounts', format='xlsx', request_id=worker_id))
                omitted = post_export(dict(kind='accounts', format='xlsx', request_id='not-a-uuid'))
            assert disk[0] == 503 and disk[1]['code'] == 'FILE_FAILED' and disk[1]['request_id'] == disk_id, disk
            assert failed[0] == 503 and failed[1]['code'] == 'FILE_FAILED' and failed[1]['request_id'] == worker_id, failed
            assert omitted[0] == 503 and omitted[1]['code'] == 'FILE_FAILED' and 'request_id' not in omitted[1], omitted
            for payload in (disk[1], failed[1], omitted[1]):
                assert 'content' not in payload and 'filename' not in payload and 'mime' not in payload
            file_text = json.dumps([disk[1], failed[1], omitted[1]], ensure_ascii=False)
            assert 'SELECT' not in file_text and 'secret-disk-path' not in file_text and 'secret-worker-output' not in file_text and 'input.json' not in file_text and 'workspace-export' not in file_text
            with connect() as db:
                assert db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (disk_id,)).fetchone()[0] == 0
                assert db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (worker_id,)).fetchone()[0] == 0
                assert db.execute("SELECT count(*) FROM auth_audit WHERE action='导出业务资料'").fetchone()[0] == file_audits
            assert get_board(owner_client)['accounts'][0]['published'] == 1
            # The same begin_read helper used by /api/board holds a coherent snapshot.
            from sql_dialect import begin_read
            with connect() as reader, connect_synthetic() as writer:
                begin_read(reader)
                original = reader.execute('SELECT note FROM accounts WHERE id=?', (account,)).fetchone()[0]
                writer.execute("UPDATE board_candidate.accounts SET note='synthetic-new-note' WHERE id=%s", (account,))
                assert reader.execute('SELECT note FROM accounts WHERE id=?', (account,)).fetchone()[0] == original
                reader.commit()
                assert reader.execute('SELECT note FROM accounts WHERE id=?', (account,)).fetchone()[0] == 'synthetic-new-note'
            password_id = 'e3e3e3e3-e3e3-4e3e-8e3e-e3e3e3e3e3e3'
            new_password = 'synthetic-new-password'
            with connect() as db:
                owner_id = db.execute("SELECT id FROM auth_users WHERE username='test_owner'").fetchone()[0]
                previous_session = db.execute('SELECT token_hash FROM auth_sessions WHERE user_id=?', (owner_id,)).fetchone()[0]
            def post_password(payload):
                request = urllib.request.Request(base + '/api/auth/password', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with owner_client.open(request, timeout=15) as response:
                        return response.status, json.load(response), response.headers.get('Set-Cookie') or ''
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error), error.headers.get('Set-Cookie') or ''
            def session_cookie_flags(header):
                parts = [part.strip() for part in header.split(';')]
                name = parts[0].split('=', 1)[0] if parts else ''
                return (name == 'board_session', 'HttpOnly' in parts, 'SameSite=Strict' in parts, 'Path=/' in parts, any(part.startswith('Max-Age=') for part in parts), 'Secure' not in parts)
            changed = post_password(dict(old_password=test_auth_support.TEST_PASSWORD, password=new_password, request_id=password_id))
            assert changed[0] == 200 and changed[1] == {'ok': True}, changed[1]
            assert changed[2].startswith('board_session=') and new_password not in changed[2]
            assert session_cookie_flags(changed[2]) == (True, True, True, True, True, True), session_cookie_flags(changed[2])
            replay = post_password(dict(old_password=test_auth_support.TEST_PASSWORD, password=new_password, request_id=password_id))
            assert replay[0] == 403 and replay[1].get('code') != 'REQUEST_SUCCEEDED', replay[1]
            assert new_password not in json.dumps(replay[1], ensure_ascii=False)
            with connect() as db:
                assert db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (password_id,)).fetchone()[0] == 0
                assert db.execute('SELECT token_hash FROM auth_sessions WHERE user_id=?', (owner_id,)).fetchone()[0] != previous_session
                audit_rows = db.execute("SELECT action, COALESCE(target,'') FROM auth_audit WHERE action='修改本人密码'").fetchall()
                audit_text = '\n'.join((row[0] or '') + (row[1] or '') for row in audit_rows)
                audit_leaked = (test_auth_support.TEST_PASSWORD in audit_text) or (new_password in audit_text)
                assert len(audit_rows) == 1 and audit_leaked is False, len(audit_rows)
                raw_session = changed[2].split(';', 1)[0].split('=', 1)[1]
                session_rows = db.execute('SELECT token_hash, csrf FROM auth_sessions WHERE user_id=?', (owner_id,)).fetchall()
                session_matched = any((row[0] or '') == auth.digest(raw_session) for row in session_rows)
                session_leaked = any((row[0] or '') == raw_session or (row[1] or '') == raw_session for row in session_rows)
                assert len(session_rows) == 1 and session_matched is True and session_leaked is False, len(session_rows)
            with connect() as db:
                prior_expiry = db.execute('SELECT expires_at FROM auth_sessions WHERE user_id=?', (owner_id,)).fetchone()[0]
                db.execute('UPDATE auth_sessions SET expires_at=? WHERE user_id=?', (1, owner_id))
            expired_status = 200
            try:
                try:
                    get_board(owner_client)
                except urllib.error.HTTPError as error:
                    expired_status = error.code
                    error.read()
                assert expired_status == 401, expired_status
            finally:
                with connect() as db:
                    db.execute('UPDATE auth_sessions SET expires_at=? WHERE user_id=?', (prior_expiry, owner_id))
                    restored = db.execute('SELECT count(*) FROM auth_sessions WHERE user_id=? AND expires_at=?', (owner_id, prior_expiry)).fetchone()[0]
            assert restored == 1, restored
            assert get_board(owner_client)['accounts'][0]['published'] == 1
            marker = 'rejected-secret-marker'
            guard_id = '6c6c6c6c-6c6c-46c6-86c6-6c6c6c6c6c6c'
            with connect() as db:
                actor = {'id': owner_id}
                reserved = idempotency.claim(db, actor, '/api/account/create', {'name': 'guard', 'request_id': guard_id})
                assert reserved[0] == 'reserved', reserved[0]
                for key in ('password', 'password_hash', 'csrf', 'token', 'cookie', 'content', 'image', 'bytes'):
                    rejected = False
                    try:
                        idempotency.complete(db, actor, {'request_id': guard_id}, 'succeeded', 200, {'ok': True, key: marker})
                    except ValueError as error:
                        rejected = '凭据或图片' in str(error)
                    assert rejected is True, key
                guard_row = db.execute('SELECT state, COALESCE(result_json, \'\') FROM idempotency_records WHERE request_id=?', (guard_id,)).fetchone()
                guard_leaked = marker in (guard_row[1] or '')
                assert guard_row[0] == 'pending' and guard_leaked is False, guard_row[0]
                db.execute('DELETE FROM idempotency_records WHERE request_id=?', (guard_id,))
            def post_login(username, secret, forwarded=None):
                headers = {'Content-Type': 'application/json'}
                if forwarded:
                    headers['X-Forwarded-For'] = forwarded
                request = urllib.request.Request(base + '/api/auth/login', data=json.dumps(dict(username=username, password=secret)).encode(), headers=headers)
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        return response.status, json.load(response), response.headers.get('Set-Cookie') or ''
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error), ''
            def post_sized(path, declared, payload=b'{}'):
                request = urllib.request.Request(base + path, data=payload, method='POST', headers={'Content-Type': 'application/json', 'Content-Length': str(declared)})
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            ordinary = post_sized('/api/daily', 100001, b'{' + b' ' * 99999 + b'}')
            assert ordinary[0] == 400 and ordinary[1].get('error') == '请求过大或为空'
            empty = post_sized('/api/daily', 0, b'')
            assert empty[0] == 400 and empty[1].get('error') == '请求过大或为空'
            upload = post_sized('/api/workspace/nurture/upload', 12 * 1024 * 1024 + 1)
            assert upload[0] == 400 and upload[1].get('error') == '请求过大或为空'
            def post_origin(origin):
                headers = {'Content-Type': 'application/json'}
                if origin is not None:
                    headers['Origin'] = origin
                request = urllib.request.Request(base + '/api/daily', data=b'{}', headers=headers)
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            foreign = post_origin('https://evil.example')
            assert foreign[0] == 403 and foreign[1].get('error') == '请求来源不正确'
            same = post_origin(base)
            assert same[1].get('error') != '请求来源不正确'
            omitted = post_origin(None)
            assert omitted[1].get('error') != '请求来源不正确'
            assert get_board(owner_client)['accounts'][0]['published'] == 1
            def wait_socket(send=None):
                held = socket.create_connection(('127.0.0.1', server.server_port), timeout=app.REQUEST_IO_TIMEOUT + 6)
                held.settimeout(app.REQUEST_IO_TIMEOUT + 6)
                if send is not None:
                    held.sendall(send)
                opened = time.monotonic()
                chunks = []
                try:
                    while True:
                        piece = held.recv(4096)
                        if not piece:
                            break
                        chunks.append(piece)
                except OSError:
                    pass
                held.close()
                return time.monotonic() - opened, b''.join(chunks)
            silent_elapsed, silent = wait_socket()
            assert silent == b'' and app.REQUEST_IO_TIMEOUT - 1 <= silent_elapsed < app.REQUEST_IO_TIMEOUT + 6
            body_header = b'POST /api/daily HTTP/1.1\r\nHost: 127.0.0.1:' + str(server.server_port).encode() + b'\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n'
            stalled_elapsed, stalled = wait_socket(body_header)
            stalled_text = stalled.decode('utf-8', errors='replace')
            assert ' 408 ' in stalled_text and 'READ_TIMEOUT' in stalled_text and '请求读取超时' in stalled_text
            assert 'password' not in stalled_text.lower() and app.REQUEST_IO_TIMEOUT - 1 <= stalled_elapsed < app.REQUEST_IO_TIMEOUT + 6
            assert get_board(owner_client)['accounts'][0]['published'] == 1
            with urllib.request.urlopen(base + '/api/health', timeout=10) as health:
                assert health.status == 200
            holder_errors = []
            holder_ready = []
            holder_lock = threading.Lock()
            holders_sent = threading.Event()
            def hold_slot():
                try:
                    held = socket.create_connection(('127.0.0.1', server.server_port), timeout=5)
                    held.settimeout(app.REQUEST_IO_TIMEOUT + 6)
                    held.sendall(body_header)
                    with holder_lock:
                        holder_ready.append(held)
                        if len(holder_ready) == app.REQUEST_CONCURRENCY_LIMIT:
                            holders_sent.set()
                    chunks = []
                    try:
                        while True:
                            piece = held.recv(4096)
                            if not piece:
                                break
                            chunks.append(piece)
                    except OSError:
                        pass
                    finally:
                        held.close()
                    if b'READ_TIMEOUT' not in b''.join(chunks):
                        holder_errors.append('holder')
                except OSError:
                    holder_errors.append('connect')
            holders = [threading.Thread(target=hold_slot) for _ in range(app.REQUEST_CONCURRENCY_LIMIT)]
            for holder in holders:
                holder.start()
            assert holders_sent.wait(5) and not holder_errors
            rejected = False
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and not rejected:
                request = urllib.request.Request(base + '/api/daily', data=b'{}', headers={'Content-Type': 'application/json'})
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        status, payload = response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    status, payload = error.code, json.load(error)
                rejected = status == 503 and payload.get('code') == 'CONCURRENCY_LIMIT' and 'password' not in json.dumps(payload, ensure_ascii=False).lower()
            for holder in holders:
                holder.join(app.REQUEST_IO_TIMEOUT + 6)
            assert rejected and not holder_errors and not any(holder.is_alive() for holder in holders)
            assert get_board(owner_client)['accounts'][0]['published'] == 1
            with urllib.request.urlopen(base + '/api/health', timeout=10) as health:
                assert health.status == 200
            before_batch = get_board(owner_client)
            batch_target = next(row for row in before_batch['accounts'] if row['id'] == account)
            batch_key = 'account:' + account
            with connect() as db:
                batch_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', (batch_key,)).fetchone()[0]
                batch_audit = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
            current_owner = test_auth_support.client(base, 'test_owner', new_password)
            def post_batch(payload):
                request = urllib.request.Request(base + '/api/batch-edit', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with current_owner.open(request, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            missing_batch = dict(kind='accounts', ids=[account, 'missing-synthetic'], patch={'status': '停用'}, request_id='a6a6a6a6-a6a6-4a6a-8a6a-a6a6a6a6a6a6', _expected_versions={batch_key: before_batch['record_versions'][batch_key], 'account:missing-synthetic': None})
            missing_row = post_batch(missing_batch)
            assert missing_row[0] == 400 and '已不存在' in str(missing_row[1].get('error', '')), missing_row[0]
            assert post_batch(missing_batch) == missing_row
            after_batch = get_board(owner_client)
            assert next(row for row in after_batch['accounts'] if row['id'] == account)['status'] == batch_target['status']
            assert after_batch['record_versions'][batch_key] == before_batch['record_versions'][batch_key]
            with connect() as db:
                assert db.execute('SELECT count(*) FROM operations_history WHERE entity=?', (batch_key,)).fetchone()[0] == batch_history
                assert db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0] == batch_audit
            nurture_key = 'nurture:' + account
            with connect() as db:
                nurture_rows = db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?', (account,)).fetchone()[0]
                nurture_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', (nurture_key,)).fetchone()[0]
            assert nurture_rows == 0
            invalid_verification = dict(kind='verification', ids=[account], patch={'verification_state': '不是核对'}, request_id='b7b7b7b7-b7b7-4b7b-8b7b-b7b7b7b7b7b7', _expected_versions={batch_key: after_batch['record_versions'][batch_key], nurture_key: None})
            invalid_row = post_batch(invalid_verification)
            assert invalid_row[0] == 400 and '核对情况不正确' in str(invalid_row[1].get('error', '')), invalid_row[0]
            assert post_batch(invalid_verification) == invalid_row
            verified = get_board(owner_client)
            verified_account = next(row for row in verified['accounts'] if row['id'] == account)
            assert verified_account['status'] == batch_target['status']
            assert verified['record_versions'][batch_key] == after_batch['record_versions'][batch_key]
            with connect() as db:
                assert db.execute('SELECT count(*) FROM nurture_records WHERE account_id=?', (account,)).fetchone()[0] == 0
                assert db.execute('SELECT count(*) FROM operations_history WHERE entity=?', (nurture_key,)).fetchone()[0] == nurture_history
                assert db.execute('SELECT count(*) FROM operations_history WHERE entity=?', (batch_key,)).fetchone()[0] == batch_history
                assert db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0] == batch_audit
            status_then_invalid = dict(kind='accounts', ids=[account], patch={'status': '停用', 'password_state': '不是状态'}, request_id='f8f8f8f8-f8f8-4f8f-8f8f-f8f8f8f8f8f8', _expected_versions={batch_key: verified['record_versions'][batch_key]})
            status_row = post_batch(status_then_invalid)
            assert status_row[0] == 400 and '账号稳定或改密状态不正确' in str(status_row[1].get('error', '')), status_row[0]
            assert post_batch(status_then_invalid) == status_row
            kept = get_board(owner_client)
            kept_account = next(row for row in kept['accounts'] if row['id'] == account)
            assert kept_account['status'] == verified_account['status']
            assert kept_account['password_state'] == verified_account['password_state']
            assert kept_account['card_stability'] == verified_account['card_stability']
            assert kept['record_versions'][batch_key] == verified['record_versions'][batch_key]
            with connect() as db:
                assert db.execute('SELECT count(*) FROM operations_history WHERE entity=?', (batch_key,)).fetchone()[0] == batch_history
                assert db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0] == batch_audit
            with connect() as db:
                good_card = db.execute("INSERT INTO phone_cards(phone,return_state,returned_on) VALUES('13800001001','待确认','') RETURNING id").fetchone()[0]
                bad_card = db.execute("INSERT INTO phone_cards(phone,return_state,returned_on) VALUES('13800001002','已归还','') RETURNING id").fetchone()[0]
            good_key, bad_key = 'card:' + str(good_card), 'card:' + str(bad_card)
            card_board = get_board(current_owner)
            with connect() as db:
                card_history = {key: db.execute('SELECT count(*) FROM operations_history WHERE entity=?', (key,)).fetchone()[0] for key in (good_key, bad_key)}
                card_audit = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
            second_fails = dict(kind='cards', ids=[str(good_card), str(bad_card)], patch={'owner': '合成负责人'}, request_id='4c4c4c4c-4c4c-44c4-84c4-4c4c4c4c4c4c', _expected_versions={good_key: card_board['record_versions'][good_key], bad_key: card_board['record_versions'][bad_key]})
            second_row = post_batch(second_fails)
            assert second_row[0] == 400 and '实际归还日期' in str(second_row[1].get('error', '')), second_row[0]
            assert post_batch(second_fails) == second_row
            with connect() as db:
                kept_cards = {row[0]: (row[1], row[2], row[3]) for row in db.execute('SELECT id,owner,return_state,returned_on FROM phone_cards WHERE id IN (?,?)', (good_card, bad_card))}
                assert kept_cards[good_card] == ('', '待确认', '') and kept_cards[bad_card] == ('', '已归还', '')
                assert db.execute('SELECT count(*) FROM operations_history WHERE entity=?', (good_key,)).fetchone()[0] == card_history[good_key]
                assert db.execute('SELECT count(*) FROM operations_history WHERE entity=?', (bad_key,)).fetchone()[0] == card_history[bad_key]
                assert db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0] == card_audit
            for _ in range(5):
                failed = post_login('synthetic_employee', 'invalid-synthetic')
                assert failed[0] == 401 and failed[1].get('error') == '登录名或密码不正确，或账号已停用'
            locked = post_login('synthetic_employee', 'invalid-synthetic', '203.0.113.9')
            assert locked[0] == 429 and locked[1].get('error') == '登录尝试过多，请15分钟后再试'
            peer = post_login('synthetic_peer', test_auth_support.TEST_PASSWORD, '198.51.100.8')
            assert peer[0] == 200 and peer[1] == {'ok': True} and str(peer[2]).startswith('board_session=')
            assert session_cookie_flags(peer[2]) == (True, True, True, True, True, True), session_cookie_flags(peer[2])
            with connect() as db:
                login_rows = db.execute("SELECT action, COALESCE(target,'') FROM auth_audit WHERE action='登录'").fetchall()
                limit_rows = db.execute('SELECT key FROM auth_limits').fetchall()
                stored = '\n'.join((row[0] or '') + (row[1] or '') for row in login_rows) + '\n' + '\n'.join((row[0] or '') for row in limit_rows)
                login_leaked = (test_auth_support.TEST_PASSWORD in stored) or (new_password in stored) or ('invalid-synthetic' in stored)
                assert len(login_rows) >= 1 and len(limit_rows) >= 1 and login_leaked is False, (len(login_rows), len(limit_rows))
            peer_client = test_auth_support.client(base, 'synthetic_peer')
            with connect() as db:
                peer_id = db.execute("SELECT id FROM auth_users WHERE username='synthetic_peer'").fetchone()[0]
                sessions_before = db.execute('SELECT count(*) FROM auth_sessions WHERE user_id=?', (peer_id,)).fetchone()[0]
                logout_audits_before = db.execute("SELECT count(*) FROM auth_audit WHERE action='退出登录'").fetchone()[0]
            wrong_logout = urllib.request.Request(base + '/api/auth/logout', data=b'{}', headers={'Content-Type': 'application/json', 'X-CSRF-Token': 'wrong-csrf'})
            try:
                with peer_client.open(wrong_logout, timeout=10) as response:
                    csrf_status, csrf_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                csrf_status, csrf_body = error.code, json.load(error)
            assert csrf_status == 403 and csrf_body.get('error') == '页面已过期，请刷新后重试', csrf_status
            with connect() as db:
                sessions_after = db.execute('SELECT count(*) FROM auth_sessions WHERE user_id=?', (peer_id,)).fetchone()[0]
                logout_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='退出登录'").fetchone()[0]
            assert sessions_before >= 1 and sessions_after == sessions_before and logout_audits_after == logout_audits_before, (sessions_before, sessions_after)
            peer_cookie = next((item.value for handler in peer_client.handlers for item in (getattr(handler, 'cookiejar', None) or []) if item.name == 'board_session' and item.value), '')
            peer_csrf = next((value for name, value in peer_client.addheaders if name == 'X-CSRF-Token'), '')
            logout_request = urllib.request.Request(base + '/api/auth/logout', data=b'{}', headers={'Content-Type': 'application/json'})
            with peer_client.open(logout_request, timeout=10) as response:
                logout_status, logout_body, logout_cookie = response.status, json.load(response), response.headers.get('Set-Cookie') or ''
            assert logout_status == 200 and logout_body.get('ok') is True, logout_status
            logout_parts = [part.strip() for part in logout_cookie.split(';')]
            logout_name, _, logout_value = (logout_parts[0] if logout_parts else '').partition('=')
            logout_cleared = (logout_name == 'board_session', logout_value == '', 'Max-Age=0' in logout_parts, 'Secure' not in logout_parts)
            assert logout_cleared == (True, True, True, True), logout_cleared
            with connect() as db:
                logout_rows = db.execute("SELECT action, COALESCE(target,''), COALESCE(actor_name,'') FROM auth_audit WHERE action='退出登录'").fetchall()
            logout_text = '\n'.join((row[0] or '') + (row[1] or '') + (row[2] or '') for row in logout_rows)
            logout_leaked = (bool(peer_cookie) and peer_cookie in logout_text) or (bool(peer_csrf) and peer_csrf in logout_text) or (test_auth_support.TEST_PASSWORD in logout_text) or (new_password in logout_text) or ('invalid-synthetic' in logout_text)
            logout_empty = all(row[0] == '退出登录' and row[1] == '' for row in logout_rows)
            assert len(logout_rows) == logout_audits_before + 1 and logout_empty is True and logout_leaked is False and bool(peer_cookie) and bool(peer_csrf), len(logout_rows)
            published_before = get_board(current_owner)['accounts'][0]['published']
            wrong_daily = urllib.request.Request(base + '/api/daily', data=b'{}', headers={'Content-Type': 'application/json', 'X-CSRF-Token': 'wrong-csrf'})
            try:
                with current_owner.open(wrong_daily, timeout=10) as response:
                    daily_status, daily_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                daily_status, daily_body = error.code, json.load(error)
            assert daily_status == 403 and daily_body.get('error') == '页面已过期，请刷新后重试', daily_status
            assert get_board(current_owner)['accounts'][0]['published'] == published_before
            with connect() as db:
                owner_sessions = db.execute("SELECT count(*) FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id WHERE u.username='test_owner'").fetchone()[0]
                db.execute("UPDATE auth_users SET active=0 WHERE username='test_owner'")
            inactive_status = 200
            try:
                try:
                    get_board(current_owner)
                except urllib.error.HTTPError as error:
                    inactive_status = error.code
                    error.read()
                assert inactive_status == 401, inactive_status
                with connect() as db:
                    still_there = db.execute("SELECT count(*) FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id WHERE u.username='test_owner'").fetchone()[0]
                assert owner_sessions >= 1 and still_there == owner_sessions, (owner_sessions, still_there)
            finally:
                with connect() as db:
                    db.execute("UPDATE auth_users SET active=1 WHERE username='test_owner'")
            assert get_board(current_owner)['accounts'][0]['published'] == published_before
            with connect() as db:
                credential_sessions = db.execute("SELECT count(*) FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id WHERE u.username='test_owner'").fetchone()[0]
                credential_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='修改本人密码'").fetchone()[0]
                credential_hash = db.execute("SELECT password_hash FROM auth_users WHERE username='test_owner'").fetchone()[0]
            rejected_password = urllib.request.Request(base + '/api/auth/password', data=json.dumps(dict(old_password='not-the-old-password', password='synthetic-rejected-password')).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(rejected_password, timeout=10) as response:
                    rejected_status, rejected_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                rejected_status, rejected_body = error.code, json.load(error)
            assert rejected_status == 400 and rejected_body.get('error') == '原密码不正确', rejected_status
            rejected_text = json.dumps(rejected_body, ensure_ascii=False)
            rejected_leaked = ('not-the-old-password' in rejected_text) or ('synthetic-rejected-password' in rejected_text)
            assert rejected_leaked is False
            with connect() as db:
                credential_hash_after = db.execute("SELECT password_hash FROM auth_users WHERE username='test_owner'").fetchone()[0]
                credential_sessions_after = db.execute("SELECT count(*) FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id WHERE u.username='test_owner'").fetchone()[0]
                credential_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='修改本人密码'").fetchone()[0]
            credential_kept = credential_hash_after == credential_hash
            assert credential_kept is True and credential_sessions_after == credential_sessions and credential_audits_after == credential_audits, (credential_sessions, credential_sessions_after)
            assert get_board(current_owner)['accounts'][0]['published'] == published_before
            for candidate in ('short', 'x' * 129):
                short_password = urllib.request.Request(base + '/api/auth/password', data=json.dumps(dict(old_password=new_password, password=candidate)).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with current_owner.open(short_password, timeout=10) as response:
                        short_status, short_body = response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    short_status, short_body = error.code, json.load(error)
                assert short_status == 400 and short_body.get('error') == '密码请设置为10至128位', short_status
                short_text = json.dumps(short_body, ensure_ascii=False)
                short_leaked = (candidate in short_text) or (new_password in short_text)
                assert short_leaked is False
            with connect() as db:
                length_hash = db.execute("SELECT password_hash FROM auth_users WHERE username='test_owner'").fetchone()[0]
                length_sessions = db.execute("SELECT count(*) FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id WHERE u.username='test_owner'").fetchone()[0]
                length_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='修改本人密码'").fetchone()[0]
            length_kept = length_hash == credential_hash
            assert length_kept is True and length_sessions == credential_sessions and length_audits == credential_audits, (credential_sessions, length_sessions)
            assert get_board(current_owner)['accounts'][0]['published'] == published_before
            with connect() as db:
                daily_count = db.execute('SELECT count(*) FROM daily').fetchone()[0]
            form_request = urllib.request.Request(base + '/api/daily', data=b'published=true&marker=form-body-marker', headers={'Content-Type': 'application/x-www-form-urlencoded'})
            try:
                with current_owner.open(form_request, timeout=10) as response:
                    form_status, form_payload = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                form_status, form_payload = error.code, json.load(error)
            form_leaked = 'form-body-marker' in json.dumps(form_payload, ensure_ascii=False)
            assert form_status == 400 and form_payload.get('error') == '需要 JSON 请求' and form_leaked is False, form_status
            charset_request = urllib.request.Request(base + '/api/daily', data=b'{}', headers={'Content-Type': 'application/json; charset=utf-8'})
            try:
                with current_owner.open(charset_request, timeout=10) as response:
                    charset_status, charset_payload = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                charset_status, charset_payload = error.code, json.load(error)
            assert charset_status == 400 and charset_payload.get('code') == 'REQUEST_ID_REQUIRED' and charset_payload.get('error') != '需要 JSON 请求', charset_status
            with connect() as db:
                daily_after = db.execute('SELECT count(*) FROM daily').fetchone()[0]
            assert daily_after == daily_count, (daily_count, daily_after)
            assert get_board(current_owner)['accounts'][0]['published'] == published_before
            stored_id = 'a6a6a6a6-a6a6-4a6a-8a6a-a6a6a6a6a6a6'
            def lookup_result(client, request_id):
                request = urllib.request.Request(base + '/api/idempotency/result?request_id=' + request_id)
                try:
                    with client.open(request, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            with connect() as db:
                stored_rows = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (stored_id,)).fetchone()[0]
            owner_lookup = lookup_result(current_owner, stored_id)
            owner_kept = owner_lookup[0] == 400 and owner_lookup[1].get('code') == 'REQUEST_FAILED' and '已不存在' in str(owner_lookup[1].get('error', ''))
            assert owner_kept is True, owner_lookup[0]
            other_client = test_auth_support.client(base, 'synthetic_peer')
            other_lookup = lookup_result(other_client, stored_id)
            other_text = json.dumps(other_lookup[1], ensure_ascii=False)
            other_hidden = other_lookup[0] == 404 and other_lookup[1].get('code') == 'REQUEST_NOT_FOUND' and other_lookup[1].get('error') == '没有这条请求记录'
            other_leaked = '已不存在' in other_text
            assert other_hidden is True and other_leaked is False, other_lookup[0]
            again = lookup_result(current_owner, stored_id)
            assert again[0] == 400 and again[1].get('code') == 'REQUEST_FAILED', again[0]
            with connect() as db:
                stored_rows_after = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (stored_id,)).fetchone()[0]
            assert stored_rows == 1 and stored_rows_after == stored_rows, (stored_rows, stored_rows_after)
            lookup_id = 'c4c4c4c4-c4c4-4c4c-84c4-c4c4c4c4c4c4'
            hidden_marker = 'stored-result-marker'
            with connect() as db:
                peer_user_id = db.execute("SELECT id FROM auth_users WHERE username='synthetic_peer'").fetchone()[0]
                peer_permissions = db.execute("SELECT permissions FROM auth_users WHERE id=?", (peer_user_id,)).fetchone()[0]
                reserved = idempotency.claim(db, {'id': peer_user_id}, '/api/daily', {'published': True, 'request_id': lookup_id})
                assert reserved[0] == 'reserved', reserved[0]
                idempotency.complete(db, {'id': peer_user_id}, {'request_id': lookup_id}, 'failed', 400, {'error': hidden_marker})
                db.execute("UPDATE auth_users SET permissions=? WHERE id=?", (json.dumps(['account_status', 'account_progress', 'card_tasks']), peer_user_id))
            try:
                denied = lookup_result(other_client, lookup_id)
                denied_leaked = hidden_marker in json.dumps(denied[1], ensure_ascii=False)
                assert denied[0] == 403 and denied[1].get('error') == '当前账号没有此操作权限' and denied_leaked is False, denied[0]
            finally:
                with connect() as db:
                    db.execute('UPDATE auth_users SET permissions=? WHERE id=?', (peer_permissions, peer_user_id))
            restored_lookup = lookup_result(other_client, lookup_id)
            restored_seen = restored_lookup[0] == 400 and restored_lookup[1].get('code') == 'REQUEST_FAILED' and restored_lookup[1].get('error') == hidden_marker
            assert restored_seen is True, restored_lookup[0]
            pending_id = 'd5d5d5d5-d5d5-4d5d-8d5d-d5d5d5d5d5d5'
            with connect() as db:
                pending_reserved = idempotency.claim(db, {'id': peer_user_id}, '/api/daily', {'published': False, 'request_id': pending_id})
                daily_before_lookup = db.execute('SELECT count(*) FROM daily').fetchone()[0]
            assert pending_reserved[0] == 'reserved', pending_reserved[0]
            pending_lookup = lookup_result(other_client, pending_id)
            assert pending_lookup[0] == 409 and pending_lookup[1].get('code') == 'REQUEST_PENDING' and pending_lookup[1].get('error') == '结果待确认', pending_lookup[0]
            pending_again = lookup_result(other_client, pending_id)
            assert pending_again[0] == 409 and pending_again[1].get('code') == 'REQUEST_PENDING', pending_again[0]
            with connect() as db:
                pending_row = db.execute('SELECT state, result_json IS NULL FROM idempotency_records WHERE user_id=? AND request_id=?', (peer_user_id, pending_id)).fetchone()
                daily_after_lookup = db.execute('SELECT count(*) FROM daily').fetchone()[0]
            pending_kept = pending_row[0] == 'pending' and pending_row[1] is True
            assert pending_kept is True and daily_after_lookup == daily_before_lookup, pending_row[0]
            bare_lookup = urllib.request.Request(base + '/api/idempotency/result?request_id=' + stored_id)
            try:
                with urllib.request.urlopen(bare_lookup, timeout=10) as response:
                    bare_status, bare_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                bare_status, bare_body = error.code, json.load(error)
            bare_leaked = '已不存在' in json.dumps(bare_body, ensure_ascii=False)
            assert bare_status == 401 and bare_body.get('error') == '请先登录工作台' and bare_leaked is False, bare_status
            owner_still = lookup_result(current_owner, stored_id)
            assert owner_still[0] == 400 and owner_still[1].get('code') == 'REQUEST_FAILED', owner_still[0]
            with connect() as db:
                bare_rows = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (stored_id,)).fetchone()[0]
            assert bare_rows == 1, bare_rows
            with connect() as db:
                object_daily = db.execute('SELECT count(*) FROM daily').fetchone()[0]
            array_request = urllib.request.Request(base + '/api/daily', data=b'["array-body-marker"]', headers={'Content-Type': 'application/json'})
            broken_request = urllib.request.Request(base + '/api/daily', data=b'{"broken-body-marker":', headers={'Content-Type': 'application/json'})
            def open_json(request):
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            array_status, array_body = open_json(array_request)
            array_text = json.dumps(array_body, ensure_ascii=False)
            array_leaked = 'array-body-marker' in array_text
            assert array_status == 400 and array_body.get('error') == '请求内容应为对象' and array_leaked is False, array_status
            broken_status, broken_body = open_json(broken_request)
            broken_text = json.dumps(broken_body, ensure_ascii=False)
            broken_leaked = 'broken-body-marker' in broken_text
            assert broken_status == 400 and broken_body.get('error') != '需要 JSON 请求' and broken_leaked is False, broken_status
            with connect() as db:
                object_daily_after = db.execute('SELECT count(*) FROM daily').fetchone()[0]
            assert object_daily_after == object_daily, (object_daily, object_daily_after)
            assert get_board(current_owner)['accounts'][0]['published'] == published_before
            with connect() as db:
                task_card = db.execute("INSERT INTO phone_cards(phone,label,status) VALUES('13800002001','原标签','待确认') RETURNING id").fetchone()[0]
                task_before = db.execute('SELECT phone,label,status FROM phone_cards WHERE id=?', (task_card,)).fetchone()
                task_audits_before = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            refused_card = urllib.request.Request(base + '/api/phone-card', data=json.dumps(dict(id=task_card, phone='13800002002', label='基础资料标记', status='正常', request_id='e1e1e1e1-e1e1-4e1e-8e1e-e1e1e1e1e1e1')).encode(), headers={'Content-Type': 'application/json'})
            try:
                with other_client.open(refused_card, timeout=10) as response:
                    refused_card_status, refused_card_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                refused_card_status, refused_card_body = error.code, json.load(error)
            assert refused_card_status == 403 and refused_card_body.get('error') == '只能修改卡状态、接码及归还事项，不能修改基础资料', refused_card_status
            with connect() as db:
                task_refused = db.execute('SELECT phone,label,status FROM phone_cards WHERE id=?', (task_card,)).fetchone()
                task_audits_refused = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
                task_reused = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', ('e1e1e1e1-e1e1-4e1e-8e1e-e1e1e1e1e1e1',)).fetchone()[0]
            task_unchanged = task_refused[0] == task_before[0] and task_refused[1] == task_before[1] and task_refused[2] == task_before[2]
            assert task_unchanged is True and task_audits_refused == task_audits_before and task_reused == 0, refused_card_status
            allowed_card = urllib.request.Request(base + '/api/phone-card', data=json.dumps(dict(id=task_card, status='正常', request_id='e2e2e2e2-e2e2-4e2e-8e2e-e2e2e2e2e2e2')).encode(), headers={'Content-Type': 'application/json'})
            try:
                with other_client.open(allowed_card, timeout=10) as response:
                    allowed_card_status, allowed_card_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                allowed_card_status, allowed_card_body = error.code, json.load(error)
            assert allowed_card_status == 200 and allowed_card_body.get('ok') is True, allowed_card_status
            with connect() as db:
                task_after = db.execute('SELECT phone,label,status FROM phone_cards WHERE id=?', (task_card,)).fetchone()
                task_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
                task_saved = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', ('e2e2e2e2-e2e2-4e2e-8e2e-e2e2e2e2e2e2',)).fetchone()[0]
            task_only = task_after[0] == task_before[0] and task_after[1] == task_before[1] and task_after[2] == '正常'
            assert task_only is True and task_audits_after == task_audits_before + 1 and task_saved == 1, allowed_card_status
            with connect() as db:
                manage_user = db.execute("SELECT id, permissions FROM auth_users WHERE username='synthetic_peer'").fetchone()
                manage_id, manage_permissions = manage_user[0], manage_user[1]
                db.execute('UPDATE auth_users SET permissions=? WHERE id=?', (json.dumps(['card_manage']), manage_id))
            try:
                manage_refused = urllib.request.Request(base + '/api/phone-card', data=json.dumps(dict(id=task_card, phone=task_after[0], label='基础资料已改', status='欠费', request_id='e4e4e4e4-e4e4-4e4e-8e4e-e4e4e4e4e4e4')).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with other_client.open(manage_refused, timeout=10) as response:
                        manage_status, manage_body = response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    manage_status, manage_body = error.code, json.load(error)
                assert manage_status == 403 and manage_body.get('error') == '修改卡状态、接码和归还需要手机卡待办权限', manage_status
                with connect() as db:
                    manage_kept = db.execute('SELECT phone,label,status FROM phone_cards WHERE id=?', (task_card,)).fetchone()
                    manage_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
                    manage_reused = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', ('e4e4e4e4-e4e4-4e4e-8e4e-e4e4e4e4e4e4',)).fetchone()[0]
                manage_unchanged = manage_kept[0] == task_after[0] and manage_kept[1] == task_after[1] and manage_kept[2] == task_after[2]
                assert manage_unchanged is True and manage_audits == task_audits_after and manage_reused == 0, manage_status
                manage_allowed = urllib.request.Request(base + '/api/phone-card', data=json.dumps(dict(id=task_card, phone=task_after[0], label='基础资料已改', status=task_after[2], request_id='e5e5e5e5-e5e5-4e5e-8e5e-e5e5e5e5e5e5')).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with other_client.open(manage_allowed, timeout=10) as response:
                        manage_ok_status, manage_ok_body = response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    manage_ok_status, manage_ok_body = error.code, json.load(error)
                assert manage_ok_status == 200 and manage_ok_body.get('ok') is True, manage_ok_status
                with connect() as db:
                    manage_after = db.execute('SELECT phone,label,status FROM phone_cards WHERE id=?', (task_card,)).fetchone()
                    manage_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
                    manage_saved = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', ('e5e5e5e5-e5e5-4e5e-8e5e-e5e5e5e5e5e5',)).fetchone()[0]
                manage_base = manage_after[0] == task_after[0] and manage_after[1] == '基础资料已改' and manage_after[2] == task_after[2]
                assert manage_base is True and manage_audits_after == task_audits_after + 1 and manage_saved == 1, manage_ok_status
            finally:
                with connect() as db:
                    db.execute('UPDATE auth_users SET permissions=? WHERE id=?', (manage_permissions, manage_id))
            with connect() as db:
                saved_account = db.execute('SELECT name,status,computer_id,slot,COALESCE(note,\'\') FROM accounts WHERE id=?', (account,)).fetchone()
                account_audits_before = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?", (account,)).fetchone()[0]
                account_user = db.execute("SELECT id, permissions FROM auth_users WHERE username='synthetic_peer'").fetchone()
                account_user_id, account_permissions = account_user[0], account_user[1]
                db.execute('UPDATE auth_users SET permissions=? WHERE id=?', (json.dumps(['account_manage']), account_user_id))
            changed_status = '停用' if saved_account[1] != '停用' else '正常'
            try:
                status_body = dict(id=account, name=saved_account[0], status=changed_status, computer_id=saved_account[2], slot=int(saved_account[3]), note=saved_account[4], request_id='e6e6e6e6-e6e6-4e6e-8e6e-e6e6e6e6e6e6')
                status_request = urllib.request.Request(base + '/api/account', data=json.dumps(status_body).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with other_client.open(status_request, timeout=10) as response:
                        status_code, status_payload = response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    status_code, status_payload = error.code, json.load(error)
                assert status_code == 403 and status_payload.get('error') == '修改该字段还需要：修改账号状态', status_code
                with connect() as db:
                    status_kept = db.execute('SELECT name,status,COALESCE(note,\'\') FROM accounts WHERE id=?', (account,)).fetchone()
                    status_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?", (account,)).fetchone()[0]
                    status_pending = db.execute('SELECT state, result_json IS NULL FROM idempotency_records WHERE request_id=?', ('e6e6e6e6-e6e6-4e6e-8e6e-e6e6e6e6e6e6',)).fetchone()
                status_same = status_kept[0] == saved_account[0] and status_kept[1] == saved_account[1] and status_kept[2] == saved_account[4]
                pending_kept = status_pending[0] == 'pending' and status_pending[1] is True
                assert status_same is True and status_audits == account_audits_before and pending_kept is True, status_code
                replay_request = urllib.request.Request(base + '/api/account', data=json.dumps(status_body).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with other_client.open(replay_request, timeout=10) as response:
                        replay_code, replay_payload = response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    replay_code, replay_payload = error.code, json.load(error)
                assert replay_code == 403 and replay_payload.get('error') == '修改该字段还需要：修改账号状态', (replay_code, replay_payload.get('code'))
                with connect() as db:
                    replay_kept = db.execute('SELECT name,status,COALESCE(note,\'\') FROM accounts WHERE id=?', (account,)).fetchone()
                    replay_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?", (account,)).fetchone()[0]
                    replay_pending = db.execute('SELECT state, result_status IS NULL, result_json IS NULL FROM idempotency_records WHERE request_id=?', ('e6e6e6e6-e6e6-4e6e-8e6e-e6e6e6e6e6e6',)).fetchone()
                    replay_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', ('e6e6e6e6-e6e6-4e6e-8e6e-e6e6e6e6e6e6',)).fetchone()[0]
                replay_same = replay_kept[0] == saved_account[0] and replay_kept[1] == saved_account[1] and replay_kept[2] == saved_account[4]
                replay_open = replay_pending[0] == 'pending' and replay_pending[1] is True and replay_pending[2] is True
                assert replay_same is True and replay_audits == account_audits_before and replay_open is True and replay_count == 1, (replay_code, replay_payload.get('code'))
                note_body = dict(id=account, name=saved_account[0], status=saved_account[1], computer_id=saved_account[2], slot=int(saved_account[3]), note='账号备注标记', request_id='e8e8e8e8-e8e8-4e8e-8e8e-e8e8e8e8e8e8')
                note_request = urllib.request.Request(base + '/api/account', data=json.dumps(note_body).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with other_client.open(note_request, timeout=10) as response:
                        note_code, note_payload = response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    note_code, note_payload = error.code, json.load(error)
                assert note_code == 200 and note_payload.get('ok') is True, note_code
                with connect() as db:
                    note_after = db.execute('SELECT name,status,COALESCE(note,\'\') FROM accounts WHERE id=?', (account,)).fetchone()
                    note_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?", (account,)).fetchone()[0]
                    note_saved = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', ('e8e8e8e8-e8e8-4e8e-8e8e-e8e8e8e8e8e8',)).fetchone()[0]
                note_only = note_after[0] == saved_account[0] and note_after[1] == saved_account[1] and note_after[2] == '账号备注标记'
                assert note_only is True and note_audits == account_audits_before + 1 and note_saved == 1, note_code
            finally:
                with connect() as db:
                    db.execute('UPDATE auth_users SET permissions=? WHERE id=?', (account_permissions, account_user_id))
            with connect() as db:
                field_row = db.execute("SELECT name,status,computer_id,slot,COALESCE(note,''),password_state,phone_device_id FROM accounts WHERE id=?", (account,)).fetchone()
                field_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?", (account,)).fetchone()[0]
                field_user = db.execute("SELECT id, permissions FROM auth_users WHERE username='synthetic_peer'").fetchone()
                field_user_id, field_permissions = field_user[0], field_user[1]
                db.execute('UPDATE auth_users SET permissions=? WHERE id=?', (json.dumps(['account_manage', 'account_status']), field_user_id))
            progress_value = '未修改' if field_row[5] == '已修改' else '已修改'
            def save_account(payload):
                request = urllib.request.Request(base + '/api/account', data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with other_client.open(request, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            try:
                progress_rejected = save_account(dict(id=account, name=field_row[0], status=field_row[1], computer_id=field_row[2], slot=int(field_row[3]), note=field_row[4], password_state=progress_value, request_id='e7e7e7e7-e7e7-4e7e-8e7e-e7e7e7e7e7e7'))
                assert progress_rejected[0] == 403 and progress_rejected[1].get('error') == '修改该字段还需要：修改注册进度、稳定及改密记录', progress_rejected[0]
                with connect() as db:
                    progress_kept = db.execute('SELECT password_state,phone_device_id FROM accounts WHERE id=?', (account,)).fetchone()
                    progress_audit_count = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?", (account,)).fetchone()[0]
                    progress_pending = db.execute('SELECT state, result_json IS NULL FROM idempotency_records WHERE request_id=?', ('e7e7e7e7-e7e7-4e7e-8e7e-e7e7e7e7e7e7',)).fetchone()
                progress_same = progress_kept[0] == field_row[5] and progress_kept[1] == field_row[6]
                assert progress_same is True and progress_audit_count == field_audits and progress_pending[0] == 'pending' and progress_pending[1] is True, progress_rejected[0]
                with connect() as db:
                    db.execute('UPDATE auth_users SET permissions=? WHERE id=?', (json.dumps(['account_manage', 'account_progress', 'account_status']), field_user_id))
                progress_saved = save_account(dict(id=account, name=field_row[0], status=field_row[1], computer_id=field_row[2], slot=int(field_row[3]), note=field_row[4], password_state=progress_value, request_id='e9e9e9e9-e9e9-4e9e-8e9e-e9e9e9e9e9e9'))
                assert progress_saved[0] == 200 and progress_saved[1].get('ok') is True, progress_saved[0]
                with connect() as db:
                    progress_after = db.execute('SELECT status,password_state,phone_device_id FROM accounts WHERE id=?', (account,)).fetchone()
                    progress_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?", (account,)).fetchone()[0]
                assert progress_after[0] == field_row[1] and progress_after[1] == progress_value and progress_after[2] == field_row[6] and progress_audits_after == field_audits + 1, progress_saved[0]
                device_rejected = save_account(dict(id=account, name=field_row[0], status=field_row[1], computer_id=field_row[2], slot=int(field_row[3]), note=field_row[4], phone_device_id='missing-device', request_id='eaeaeaea-eaea-4eae-8eae-eaeaeaeaeaea'))
                assert device_rejected[0] == 403 and device_rejected[1].get('error') == '修改该字段还需要：修改手机设备及账号关联', device_rejected[0]
                with connect() as db:
                    device_kept = db.execute('SELECT password_state,phone_device_id FROM accounts WHERE id=?', (account,)).fetchone()
                    device_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?", (account,)).fetchone()[0]
                    device_rows = db.execute('SELECT count(*) FROM phone_devices WHERE id=?', ('missing-device',)).fetchone()[0]
                    device_pending = db.execute('SELECT state, result_json IS NULL FROM idempotency_records WHERE request_id=?', ('eaeaeaea-eaea-4eae-8eae-eaeaeaeaeaea',)).fetchone()
                device_same = device_kept[0] == progress_value and device_kept[1] == field_row[6] and device_rows == 0
                assert device_same is True and device_audits == progress_audits_after and device_pending[0] == 'pending' and device_pending[1] is True, device_rejected[0]
            finally:
                with connect() as db:
                    db.execute('UPDATE auth_users SET permissions=? WHERE id=?', (field_permissions, field_user_id))
            with connect() as db:
                batch_owner_before = db.execute("SELECT COALESCE(owner,'') FROM phone_cards WHERE id=?", (task_card,)).fetchone()[0]
                batch_audits_before = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
                card_count_before = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
            batch_request = urllib.request.Request(base + '/api/batch-edit', data=json.dumps(dict(kind='cards', ids=[str(task_card)], patch={'owner': '合成负责人'}, request_id='ecececec-ecec-4cec-8cec-ecececececec')).encode(), headers={'Content-Type': 'application/json'})
            try:
                with other_client.open(batch_request, timeout=10) as response:
                    batch_code, batch_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                batch_code, batch_body = error.code, json.load(error)
            assert batch_code == 403 and batch_body.get('error') == '当前账号没有此操作权限', batch_code
            missing_request = urllib.request.Request(base + '/api/phone-card', data=json.dumps(dict(id=999999, phone='13800002999', status='正常', request_id='edededed-eded-4ded-8ded-edededededed')).encode(), headers={'Content-Type': 'application/json'})
            try:
                with other_client.open(missing_request, timeout=10) as response:
                    missing_code, missing_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                missing_code, missing_body = error.code, json.load(error)
            assert missing_code == 403 and missing_body.get('error') == '新增手机卡需要手机卡基础资料权限', missing_code
            with connect() as db:
                batch_owner_after = db.execute("SELECT COALESCE(owner,'') FROM phone_cards WHERE id=?", (task_card,)).fetchone()[0]
                batch_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
                card_count_after = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                missing_card = db.execute('SELECT count(*) FROM phone_cards WHERE id=999999 OR phone=?', ('13800002999',)).fetchone()[0]
                batch_saved = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id IN (?,?)', ('ecececec-ecec-4cec-8cec-ecececececec', 'edededed-eded-4ded-8ded-edededededed')).fetchone()[0]
            batch_kept = batch_owner_after == batch_owner_before and batch_audits_after == batch_audits_before and card_count_after == card_count_before and missing_card == 0 and batch_saved == 0
            assert batch_kept is True, (batch_code, missing_code)
            with connect() as db:
                mixed_before = db.execute('SELECT status,password_state FROM accounts WHERE id=?', (account,)).fetchone()
                mixed_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
                mixed_user = db.execute("SELECT id, permissions FROM auth_users WHERE username='synthetic_peer'").fetchone()
                mixed_user_id, mixed_permissions = mixed_user[0], mixed_user[1]
                db.execute('UPDATE auth_users SET permissions=? WHERE id=?', (json.dumps(['account_status']), mixed_user_id))
            mixed_status = '正常' if mixed_before[0] != '正常' else '停用'
            mixed_progress = '未修改' if mixed_before[1] != '未修改' else '已修改'
            try:
                mixed_request = urllib.request.Request(base + '/api/batch-edit', data=json.dumps(dict(kind='accounts', ids=[str(account)], patch={'status': mixed_status, 'password_state': mixed_progress}, request_id='efefefef-efef-4fef-8fef-efefefefefef')).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with other_client.open(mixed_request, timeout=10) as response:
                        mixed_code, mixed_body = response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    mixed_code, mixed_body = error.code, json.load(error)
                assert mixed_code == 403 and mixed_body.get('error') == '当前账号没有此操作权限', mixed_code
                with connect() as db:
                    mixed_after = db.execute('SELECT status,password_state FROM accounts WHERE id=?', (account,)).fetchone()
                    mixed_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
                    mixed_saved = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', ('efefefef-efef-4fef-8fef-efefefefefef',)).fetchone()[0]
                mixed_same = mixed_after[0] == mixed_before[0] and mixed_after[1] == mixed_before[1]
                assert mixed_same is True and mixed_audits_after == mixed_audits and mixed_saved == 0, mixed_code
            finally:
                with connect() as db:
                    db.execute('UPDATE auth_users SET permissions=? WHERE id=?', (mixed_permissions, mixed_user_id))
            with connect() as db:
                unknown_before = db.execute('SELECT status FROM accounts WHERE id=?', (account,)).fetchone()[0]
                unknown_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
            unknown_request = urllib.request.Request(base + '/api/batch-edit', data=json.dumps(dict(kind='accounts', ids=[str(account)], patch={'not_listed': '未列出字段'}, request_id='f1f1f1f1-f1f1-4f1f-8f1f-f1f1f1f1f1f1')).encode(), headers={'Content-Type': 'application/json'})
            try:
                with other_client.open(unknown_request, timeout=10) as response:
                    unknown_code, unknown_payload = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                unknown_code, unknown_payload = error.code, json.load(error)
            assert unknown_code == 400 and unknown_payload.get('error') == '只能批量修改页面列出的字段', unknown_code
            with connect() as db:
                unknown_after = db.execute('SELECT status FROM accounts WHERE id=?', (account,)).fetchone()[0]
                unknown_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
                unknown_saved = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', ('f1f1f1f1-f1f1-4f1f-8f1f-f1f1f1f1f1f1',)).fetchone()[0]
            assert unknown_after == unknown_before and unknown_audits_after == unknown_audits and unknown_saved == 0, unknown_code
            with connect() as db:
                format_user = db.execute("SELECT id, permissions FROM auth_users WHERE username='synthetic_peer'").fetchone()
                format_user_id, format_permissions = format_user[0], format_user[1]
                format_cards = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                format_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
                db.execute('UPDATE auth_users SET permissions=? WHERE id=?', (json.dumps(['card_manage']), format_user_id))
            format_id = 'f2f2f2f2-f2f2-4f2f-8f2f-f2f2f2f2f2f2'
            def post_format():
                request = urllib.request.Request(base + '/api/batch-edit', data=json.dumps(dict(kind='cards', ids=['not-a-card'], patch={'owner': '未编号'}, request_id=format_id)).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with other_client.open(request, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            try:
                format_code, format_payload = post_format()
                assert format_code == 400 and format_payload.get('error') == '手机卡记录编号不正确', format_code
                with connect() as db:
                    format_row = db.execute('SELECT state, result_status, result_json IS NULL FROM idempotency_records WHERE request_id=?', (format_id,)).fetchone()
                    format_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (format_id,)).fetchone()[0]
                    format_cards_after = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                    format_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
                    format_history = db.execute("SELECT count(*) FROM operations_history WHERE entity=?", ('card:not-a-card',)).fetchone()[0]
                format_failed = format_row[0] == 'failed' and format_row[1] == 400 and format_row[2] is False
                assert format_failed is True and format_count == 1 and format_cards_after == format_cards and format_audits_after == format_audits and format_history == 0, format_code
                replay_code, replay_payload = post_format()
                assert replay_code == 400 and replay_payload.get('error') == '手机卡记录编号不正确', replay_code
                with connect() as db:
                    replay_row = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (format_id,)).fetchone()
                    replay_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (format_id,)).fetchone()[0]
                    replay_cards = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                assert replay_row[0] == 'failed' and replay_row[1] == 400 and replay_count == 1 and replay_cards == format_cards, replay_code
            finally:
                with connect() as db:
                    db.execute('UPDATE auth_users SET permissions=? WHERE id=?', (format_permissions, format_user_id))
            with connect() as db:
                continue_user = db.execute("SELECT id, permissions FROM auth_users WHERE username='synthetic_peer'").fetchone()
                continue_user_id, continue_permissions = continue_user[0], continue_user[1]
                continue_row = db.execute("SELECT name,status,computer_id,slot,COALESCE(note,'') FROM accounts WHERE id=?", (account,)).fetchone()
                continue_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                continue_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?", (account,)).fetchone()[0]
                db.execute('UPDATE auth_users SET permissions=? WHERE id=?', (json.dumps(['account_manage']), continue_user_id))
            continue_status = '停用' if continue_row[1] != '停用' else '正常'
            continue_id = 'f3f3f3f3-f3f3-4f3f-8f3f-f3f3f3f3f3f3'
            continue_request = urllib.request.Request(base + '/api/account', data=json.dumps(dict(id=account, name=continue_row[0], status=continue_status, computer_id=continue_row[2], slot=int(continue_row[3]), note=continue_row[4], request_id=continue_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_continue():
                try:
                    with other_client.open(continue_request, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            try:
                blocked_code, blocked_payload = post_continue()
                assert blocked_code == 403 and blocked_payload.get('error') == '修改该字段还需要：修改账号状态', blocked_code
                with connect() as db:
                    blocked_row = db.execute("SELECT status,COALESCE(note,'') FROM accounts WHERE id=?", (account,)).fetchone()
                    blocked_pending = db.execute('SELECT state, result_status IS NULL, result_json IS NULL FROM idempotency_records WHERE request_id=?', (continue_id,)).fetchone()
                    blocked_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    blocked_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?", (account,)).fetchone()[0]
                blocked_same = blocked_row[0] == continue_row[1] and blocked_row[1] == continue_row[4]
                blocked_open = blocked_pending[0] == 'pending' and blocked_pending[1] is True and blocked_pending[2] is True
                assert blocked_same is True and blocked_open is True and blocked_history == continue_history and blocked_audits == continue_audits, blocked_code
                with connect() as db:
                    db.execute('UPDATE auth_users SET permissions=? WHERE id=?', (json.dumps(['account_manage', 'account_status']), continue_user_id))
                continued_code, continued_payload = post_continue()
                assert continued_code == 200 and continued_payload.get('ok') is True, continued_code
                with connect() as db:
                    continued_row = db.execute("SELECT status,COALESCE(note,'') FROM accounts WHERE id=?", (account,)).fetchone()
                    continued_stored = db.execute('SELECT state, result_status, result_json IS NULL FROM idempotency_records WHERE request_id=?', (continue_id,)).fetchone()
                    continued_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    continued_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?", (account,)).fetchone()[0]
                    continued_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (continue_id,)).fetchone()[0]
                continued_written = continued_row[0] == continue_status and continued_row[1] == continue_row[4]
                continued_saved = continued_stored[0] == 'succeeded' and continued_stored[1] == 200 and continued_stored[2] is False
                assert continued_written is True and continued_saved is True and continued_history == continue_history + 1 and continued_audits == continue_audits + 1 and continued_count == 1, continued_code
                replayed_code, replayed_payload = post_continue()
                assert replayed_code == 200 and replayed_payload.get('ok') is True, replayed_code
                with connect() as db:
                    replayed_row = db.execute('SELECT status FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    replayed_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (continue_id,)).fetchone()
                    replayed_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    replayed_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account' AND target=?", (account,)).fetchone()[0]
                    replayed_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (continue_id,)).fetchone()[0]
                assert replayed_row == continue_status and replayed_stored[0] == 'succeeded' and replayed_stored[1] == 200 and replayed_history == continued_history and replayed_audits == continued_audits and replayed_count == 1, replayed_code
            finally:
                with connect() as db:
                    db.execute('UPDATE auth_users SET permissions=? WHERE id=?', (continue_permissions, continue_user_id))
            published_now = next(row['published'] for row in get_board(other_client)['accounts'] if row['id'] == account)
            published_flag = True if published_now else False
            with connect() as db:
                stale_daily = db.execute('SELECT count(*) FROM daily WHERE account_id=?', (account,)).fetchone()[0]
                stale_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                stale_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/daily'").fetchone()[0]
            stale_id = 'f4f4f4f4-f4f4-4f4f-8f4f-f4f4f4f4f4f4'
            stale_request = urllib.request.Request(base + '/api/daily', data=json.dumps(dict(id=account, date=day, published=published_flag, request_id=stale_id, _expected_versions={'account:' + account: 'stale-version', 'daily:' + account + ':' + day: 'stale-version'})).encode(), headers={'Content-Type': 'application/json'})
            def post_stale():
                try:
                    with other_client.open(stale_request, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            stale_code, stale_payload = post_stale()
            assert stale_code == 409 and stale_payload.get('error') == '这条资料已被其他人修改，请刷新核对后再保存；未覆盖对方修改', stale_code
            with connect() as db:
                stale_row = db.execute('SELECT state, result_status IS NULL, result_json IS NULL FROM idempotency_records WHERE request_id=?', (stale_id,)).fetchone()
                stale_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (stale_id,)).fetchone()[0]
                stale_daily_after = db.execute('SELECT count(*) FROM daily WHERE account_id=?', (account,)).fetchone()[0]
                stale_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                stale_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/daily'").fetchone()[0]
            stale_open = stale_row[0] == 'pending' and stale_row[1] is True and stale_row[2] is True
            assert stale_open is True and stale_count == 1 and stale_daily_after == stale_daily and stale_history_after == stale_history and stale_audits_after == stale_audits, stale_code
            stale_again_code, stale_again_payload = post_stale()
            assert stale_again_code == 409 and stale_again_payload.get('error') == '这条资料已被其他人修改，请刷新核对后再保存；未覆盖对方修改', stale_again_code
            with connect() as db:
                stale_again = db.execute('SELECT state, result_status IS NULL, result_json IS NULL FROM idempotency_records WHERE request_id=?', (stale_id,)).fetchone()
                stale_again_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (stale_id,)).fetchone()[0]
                stale_daily_again = db.execute('SELECT count(*) FROM daily WHERE account_id=?', (account,)).fetchone()[0]
            stale_still = stale_again[0] == 'pending' and stale_again[1] is True and stale_again[2] is True
            assert stale_still is True and stale_again_count == 1 and stale_daily_again == stale_daily, stale_again_code
            fresh_request = urllib.request.Request(base + '/api/daily', data=json.dumps(dict(id=account, date=day, published=published_flag, request_id=stale_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with other_client.open(fresh_request, timeout=10) as response:
                    fresh_code, fresh_payload = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                fresh_code, fresh_payload = error.code, json.load(error)
            assert fresh_code == 409 and fresh_payload.get('code') == 'REQUEST_ID_REUSED', fresh_code
            with connect() as db:
                fresh_row = db.execute('SELECT state, result_status IS NULL, result_json IS NULL FROM idempotency_records WHERE request_id=?', (stale_id,)).fetchone()
                fresh_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (stale_id,)).fetchone()[0]
                fresh_daily = db.execute('SELECT count(*) FROM daily WHERE account_id=?', (account,)).fetchone()[0]
                fresh_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
            fresh_open = fresh_row[0] == 'pending' and fresh_row[1] is True and fresh_row[2] is True
            assert fresh_open is True and fresh_count == 1 and fresh_daily == stale_daily and fresh_history == stale_history, fresh_code
            daily_key = 'daily:' + account + ':' + day
            opposite_flag = not published_flag
            with connect() as db:
                daily_history_before = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', (daily_key,)).fetchone()[0]
            fresh_daily_id = 'f7f7f7f7-f7f7-4f7f-8f7f-f7f7f7f7f7f7'
            fresh_daily_request = urllib.request.Request(base + '/api/daily', data=json.dumps(dict(id=account, date=day, published=opposite_flag, request_id=fresh_daily_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_fresh_daily():
                try:
                    with other_client.open(fresh_daily_request, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            fresh_daily_code, fresh_daily_payload = post_fresh_daily()
            assert fresh_daily_code == 200 and fresh_daily_payload.get('ok') is True, fresh_daily_code
            with connect() as db:
                fresh_daily_value = db.execute('SELECT published FROM daily WHERE account_id=? AND day=?', (account, day)).fetchone()[0]
                fresh_daily_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (fresh_daily_id,)).fetchone()
                fresh_daily_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (fresh_daily_id,)).fetchone()[0]
                fresh_daily_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', (daily_key,)).fetchone()[0]
                stuck_row = db.execute('SELECT state FROM idempotency_records WHERE request_id=?', (stale_id,)).fetchone()[0]
            assert fresh_daily_value == int(opposite_flag) and fresh_daily_stored[0] == 'succeeded' and fresh_daily_stored[1] == 200 and fresh_daily_count == 1 and fresh_daily_history == daily_history_before + 1 and stuck_row == 'pending', fresh_daily_code
            fresh_daily_again_code, fresh_daily_again_payload = post_fresh_daily()
            assert fresh_daily_again_code == 200 and fresh_daily_again_payload.get('ok') is True, fresh_daily_again_code
            with connect() as db:
                fresh_daily_history_again = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', (daily_key,)).fetchone()[0]
                fresh_daily_count_again = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (fresh_daily_id,)).fetchone()[0]
            assert fresh_daily_history_again == fresh_daily_history and fresh_daily_count_again == 1, fresh_daily_again_code
            restore_daily_id = 'f9f9f9f9-f9f9-4f9f-8f9f-f9f9f9f9f9f9'
            restore_daily_request = urllib.request.Request(base + '/api/daily', data=json.dumps(dict(id=account, date=day, published=published_flag, request_id=restore_daily_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with other_client.open(restore_daily_request, timeout=10) as response:
                    restore_daily_code, restore_daily_payload = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                restore_daily_code, restore_daily_payload = error.code, json.load(error)
            assert restore_daily_code == 200 and restore_daily_payload.get('ok') is True, restore_daily_code
            with connect() as db:
                restored_daily_value = db.execute('SELECT published FROM daily WHERE account_id=? AND day=?', (account, day)).fetchone()[0]
            assert restored_daily_value == int(published_flag), restore_daily_code
            idle_device = '8'
            idle_create_id = 'abababab-abab-4aba-8aba-abababababab'
            idle_update_id = 'bcbcbcbc-bcbc-4bcb-8bcb-bcbcbcbcbcbc'
            idle_create = urllib.request.Request(base + '/api/phone-device/create', data=json.dumps(dict(id=idle_device, state='在用', request_id=idle_create_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(idle_create, timeout=10) as response:
                    idle_create_code, idle_create_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                idle_create_code, idle_create_body = error.code, json.load(error)
            assert idle_create_code == 200 and idle_create_body.get('ok') is True, idle_create_code
            with connect() as db:
                previous_device = db.execute('SELECT phone_device_id FROM accounts WHERE id=?', (account,)).fetchone()[0]
                db.execute('UPDATE accounts SET phone_device_id=? WHERE id=?', (idle_device, account))
                idle_state_before = db.execute('SELECT state FROM phone_devices WHERE id=?', (idle_device,)).fetchone()[0]
                idle_history_before = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('device:' + idle_device,)).fetchone()[0]
                idle_audits_before = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device'").fetchone()[0]
            assert idle_state_before == '在用', idle_create_code
            idle_update = urllib.request.Request(base + '/api/phone-device', data=json.dumps(dict(id=idle_device, state='可用闲置', request_id=idle_update_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_idle():
                try:
                    with current_owner.open(idle_update, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            try:
                idle_code, idle_body = post_idle()
                assert idle_code == 400 and idle_body.get('error') == '仍有当前账号关联，请先移出账号并确认清理完成，再标记可用闲置', idle_code
                idle_again_code, idle_again_body = post_idle()
                assert idle_again_code == 400 and idle_again_body.get('error') == '仍有当前账号关联，请先移出账号并确认清理完成，再标记可用闲置', idle_again_code
                with connect() as db:
                    idle_state_after = db.execute('SELECT state FROM phone_devices WHERE id=?', (idle_device,)).fetchone()[0]
                    idle_link_after = db.execute('SELECT phone_device_id FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    idle_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (idle_update_id,)).fetchone()
                    idle_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (idle_update_id,)).fetchone()[0]
                    idle_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('device:' + idle_device,)).fetchone()[0]
                    idle_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device'").fetchone()[0]
                idle_kept = idle_state_after == '在用' and idle_link_after == idle_device and idle_stored[0] == 'failed' and idle_stored[1] == 400 and idle_count == 1 and idle_history_after == idle_history_before and idle_audits_after == idle_audits_before
                assert idle_kept is True, idle_code
            finally:
                with connect() as db:
                    db.execute('UPDATE accounts SET phone_device_id=? WHERE id=?', (previous_device, account))
            child_create_id = 'cdcdcdcd-cdcd-4cdc-8cdc-cdcdcdcdcdcd'
            child_reject_id = 'd1d1d1d1-d1d1-4d1d-8d1d-d1d1d1d1d1d1'
            child_id = None
            with connect() as db:
                parent_type = db.execute('SELECT account_type FROM accounts WHERE id=?', (account,)).fetchone()[0]
                db.execute("UPDATE accounts SET account_type='大号' WHERE id=?", (account,))
            try:
                child_create = urllib.request.Request(base + '/api/account/create', data=json.dumps(dict(name='配对小号', status='正常', computer_id='synthetic-pc', slot=2, account_type='小号', parent_account_id=account, request_id=child_create_id)).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with current_owner.open(child_create, timeout=10) as response:
                        child_create_code, child_create_body = response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    child_create_code, child_create_body = error.code, json.load(error)
                assert child_create_code == 200 and child_create_body.get('ok') is True, child_create_code
                child_id = child_create_body.get('id')
                with connect() as db:
                    aux_before = db.execute('SELECT auxiliary_state FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    linked_before = db.execute('SELECT count(*) FROM accounts WHERE parent_account_id=?', (account,)).fetchone()[0]
                    child_history_before = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    child_audits_before = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/lifecycle-status'").fetchone()[0]
                assert linked_before == 1, child_create_code
                child_reject = urllib.request.Request(base + '/api/lifecycle-status', data=json.dumps(dict(id=account, auxiliary_state='未注册', request_id=child_reject_id)).encode(), headers={'Content-Type': 'application/json'})
                def post_child_reject():
                    try:
                        with current_owner.open(child_reject, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                child_code, child_body = post_child_reject()
                assert child_code == 400 and child_body.get('error') == '已有小号关联，不能将大号改为其他类型或标记未注册', child_code
                child_again_code, child_again_body = post_child_reject()
                assert child_again_code == 400 and child_again_body.get('error') == '已有小号关联，不能将大号改为其他类型或标记未注册', child_again_code
                with connect() as db:
                    aux_after = db.execute('SELECT auxiliary_state FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    linked_after = db.execute('SELECT count(*) FROM accounts WHERE parent_account_id=?', (account,)).fetchone()[0]
                    child_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (child_reject_id,)).fetchone()
                    child_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (child_reject_id,)).fetchone()[0]
                    child_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    child_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/lifecycle-status'").fetchone()[0]
                child_kept = aux_after == aux_before and linked_after == 1 and child_stored[0] == 'failed' and child_stored[1] == 400 and child_count == 1 and child_history_after == child_history_before and child_audits_after == child_audits_before
                assert child_kept is True, child_code
            finally:
                with connect() as db:
                    if child_id:
                        db.execute('DELETE FROM accounts WHERE id=?', (child_id,))
                    db.execute('UPDATE accounts SET account_type=? WHERE id=?', (parent_type, account))
            first_child_id = None
            extra_child_id = 'e0e0e0e0-e0e0-4e0e-8e0e-e0e0e0e0e0e0'
            second_child_id = 'd2d2d2d2-d2d2-4d2d-8d2d-d2d2d2d2d2d2'
            with connect() as db:
                second_parent_type = db.execute('SELECT account_type FROM accounts WHERE id=?', (account,)).fetchone()[0]
                db.execute("UPDATE accounts SET account_type='大号' WHERE id=?", (account,))
            try:
                first_child = urllib.request.Request(base + '/api/account/create', data=json.dumps(dict(name='已有小号', status='正常', computer_id='synthetic-pc', slot=2, account_type='小号', parent_account_id=account, request_id=extra_child_id)).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with current_owner.open(first_child, timeout=10) as response:
                        first_child_code, first_child_body = response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    first_child_code, first_child_body = error.code, json.load(error)
                assert first_child_code == 200 and first_child_body.get('ok') is True, first_child_code
                first_child_id = first_child_body.get('id')
                with connect() as db:
                    placeholder_before = db.execute("SELECT count(*) FROM accounts WHERE name='新账号'").fetchone()[0]
                    siblings_before = db.execute('SELECT count(*) FROM accounts WHERE parent_account_id=?', (account,)).fetchone()[0]
                    slot_before = db.execute("SELECT count(*) FROM accounts WHERE computer_id='synthetic-pc' AND slot=3").fetchone()[0]
                    second_history_before = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    second_audits_before = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
                assert siblings_before == 1 and slot_before == 0, first_child_code
                second_child = urllib.request.Request(base + '/api/account/create', data=json.dumps(dict(name='再加小号', status='正常', computer_id='synthetic-pc', slot=3, account_type='小号', parent_account_id=account, request_id=second_child_id)).encode(), headers={'Content-Type': 'application/json'})
                def post_second_child():
                    try:
                        with current_owner.open(second_child, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                second_code, second_body = post_second_child()
                assert second_code == 400 and second_body.get('error') == '这个大号已经关联小号，请先核对原有关系', second_code
                second_again_code, second_again_body = post_second_child()
                assert second_again_code == 400 and second_again_body.get('error') == '这个大号已经关联小号，请先核对原有关系', second_again_code
                with connect() as db:
                    placeholder_after = db.execute("SELECT count(*) FROM accounts WHERE name='新账号'").fetchone()[0]
                    siblings_after = db.execute('SELECT count(*) FROM accounts WHERE parent_account_id=?', (account,)).fetchone()[0]
                    slot_after = db.execute("SELECT count(*) FROM accounts WHERE computer_id='synthetic-pc' AND slot=3").fetchone()[0]
                    kept_child = db.execute('SELECT id FROM accounts WHERE parent_account_id=?', (account,)).fetchone()[0]
                    second_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (second_child_id,)).fetchone()
                    second_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (second_child_id,)).fetchone()[0]
                    second_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    second_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
                second_kept = placeholder_after == placeholder_before and siblings_after == 1 and slot_after == 0 and kept_child == first_child_id and second_stored[0] == 'failed' and second_stored[1] == 400 and second_count == 1 and second_history_after == second_history_before and second_audits_after == second_audits_before
                assert second_kept is True, second_code
            finally:
                with connect() as db:
                    if first_child_id:
                        db.execute('DELETE FROM accounts WHERE id=?', (first_child_id,))
                    db.execute('UPDATE accounts SET account_type=? WHERE id=?', (second_parent_type, account))
            future_id = 'c0c0c0c0-c0c0-4c0c-8c0c-c0c0c0c0c0c0'
            future_day = (datetime.date.fromisoformat(day) + datetime.timedelta(days=1)).isoformat()
            registered_before = None
            future_ready = False
            try:
                with connect() as db:
                    registered_before = db.execute('SELECT registered_on FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    future_history_before = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    future_audits_before = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                future_ready = True
                future_request = urllib.request.Request(base + '/api/account/patch', data=json.dumps(dict(id=account, changes=dict(registered_on=future_day), request_id=future_id)).encode(), headers={'Content-Type': 'application/json'})
                def post_future():
                    try:
                        with current_owner.open(future_request, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                future_code, future_body = post_future()
                assert future_code == 400 and future_body.get('error') == '注册日期不能晚于今天', future_code
                future_again_code, future_again_body = post_future()
                assert future_again_code == 400 and future_again_body.get('error') == '注册日期不能晚于今天', future_again_code
                with connect() as db:
                    registered_after = db.execute('SELECT registered_on FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    future_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (future_id,)).fetchone()
                    future_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (future_id,)).fetchone()[0]
                    future_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    future_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                future_kept = registered_after == registered_before and future_stored[0] == 'failed' and future_stored[1] == 400 and future_count == 1 and future_history_after == future_history_before and future_audits_after == future_audits_before
                assert future_kept is True, future_code
            finally:
                if future_ready:
                    with connect() as db:
                        db.execute('UPDATE accounts SET registered_on=? WHERE id=?', (registered_before, account))
            phone_child_id = None
            phone_ready = False
            child_phone = '13800003151'
            other_phone = '13800003152'
            phone_child_request_id = 'c1c1c1c1-c1c1-4c1c-8c1c-c1c1c1c1c1c1'
            phone_reject_id = 'd6d6d6d6-d6d6-4d6d-8d6d-d6d6d6d6d6d6'
            try:
                with connect() as db:
                    phone_parent = db.execute('SELECT account_type, registration_phone FROM accounts WHERE id=?', (account,)).fetchone()
                    db.execute("UPDATE accounts SET account_type='大号' WHERE id=?", (account,))
                phone_ready = True
                phone_child = urllib.request.Request(base + '/api/account/create', data=json.dumps(dict(name='号码不一致小号', status='正常', computer_id='synthetic-pc', slot=2, account_type='小号', parent_account_id=account, registration_phone=child_phone, request_id=phone_child_request_id)).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with current_owner.open(phone_child, timeout=10) as response:
                        phone_child_code, phone_child_body = response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    phone_child_code, phone_child_body = error.code, json.load(error)
                assert phone_child_code == 200 and phone_child_body.get('ok') is True, phone_child_code
                phone_child_id = phone_child_body.get('id')
                with connect() as db:
                    parent_phone_before = db.execute('SELECT registration_phone FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    linked_phone_before = db.execute('SELECT registration_phone FROM accounts WHERE id=?', (phone_child_id,)).fetchone()[0]
                    phone_history_before = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    phone_audits_before = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                phones_ready = parent_phone_before == phone_parent[1] and linked_phone_before == child_phone
                assert phones_ready is True, phone_child_code
                phone_reject = urllib.request.Request(base + '/api/account/patch', data=json.dumps(dict(id=account, changes=dict(registration_phone=other_phone), request_id=phone_reject_id)).encode(), headers={'Content-Type': 'application/json'})
                def post_phone_reject():
                    try:
                        with current_owner.open(phone_reject, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                phone_code, phone_body = post_phone_reject()
                assert phone_code == 400 and phone_body.get('error') == '修改后会与已关联小号的注册手机号不一致，请先核对或解除配对关系', phone_code
                phone_again_code, phone_again_body = post_phone_reject()
                assert phone_again_code == 400 and phone_again_body.get('error') == '修改后会与已关联小号的注册手机号不一致，请先核对或解除配对关系', phone_again_code
                with connect() as db:
                    parent_phone_after = db.execute('SELECT registration_phone FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    linked_phone_after = db.execute('SELECT registration_phone FROM accounts WHERE id=?', (phone_child_id,)).fetchone()[0]
                    phone_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (phone_reject_id,)).fetchone()
                    phone_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (phone_reject_id,)).fetchone()[0]
                    phone_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    phone_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                phone_kept = parent_phone_after == parent_phone_before and linked_phone_after == linked_phone_before and phone_stored[0] == 'failed' and phone_stored[1] == 400 and phone_count == 1 and phone_history_after == phone_history_before and phone_audits_after == phone_audits_before
                assert phone_kept is True, phone_code
            finally:
                with connect() as db:
                    if phone_child_id:
                        db.execute('DELETE FROM accounts WHERE id=?', (phone_child_id,))
                    if phone_ready:
                        db.execute('UPDATE accounts SET account_type=?, registration_phone=? WHERE id=?', (phone_parent[0], phone_parent[1], account))
            plain_child_id = 'c2c2c2c2-c2c2-4c2c-8c2c-c2c2c2c2c2c2'
            with connect() as db:
                plain_type = db.execute('SELECT account_type FROM accounts WHERE id=?', (account,)).fetchone()[0]
                plain_placeholder = db.execute("SELECT count(*) FROM accounts WHERE name='新账号'").fetchone()[0]
                plain_children = db.execute('SELECT count(*) FROM accounts WHERE parent_account_id=?', (account,)).fetchone()[0]
                plain_slot = db.execute("SELECT count(*) FROM accounts WHERE computer_id='synthetic-pc' AND slot=2").fetchone()[0]
                plain_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                plain_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
            assert plain_type != '大号' and plain_children == 0 and plain_slot == 0, 0
            plain_child = urllib.request.Request(base + '/api/account/create', data=json.dumps(dict(name='关联目标不是大号', status='正常', computer_id='synthetic-pc', slot=2, account_type='小号', parent_account_id=account, request_id=plain_child_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_plain_child():
                try:
                    with current_owner.open(plain_child, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            try:
                plain_code, plain_body = post_plain_child()
                assert plain_code == 400 and plain_body.get('error') == '小号只能关联另一个已标记为大号的账号', plain_code
                plain_again_code, plain_again_body = post_plain_child()
                assert plain_again_code == 400 and plain_again_body.get('error') == '小号只能关联另一个已标记为大号的账号', plain_again_code
                with connect() as db:
                    plain_type_after = db.execute('SELECT account_type FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    plain_placeholder_after = db.execute("SELECT count(*) FROM accounts WHERE name='新账号'").fetchone()[0]
                    plain_children_after = db.execute('SELECT count(*) FROM accounts WHERE parent_account_id=?', (account,)).fetchone()[0]
                    plain_slot_after = db.execute("SELECT count(*) FROM accounts WHERE computer_id='synthetic-pc' AND slot=2").fetchone()[0]
                    plain_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (plain_child_id,)).fetchone()
                    plain_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (plain_child_id,)).fetchone()[0]
                    plain_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    plain_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
                plain_kept = plain_type_after == plain_type and plain_placeholder_after == plain_placeholder and plain_children_after == 0 and plain_slot_after == 0 and plain_stored[0] == 'failed' and plain_stored[1] == 400 and plain_count == 1 and plain_history_after == plain_history and plain_audits_after == plain_audits
                assert plain_kept is True, plain_code
            finally:
                with connect() as db:
                    db.execute("DELETE FROM accounts WHERE computer_id='synthetic-pc' AND slot=2")
                    db.execute("DELETE FROM accounts WHERE name='新账号'")
            mismatch_id = 'c5c5c5c5-c5c5-4c5c-8c5c-c5c5c5c5c5c5'
            parent_phone = '13800003181'
            child_phone_mismatch = '13800003182'
            mismatch_ready = False
            try:
                with connect() as db:
                    mismatch_parent = db.execute('SELECT account_type, registration_phone FROM accounts WHERE id=?', (account,)).fetchone()
                    mismatch_placeholder = db.execute("SELECT count(*) FROM accounts WHERE name='新账号'").fetchone()[0]
                    mismatch_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    mismatch_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
                    db.execute("UPDATE accounts SET account_type='大号', registration_phone=? WHERE id=?", (parent_phone, account))
                mismatch_ready = True
                mismatch_child = urllib.request.Request(base + '/api/account/create', data=json.dumps(dict(name='号码对不上', status='正常', computer_id='synthetic-pc', slot=2, account_type='小号', parent_account_id=account, registration_phone=child_phone_mismatch, request_id=mismatch_id)).encode(), headers={'Content-Type': 'application/json'})
                def post_mismatch():
                    try:
                        with current_owner.open(mismatch_child, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                mismatch_code, mismatch_body = post_mismatch()
                assert mismatch_code == 400 and mismatch_body.get('error') == '大小号的注册手机号不一致，请核对配对关系', mismatch_code
                mismatch_again_code, mismatch_again_body = post_mismatch()
                assert mismatch_again_code == 400 and mismatch_again_body.get('error') == '大小号的注册手机号不一致，请核对配对关系', mismatch_again_code
                with connect() as db:
                    mismatch_type = db.execute('SELECT account_type, registration_phone FROM accounts WHERE id=?', (account,)).fetchone()
                    mismatch_children = db.execute('SELECT count(*) FROM accounts WHERE parent_account_id=?', (account,)).fetchone()[0]
                    mismatch_slot = db.execute("SELECT count(*) FROM accounts WHERE computer_id='synthetic-pc' AND slot=2").fetchone()[0]
                    mismatch_placeholder_after = db.execute("SELECT count(*) FROM accounts WHERE name='新账号'").fetchone()[0]
                    mismatch_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (mismatch_id,)).fetchone()
                    mismatch_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (mismatch_id,)).fetchone()[0]
                    mismatch_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    mismatch_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
                mismatch_kept = mismatch_type[0] == '大号' and mismatch_type[1] == parent_phone and mismatch_children == 0 and mismatch_slot == 0 and mismatch_placeholder_after == mismatch_placeholder and mismatch_stored[0] == 'failed' and mismatch_stored[1] == 400 and mismatch_count == 1 and mismatch_history_after == mismatch_history and mismatch_audits_after == mismatch_audits
                assert mismatch_kept is True, mismatch_code
            finally:
                with connect() as db:
                    db.execute("DELETE FROM accounts WHERE computer_id='synthetic-pc' AND slot=2")
                    db.execute("DELETE FROM accounts WHERE name='新账号'")
                    if mismatch_ready:
                        db.execute('UPDATE accounts SET account_type=?, registration_phone=? WHERE id=?', (mismatch_parent[0], mismatch_parent[1], account))
            shared_phone = '13800003191'
            second_main_request = 'c6c6c6c6-c6c6-4c6c-8c6c-c6c6c6c6c6c6'
            ambiguous_request = 'c7c7c7c7-c7c7-4c7c-8c7c-c7c7c7c7c7c7'
            second_main_id = None
            ambiguous_ready = False
            try:
                with connect() as db:
                    ambiguous_parent = db.execute('SELECT account_type, registration_phone FROM accounts WHERE id=?', (account,)).fetchone()
                    db.execute("UPDATE accounts SET account_type='大号', registration_phone=? WHERE id=?", (shared_phone, account))
                ambiguous_ready = True
                second_main = urllib.request.Request(base + '/api/account/create', data=json.dumps(dict(name='另一个大号', status='正常', computer_id='synthetic-pc', slot=2, account_type='大号', registration_phone=shared_phone, request_id=second_main_request)).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with current_owner.open(second_main, timeout=10) as response:
                        second_main_code, second_main_body = response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    second_main_code, second_main_body = error.code, json.load(error)
                assert second_main_code == 200 and second_main_body.get('ok') is True, second_main_code
                second_main_id = second_main_body.get('id')
                with connect() as db:
                    ambiguous_placeholder = db.execute("SELECT count(*) FROM accounts WHERE name='新账号'").fetchone()[0]
                    ambiguous_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    ambiguous_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
                ambiguous_child = urllib.request.Request(base + '/api/account/create', data=json.dumps(dict(name='待确认配对', status='正常', computer_id='synthetic-pc', slot=3, account_type='小号', registration_phone=shared_phone, request_id=ambiguous_request)).encode(), headers={'Content-Type': 'application/json'})
                def post_ambiguous():
                    try:
                        with current_owner.open(ambiguous_child, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                ambiguous_code, ambiguous_body = post_ambiguous()
                assert ambiguous_code == 400 and ambiguous_body.get('error') == '同一手机号对应多个大号，请手动确认关联大号', ambiguous_code
                ambiguous_again_code, ambiguous_again_body = post_ambiguous()
                assert ambiguous_again_code == 400 and ambiguous_again_body.get('error') == '同一手机号对应多个大号，请手动确认关联大号', ambiguous_again_code
                with connect() as db:
                    ambiguous_type = db.execute('SELECT account_type, registration_phone FROM accounts WHERE id=?', (account,)).fetchone()
                    second_phone = db.execute('SELECT registration_phone FROM accounts WHERE id=?', (second_main_id,)).fetchone()[0]
                    ambiguous_children = db.execute('SELECT count(*) FROM accounts WHERE parent_account_id IN (?,?)', (account, second_main_id)).fetchone()[0]
                    ambiguous_slot = db.execute("SELECT count(*) FROM accounts WHERE computer_id='synthetic-pc' AND slot=3").fetchone()[0]
                    ambiguous_placeholder_after = db.execute("SELECT count(*) FROM accounts WHERE name='新账号'").fetchone()[0]
                    ambiguous_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (ambiguous_request,)).fetchone()
                    ambiguous_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (ambiguous_request,)).fetchone()[0]
                    ambiguous_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    ambiguous_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
                ambiguous_kept = ambiguous_type[0] == '大号' and ambiguous_type[1] == shared_phone and second_phone == shared_phone and ambiguous_children == 0 and ambiguous_slot == 0 and ambiguous_placeholder_after == ambiguous_placeholder and ambiguous_stored[0] == 'failed' and ambiguous_stored[1] == 400 and ambiguous_count == 1 and ambiguous_history_after == ambiguous_history and ambiguous_audits_after == ambiguous_audits
                assert ambiguous_kept is True, ambiguous_code
            finally:
                with connect() as db:
                    if second_main_id:
                        db.execute('DELETE FROM accounts WHERE id=?', (second_main_id,))
                    db.execute("DELETE FROM accounts WHERE computer_id='synthetic-pc' AND slot=3")
                    db.execute("DELETE FROM accounts WHERE name='新账号'")
                    if ambiguous_ready:
                        db.execute('UPDATE accounts SET account_type=?, registration_phone=? WHERE id=?', (ambiguous_parent[0], ambiguous_parent[1], account))
            auto_phone = '13800003201'
            auto_request = 'c8c8c8c8-c8c8-4c8c-8c8c-c8c8c8c8c8c8'
            auto_child_id = None
            auto_ready = False
            try:
                with connect() as db:
                    auto_parent = db.execute('SELECT account_type, registration_phone FROM accounts WHERE id=?', (account,)).fetchone()
                    auto_accounts = db.execute('SELECT count(*) FROM accounts').fetchone()[0]
                    auto_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
                    db.execute("UPDATE accounts SET account_type='大号', registration_phone=? WHERE id=?", (auto_phone, account))
                auto_ready = True
                auto_child = urllib.request.Request(base + '/api/account/create', data=json.dumps(dict(name='自动配对小号', status='正常', computer_id='synthetic-pc', slot=2, account_type='小号', registration_phone=auto_phone, request_id=auto_request)).encode(), headers={'Content-Type': 'application/json'})
                def post_auto():
                    try:
                        with current_owner.open(auto_child, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                auto_code, auto_body = post_auto()
                assert auto_code == 200 and auto_body.get('ok') is True, auto_code
                auto_child_id = auto_body.get('id')
                with connect() as db:
                    auto_parent_id = db.execute('SELECT parent_account_id, registration_phone FROM accounts WHERE id=?', (auto_child_id,)).fetchone()
                    auto_children = db.execute('SELECT count(*) FROM accounts WHERE parent_account_id=?', (account,)).fetchone()[0]
                    auto_accounts_after = db.execute('SELECT count(*) FROM accounts').fetchone()[0]
                    auto_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
                auto_linked = auto_parent_id[0] == account and auto_parent_id[1] == auto_phone and auto_children == 1 and auto_accounts_after == auto_accounts + 1 and auto_audits_after == auto_audits + 1
                assert auto_linked is True, auto_code
                auto_again_code, auto_again_body = post_auto()
                assert auto_again_code == 200 and auto_again_body.get('ok') is True, auto_again_code
                with connect() as db:
                    auto_children_again = db.execute('SELECT count(*) FROM accounts WHERE parent_account_id=?', (account,)).fetchone()[0]
                    auto_accounts_again = db.execute('SELECT count(*) FROM accounts').fetchone()[0]
                    auto_audits_again = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
                    auto_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (auto_request,)).fetchone()
                auto_once = auto_children_again == 1 and auto_accounts_again == auto_accounts_after and auto_audits_again == auto_audits_after and auto_stored[0] == 'succeeded' and auto_stored[1] == 200
                assert auto_once is True, auto_again_code
            finally:
                with connect() as db:
                    if auto_child_id:
                        db.execute('DELETE FROM accounts WHERE id=?', (auto_child_id,))
                    db.execute("DELETE FROM accounts WHERE computer_id='synthetic-pc' AND slot=2")
                    db.execute("DELETE FROM accounts WHERE name='新账号'")
                    if auto_ready:
                        db.execute('UPDATE accounts SET account_type=?, registration_phone=? WHERE id=?', (auto_parent[0], auto_parent[1], account))
            bad_phone = 'bad-phone'
            bad_phone_id = 'c9c9c9c9-c9c9-4c9c-8c9c-c9c9c9c9c9c9'
            bad_phone_before = None
            bad_phone_ready = False
            try:
                with connect() as db:
                    bad_phone_before = db.execute('SELECT registration_phone FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    bad_history_before = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    bad_audits_before = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                bad_phone_ready = True
                bad_phone_request = urllib.request.Request(base + '/api/account/patch', data=json.dumps(dict(id=account, changes=dict(registration_phone=bad_phone), request_id=bad_phone_id)).encode(), headers={'Content-Type': 'application/json'})
                def post_bad_phone():
                    try:
                        with current_owner.open(bad_phone_request, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                bad_code, bad_body = post_bad_phone()
                assert bad_code == 400 and bad_body.get('error') == '注册手机号应为11位手机号，未知可留空', bad_code
                bad_again_code, bad_again_body = post_bad_phone()
                assert bad_again_code == 400 and bad_again_body.get('error') == '注册手机号应为11位手机号，未知可留空', bad_again_code
                with connect() as db:
                    bad_phone_after = db.execute('SELECT registration_phone FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    bad_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (bad_phone_id,)).fetchone()
                    bad_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (bad_phone_id,)).fetchone()[0]
                    bad_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    bad_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                bad_kept = bad_phone_after == bad_phone_before and bad_stored[0] == 'failed' and bad_stored[1] == 400 and bad_count == 1 and bad_history_after == bad_history_before and bad_audits_after == bad_audits_before
                assert bad_kept is True, bad_code
            finally:
                if bad_phone_ready:
                    with connect() as db:
                        db.execute('UPDATE accounts SET registration_phone=? WHERE id=?', (bad_phone_before, account))
            bad_type = '不是类型'
            bad_type_id = 'b1b1b1b1-b1b1-4b1b-8b1b-b1b1b1b1b1b1'
            try:
                with connect() as db:
                    type_before = db.execute('SELECT account_type FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    type_accounts = db.execute('SELECT count(*) FROM accounts').fetchone()[0]
                    type_placeholders = db.execute("SELECT count(*) FROM accounts WHERE name='新账号'").fetchone()[0]
                    type_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    type_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
                bad_type_request = urllib.request.Request(base + '/api/account/create', data=json.dumps(dict(name='类型不合法', status='正常', computer_id='synthetic-pc', slot=2, account_type=bad_type, request_id=bad_type_id)).encode(), headers={'Content-Type': 'application/json'})
                def post_bad_type():
                    try:
                        with current_owner.open(bad_type_request, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                type_code, type_body = post_bad_type()
                assert type_code == 400 and type_body.get('error') == '账号类型或辅助账号注册状态不正确', type_code
                type_again_code, type_again_body = post_bad_type()
                assert type_again_code == 400 and type_again_body.get('error') == '账号类型或辅助账号注册状态不正确', type_again_code
                with connect() as db:
                    type_after = db.execute('SELECT account_type FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    type_accounts_after = db.execute('SELECT count(*) FROM accounts').fetchone()[0]
                    type_placeholders_after = db.execute("SELECT count(*) FROM accounts WHERE name='新账号'").fetchone()[0]
                    type_slot = db.execute("SELECT count(*) FROM accounts WHERE computer_id='synthetic-pc' AND slot=2").fetchone()[0]
                    type_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (bad_type_id,)).fetchone()
                    type_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (bad_type_id,)).fetchone()[0]
                    type_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    type_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
                type_kept = type_after == type_before and type_accounts_after == type_accounts and type_placeholders_after == type_placeholders and type_slot == 0 and type_stored[0] == 'failed' and type_stored[1] == 400 and type_count == 1 and type_history_after == type_history and type_audits_after == type_audits
                assert type_kept is True, type_code
            finally:
                with connect() as db:
                    db.execute("DELETE FROM accounts WHERE computer_id='synthetic-pc' AND slot=2")
                    db.execute("DELETE FROM accounts WHERE name='新账号'")
            blank_phone = '13800003231'
            blank_id = 'b2b2b2b2-b2b2-4b2b-8b2b-b2b2b2b2b2b2'
            blank_before = None
            blank_ready = False
            try:
                with connect() as db:
                    blank_before = db.execute('SELECT registration_phone FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    db.execute('UPDATE accounts SET registration_phone=? WHERE id=?', (blank_phone, account))
                    blank_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    blank_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                blank_ready = True
                blank_request = urllib.request.Request(base + '/api/account/patch', data=json.dumps(dict(id=account, changes=dict(registration_phone=''), request_id=blank_id)).encode(), headers={'Content-Type': 'application/json'})
                def post_blank():
                    try:
                        with current_owner.open(blank_request, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                blank_code, blank_body = post_blank()
                assert blank_code == 200 and blank_body.get('ok') is True, blank_code
                with connect() as db:
                    blank_after = db.execute('SELECT registration_phone FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    blank_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    blank_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                blank_cleared = blank_after == '' and blank_history_after == blank_history + 1 and blank_audits_after == blank_audits + 1
                assert blank_cleared is True, blank_code
                blank_again_code, blank_again_body = post_blank()
                assert blank_again_code == 200 and blank_again_body.get('ok') is True, blank_again_code
                with connect() as db:
                    blank_again = db.execute('SELECT registration_phone FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    blank_history_again = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    blank_audits_again = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                    blank_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (blank_id,)).fetchone()
                blank_once = blank_again == '' and blank_history_again == blank_history_after and blank_audits_again == blank_audits_after and blank_stored[0] == 'succeeded' and blank_stored[1] == 200
                assert blank_once is True, blank_again_code
            finally:
                if blank_ready:
                    with connect() as db:
                        db.execute('UPDATE accounts SET registration_phone=? WHERE id=?', (blank_before, account))
            shared_identity = 'synthetic-id-0324'
            identity_create_id = 'b4b4b4b4-b4b4-4b4b-8b4b-b4b4b4b4b4b4'
            identity_reject_id = 'b5b5b5b5-b5b5-4b5b-8b5b-b5b5b5b5b5b5'
            identity_other_id = None
            identity_before = None
            identity_ready = False
            try:
                with connect() as db:
                    identity_before = db.execute('SELECT douyin_id FROM accounts WHERE id=?', (account,)).fetchone()[0]
                identity_ready = True
                identity_create = urllib.request.Request(base + '/api/account/create', data=json.dumps(dict(name='身份重复小号', status='正常', computer_id='synthetic-pc', slot=2, douyin_id=shared_identity, request_id=identity_create_id)).encode(), headers={'Content-Type': 'application/json'})
                try:
                    with current_owner.open(identity_create, timeout=10) as response:
                        identity_create_code, identity_create_body = response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    identity_create_code, identity_create_body = error.code, json.load(error)
                assert identity_create_code == 200 and identity_create_body.get('ok') is True, identity_create_code
                identity_other_id = identity_create_body.get('id')
                with connect() as db:
                    identity_seed = db.execute('SELECT douyin_id FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    identity_other = db.execute('SELECT douyin_id FROM accounts WHERE id=?', (identity_other_id,)).fetchone()[0]
                    identity_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    identity_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                identity_split = identity_seed == identity_before and identity_other == shared_identity
                assert identity_split is True, identity_create_code
                identity_reject = urllib.request.Request(base + '/api/account/patch', data=json.dumps(dict(id=account, changes=dict(douyin_id=shared_identity), request_id=identity_reject_id)).encode(), headers={'Content-Type': 'application/json'})
                def post_identity():
                    try:
                        with current_owner.open(identity_reject, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                identity_code, identity_body = post_identity()
                assert identity_code == 400 and identity_body.get('error') == '这个抖音号已经在工作台中，请编辑已有账号', identity_code
                identity_again_code, identity_again_body = post_identity()
                assert identity_again_code == 400 and identity_again_body.get('error') == '这个抖音号已经在工作台中，请编辑已有账号', identity_again_code
                with connect() as db:
                    identity_seed_after = db.execute('SELECT douyin_id FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    identity_other_after = db.execute('SELECT douyin_id FROM accounts WHERE id=?', (identity_other_id,)).fetchone()[0]
                    identity_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (identity_reject_id,)).fetchone()
                    identity_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (identity_reject_id,)).fetchone()[0]
                    identity_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    identity_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                identity_kept = identity_seed_after == identity_before and identity_other_after == shared_identity and identity_stored[0] == 'failed' and identity_stored[1] == 400 and identity_count == 1 and identity_history_after == identity_history and identity_audits_after == identity_audits
                assert identity_kept is True, identity_code
            finally:
                with connect() as db:
                    if identity_other_id:
                        db.execute('DELETE FROM accounts WHERE id=?', (identity_other_id,))
                    db.execute("DELETE FROM accounts WHERE computer_id='synthetic-pc' AND slot=2")
                    db.execute("DELETE FROM accounts WHERE name='新账号'")
                    if identity_ready:
                        db.execute('UPDATE accounts SET douyin_id=? WHERE id=?', (identity_before, account))
            spaced_identity = 'bad id'
            spaced_id = 'b6b6b6b6-b6b6-4b6b-8b6b-b6b6b6b6b6b6'
            spaced_before = None
            spaced_ready = False
            try:
                with connect() as db:
                    spaced_before = db.execute('SELECT douyin_id FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    spaced_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    spaced_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                spaced_ready = True
                spaced_request = urllib.request.Request(base + '/api/account/patch', data=json.dumps(dict(id=account, changes=dict(douyin_id=spaced_identity), request_id=spaced_id)).encode(), headers={'Content-Type': 'application/json'})
                def post_spaced():
                    try:
                        with current_owner.open(spaced_request, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                spaced_code, spaced_body = post_spaced()
                assert spaced_code == 400 and spaced_body.get('error') == '真实抖音号不能包含空格或超过100字', spaced_code
                spaced_again_code, spaced_again_body = post_spaced()
                assert spaced_again_code == 400 and spaced_again_body.get('error') == '真实抖音号不能包含空格或超过100字', spaced_again_code
                with connect() as db:
                    spaced_after = db.execute('SELECT douyin_id FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    spaced_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (spaced_id,)).fetchone()
                    spaced_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (spaced_id,)).fetchone()[0]
                    spaced_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    spaced_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                spaced_kept = spaced_after == spaced_before and spaced_stored[0] == 'failed' and spaced_stored[1] == 400 and spaced_count == 1 and spaced_history_after == spaced_history and spaced_audits_after == spaced_audits
                assert spaced_kept is True, spaced_code
            finally:
                if spaced_ready:
                    with connect() as db:
                        db.execute('UPDATE accounts SET douyin_id=? WHERE id=?', (spaced_before, account))
            long_identity = 'longid0326' + ('a' * 91)
            long_id = 'b8b8b8b8-b8b8-4b8b-8b8b-b8b8b8b8b8b8'
            long_before = None
            long_ready = False
            try:
                long_shape = len(long_identity) == 101 and (' ' not in long_identity)
                assert long_shape is True, 0
                with connect() as db:
                    long_before = db.execute('SELECT douyin_id FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    long_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    long_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                long_ready = True
                long_request = urllib.request.Request(base + '/api/account/patch', data=json.dumps(dict(id=account, changes=dict(douyin_id=long_identity), request_id=long_id)).encode(), headers={'Content-Type': 'application/json'})
                def post_long():
                    try:
                        with current_owner.open(long_request, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                long_code, long_body = post_long()
                assert long_code == 400 and long_body.get('error') == '真实抖音号不能包含空格或超过100字', long_code
                long_again_code, long_again_body = post_long()
                assert long_again_code == 400 and long_again_body.get('error') == '真实抖音号不能包含空格或超过100字', long_again_code
                with connect() as db:
                    long_after = db.execute('SELECT douyin_id FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    long_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (long_id,)).fetchone()
                    long_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (long_id,)).fetchone()[0]
                    long_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    long_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                long_kept = long_after == long_before and long_stored[0] == 'failed' and long_stored[1] == 400 and long_count == 1 and long_history_after == long_history and long_audits_after == long_audits
                assert long_kept is True, long_code
            finally:
                if long_ready:
                    with connect() as db:
                        db.execute('UPDATE accounts SET douyin_id=? WHERE id=?', (long_before, account))
            blank_name = '   '
            blank_name_id = 'b9b9b9b9-b9b9-4b9b-8b9b-b9b9b9b9b9b9'
            blank_name_before = None
            blank_name_ready = False
            try:
                blank_name_shape = blank_name.strip() == ''
                assert blank_name_shape is True, 0
                with connect() as db:
                    blank_name_before = db.execute('SELECT name FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    blank_name_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    blank_name_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                blank_name_ready = True
                blank_name_request = urllib.request.Request(base + '/api/account/patch', data=json.dumps(dict(id=account, changes=dict(name=blank_name), request_id=blank_name_id)).encode(), headers={'Content-Type': 'application/json'})
                def post_blank_name():
                    try:
                        with current_owner.open(blank_name_request, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                blank_name_code, blank_name_body = post_blank_name()
                assert blank_name_code == 400 and blank_name_body.get('error') == '账号名称不能为空或超过100字', blank_name_code
                blank_name_again_code, blank_name_again_body = post_blank_name()
                assert blank_name_again_code == 400 and blank_name_again_body.get('error') == '账号名称不能为空或超过100字', blank_name_again_code
                with connect() as db:
                    blank_name_after = db.execute('SELECT name FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    blank_name_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (blank_name_id,)).fetchone()
                    blank_name_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (blank_name_id,)).fetchone()[0]
                    blank_name_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    blank_name_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                blank_name_kept = blank_name_after == blank_name_before and blank_name_stored[0] == 'failed' and blank_name_stored[1] == 400 and blank_name_count == 1 and blank_name_history_after == blank_name_history and blank_name_audits_after == blank_name_audits
                assert blank_name_kept is True, blank_name_code
            finally:
                if blank_name_ready:
                    with connect() as db:
                        db.execute('UPDATE accounts SET name=? WHERE id=?', (blank_name_before, account))
            long_name = 'longname0328' + ('a' * 89)
            long_name_id = 'b0b0b0b0-b0b0-4b0b-8b0b-b0b0b0b0b0b0'
            long_name_before = None
            long_name_ready = False
            try:
                long_name_shape = len(long_name) == 101 and long_name.strip() == long_name
                assert long_name_shape is True, 0
                with connect() as db:
                    long_name_before = db.execute('SELECT name FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    long_name_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    long_name_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                long_name_ready = True
                long_name_request = urllib.request.Request(base + '/api/account/patch', data=json.dumps(dict(id=account, changes=dict(name=long_name), request_id=long_name_id)).encode(), headers={'Content-Type': 'application/json'})
                def post_long_name():
                    try:
                        with current_owner.open(long_name_request, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                long_name_code, long_name_body = post_long_name()
                assert long_name_code == 400 and long_name_body.get('error') == '账号名称不能为空或超过100字', long_name_code
                long_name_again_code, long_name_again_body = post_long_name()
                assert long_name_again_code == 400 and long_name_again_body.get('error') == '账号名称不能为空或超过100字', long_name_again_code
                with connect() as db:
                    long_name_after = db.execute('SELECT name FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    long_name_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (long_name_id,)).fetchone()
                    long_name_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (long_name_id,)).fetchone()[0]
                    long_name_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    long_name_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                long_name_kept = long_name_after == long_name_before and long_name_stored[0] == 'failed' and long_name_stored[1] == 400 and long_name_count == 1 and long_name_history_after == long_name_history and long_name_audits_after == long_name_audits
                assert long_name_kept is True, long_name_code
            finally:
                if long_name_ready:
                    with connect() as db:
                        db.execute('UPDATE accounts SET name=? WHERE id=?', (long_name_before, account))
            unknown_field_id = 'a3a3a3a3-a3a3-4a3a-8a3a-a3a3a3a3a3a3'
            unknown_name_before = None
            unknown_ready = False
            try:
                with connect() as db:
                    unknown_name_before = db.execute('SELECT name FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    unknown_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    unknown_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                unknown_ready = True
                unknown_request = urllib.request.Request(base + '/api/account/patch', data=json.dumps(dict(id=account, changes={'未列出字段': 'x'}, request_id=unknown_field_id)).encode(), headers={'Content-Type': 'application/json'})
                def post_unknown():
                    try:
                        with current_owner.open(unknown_request, timeout=10) as response:
                            return response.status, json.load(response)
                    except urllib.error.HTTPError as error:
                        return error.code, json.load(error)
                unknown_code, unknown_body = post_unknown()
                assert unknown_code == 400 and unknown_body.get('error') == '包含不允许修改的字段', unknown_code
                unknown_again_code, unknown_again_body = post_unknown()
                assert unknown_again_code == 400 and unknown_again_body.get('error') == '包含不允许修改的字段', unknown_again_code
                with connect() as db:
                    unknown_name_after = db.execute('SELECT name FROM accounts WHERE id=?', (account,)).fetchone()[0]
                    unknown_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (unknown_field_id,)).fetchone()[0]
                    unknown_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                    unknown_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
                unknown_kept = unknown_name_after == unknown_name_before and unknown_count == 0 and unknown_history_after == unknown_history and unknown_audits_after == unknown_audits
                assert unknown_kept is True, unknown_code
            finally:
                if unknown_ready:
                    with connect() as db:
                        db.execute('UPDATE accounts SET name=? WHERE id=?', (unknown_name_before, account))
            office_patch_id = 'a4a4a4a4-a4a4-4a4a-8a4a-a4a4a4a4a4a4'
            with connect() as db:
                office_name_before = db.execute('SELECT name FROM accounts WHERE id=?', (account,)).fetchone()[0]
                office_links_before = db.execute('SELECT count(*) FROM account_offices WHERE account_id=?', (account,)).fetchone()[0]
                office_rows_before = db.execute('SELECT count(*) FROM offices').fetchone()[0]
                office_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                office_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
            office_patch = urllib.request.Request(base + '/api/account/patch', data=json.dumps(dict(id=account, changes={'office_id': None}, request_id=office_patch_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_office_patch():
                try:
                    with current_owner.open(office_patch, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            office_code, office_body = post_office_patch()
            assert office_code == 400 and office_body.get('error') == '当前模式未启用办公室范围', office_code
            office_again_code, office_again_body = post_office_patch()
            assert office_again_code == 400 and office_again_body.get('error') == '当前模式未启用办公室范围', office_again_code
            with connect() as db:
                office_name_after = db.execute('SELECT name FROM accounts WHERE id=?', (account,)).fetchone()[0]
                office_links_after = db.execute('SELECT count(*) FROM account_offices WHERE account_id=?', (account,)).fetchone()[0]
                office_rows_after = db.execute('SELECT count(*) FROM offices').fetchone()[0]
                office_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (office_patch_id,)).fetchone()[0]
                office_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                office_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
            office_kept = office_name_after == office_name_before and office_links_after == office_links_before and office_rows_after == office_rows_before and office_count == 0 and office_history_after == office_history and office_audits_after == office_audits
            assert office_kept is True, office_code
            slot_type_id = 'a5a5a5a5-a5a5-4a5a-8a5a-a5a5a5a5a5a5'
            with connect() as db:
                slot_before = db.execute('SELECT computer_id, slot FROM accounts WHERE id=?', (account,)).fetchone()
                slot_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                slot_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
            slot_type = urllib.request.Request(base + '/api/account/patch', data=json.dumps(dict(id=account, changes=dict(slot='2'), request_id=slot_type_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_slot_type():
                try:
                    with current_owner.open(slot_type, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            slot_code, slot_body = post_slot_type()
            assert slot_code == 400 and slot_body.get('error') == '账号位置必须为整数', slot_code
            slot_again_code, slot_again_body = post_slot_type()
            assert slot_again_code == 400 and slot_again_body.get('error') == '账号位置必须为整数', slot_again_code
            with connect() as db:
                slot_after = db.execute('SELECT computer_id, slot FROM accounts WHERE id=?', (account,)).fetchone()
                slot_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (slot_type_id,)).fetchone()[0]
                slot_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                slot_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
            slot_kept = slot_after[0] == slot_before[0] and slot_after[1] == slot_before[1] and slot_count == 0 and slot_history_after == slot_history and slot_audits_after == slot_audits
            assert slot_kept is True, slot_code
            name_type_id = 'a7a7a7a7-a7a7-4a7a-8a7a-a7a7a7a7a7a7'
            with connect() as db:
                name_type_before = db.execute('SELECT name FROM accounts WHERE id=?', (account,)).fetchone()[0]
                name_type_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                name_type_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
            name_type = urllib.request.Request(base + '/api/account/patch', data=json.dumps(dict(id=account, changes=dict(name=1), request_id=name_type_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_name_type():
                try:
                    with current_owner.open(name_type, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            name_type_code, name_type_body = post_name_type()
            assert name_type_code == 400 and name_type_body.get('error') == '字段格式不正确：name', name_type_code
            name_type_again_code, name_type_again_body = post_name_type()
            assert name_type_again_code == 400 and name_type_again_body.get('error') == '字段格式不正确：name', name_type_again_code
            with connect() as db:
                name_type_after = db.execute('SELECT name FROM accounts WHERE id=?', (account,)).fetchone()[0]
                name_type_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (name_type_id,)).fetchone()[0]
                name_type_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                name_type_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
            name_type_kept = name_type_after == name_type_before and name_type_count == 0 and name_type_history_after == name_type_history and name_type_audits_after == name_type_audits
            assert name_type_kept is True, name_type_code
            empty_changes_id = 'a8a8a8a8-a8a8-4a8a-8a8a-a8a8a8a8a8a8'
            with connect() as db:
                empty_changes_before = db.execute('SELECT name FROM accounts WHERE id=?', (account,)).fetchone()[0]
                empty_changes_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                empty_changes_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
            empty_changes = urllib.request.Request(base + '/api/account/patch', data=json.dumps(dict(id=account, changes={}, request_id=empty_changes_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_empty_changes():
                try:
                    with current_owner.open(empty_changes, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            empty_changes_code, empty_changes_body = post_empty_changes()
            assert empty_changes_code == 400 and empty_changes_body.get('error') == '请提交需要修改的字段', empty_changes_code
            empty_changes_again_code, empty_changes_again_body = post_empty_changes()
            assert empty_changes_again_code == 400 and empty_changes_again_body.get('error') == '请提交需要修改的字段', empty_changes_again_code
            with connect() as db:
                empty_changes_after = db.execute('SELECT name FROM accounts WHERE id=?', (account,)).fetchone()[0]
                empty_changes_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (empty_changes_id,)).fetchone()[0]
                empty_changes_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                empty_changes_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
            empty_changes_kept = empty_changes_after == empty_changes_before and empty_changes_count == 0 and empty_changes_history_after == empty_changes_history and empty_changes_audits_after == empty_changes_audits
            assert empty_changes_kept is True, empty_changes_code
            missing_account_id = 'a9a9a9a9-a9a9-4a9a-8a9a-a9a9a9a9a9a9'
            missing_account = 'aa000335-0335-4033-8033-033503350335'
            with connect() as db:
                missing_accounts_before = db.execute('SELECT count(*) FROM accounts').fetchone()[0]
                missing_present = db.execute('SELECT count(*) FROM accounts WHERE id=?', (missing_account,)).fetchone()[0]
                missing_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + missing_account,)).fetchone()[0]
                missing_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
            assert missing_present == 0, missing_present
            missing_patch = urllib.request.Request(base + '/api/account/patch', data=json.dumps(dict(id=missing_account, changes=dict(name='合成缺席'), request_id=missing_account_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_missing():
                try:
                    with current_owner.open(missing_patch, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            missing_code, missing_body = post_missing()
            assert missing_code == 400 and missing_body.get('error') == '账号不存在', missing_code
            missing_again_code, missing_again_body = post_missing()
            assert missing_again_code == 400 and missing_again_body.get('error') == '账号不存在', missing_again_code
            with connect() as db:
                missing_accounts_after = db.execute('SELECT count(*) FROM accounts').fetchone()[0]
                missing_present_after = db.execute('SELECT count(*) FROM accounts WHERE id=?', (missing_account,)).fetchone()[0]
                missing_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (missing_account_id,)).fetchone()
                missing_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (missing_account_id,)).fetchone()[0]
                missing_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + missing_account,)).fetchone()[0]
                missing_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
            missing_kept = missing_accounts_after == missing_accounts_before and missing_present_after == 0 and missing_stored[0] == 'failed' and missing_stored[1] == 400 and missing_count == 1 and missing_history_after == missing_history and missing_audits_after == missing_audits
            assert missing_kept is True, missing_code
            slot_range_id = 'a0a0a0a0-a0a0-4a0a-8a0a-a0a0a0a0a0a0'
            with connect() as db:
                slot_range_before = db.execute('SELECT computer_id, slot FROM accounts WHERE id=?', (account,)).fetchone()
                slot_range_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                slot_range_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
            slot_range = urllib.request.Request(base + '/api/account/patch', data=json.dumps(dict(id=account, changes=dict(slot=0), request_id=slot_range_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_slot_range():
                try:
                    with current_owner.open(slot_range, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            slot_range_code, slot_range_body = post_slot_range()
            assert slot_range_code == 400 and slot_range_body.get('error') == '电脑或账号位置不正确', slot_range_code
            slot_range_again_code, slot_range_again_body = post_slot_range()
            assert slot_range_again_code == 400 and slot_range_again_body.get('error') == '电脑或账号位置不正确', slot_range_again_code
            with connect() as db:
                slot_range_after = db.execute('SELECT computer_id, slot FROM accounts WHERE id=?', (account,)).fetchone()
                slot_range_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (slot_range_id,)).fetchone()
                slot_range_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (slot_range_id,)).fetchone()[0]
                slot_range_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                slot_range_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/patch'").fetchone()[0]
            slot_range_kept = slot_range_after[0] == slot_range_before[0] and slot_range_after[1] == slot_range_before[1] and slot_range_stored[0] == 'failed' and slot_range_stored[1] == 400 and slot_range_count == 1 and slot_range_history_after == slot_range_history and slot_range_audits_after == slot_range_audits
            assert slot_range_kept is True, slot_range_code
            office_group_id = 'acacacac-acac-4aca-8aca-acacacacacac'
            with connect() as db:
                office_group_accounts = db.execute('SELECT count(*) FROM accounts').fetchone()[0]
                office_group_placeholders = db.execute("SELECT count(*) FROM accounts WHERE name='新账号'").fetchone()[0]
                office_group_computers = db.execute('SELECT count(*) FROM computers').fetchone()[0]
                office_group_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
            office_group = urllib.request.Request(base + '/api/account/create', data=json.dumps(dict(name='分组不对', status='待确认', computer_id='unassigned', slot=4, office_group='bad-group', request_id=office_group_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_office_group():
                try:
                    with current_owner.open(office_group, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            office_group_code, office_group_body = post_office_group()
            assert office_group_code == 400 and office_group_body.get('error') == '办公室不正确', office_group_code
            office_group_again_code, office_group_again_body = post_office_group()
            assert office_group_again_code == 400 and office_group_again_body.get('error') == '办公室不正确', office_group_again_code
            with connect() as db:
                office_group_accounts_after = db.execute('SELECT count(*) FROM accounts').fetchone()[0]
                office_group_placeholders_after = db.execute("SELECT count(*) FROM accounts WHERE name='新账号'").fetchone()[0]
                office_group_computers_after = db.execute('SELECT count(*) FROM computers').fetchone()[0]
                office_group_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (office_group_id,)).fetchone()
                office_group_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (office_group_id,)).fetchone()[0]
                office_group_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/account/create'").fetchone()[0]
            office_group_kept = office_group_accounts_after == office_group_accounts and office_group_placeholders_after == office_group_placeholders and office_group_computers_after == office_group_computers and office_group_stored[0] == 'failed' and office_group_stored[1] == 400 and office_group_count == 1 and office_group_audits_after == office_group_audits
            assert office_group_kept is True, office_group_code
            missing_device_id = 'adadadad-adad-4ada-8ada-adadadadadad'
            missing_device = '999991'
            with connect() as db:
                missing_device_present = db.execute('SELECT count(*) FROM phone_devices WHERE id=?', (missing_device,)).fetchone()[0]
                missing_device_total = db.execute('SELECT count(*) FROM phone_devices').fetchone()[0]
                missing_device_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('device:' + missing_device,)).fetchone()[0]
                missing_device_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device'").fetchone()[0]
            assert missing_device_present == 0, missing_device_present
            missing_device_patch = urllib.request.Request(base + '/api/phone-device', data=json.dumps(dict(id=missing_device, state='待确认', request_id=missing_device_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_missing_device():
                try:
                    with current_owner.open(missing_device_patch, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            missing_device_code, missing_device_body = post_missing_device()
            assert missing_device_code == 400 and missing_device_body.get('error') == '手机设备不存在', missing_device_code
            missing_device_again_code, missing_device_again_body = post_missing_device()
            assert missing_device_again_code == 400 and missing_device_again_body.get('error') == '手机设备不存在', missing_device_again_code
            with connect() as db:
                missing_device_present_after = db.execute('SELECT count(*) FROM phone_devices WHERE id=?', (missing_device,)).fetchone()[0]
                missing_device_total_after = db.execute('SELECT count(*) FROM phone_devices').fetchone()[0]
                missing_device_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (missing_device_id,)).fetchone()
                missing_device_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (missing_device_id,)).fetchone()[0]
                missing_device_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('device:' + missing_device,)).fetchone()[0]
                missing_device_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device'").fetchone()[0]
            missing_device_kept = missing_device_present_after == 0 and missing_device_total_after == missing_device_total and missing_device_stored[0] == 'failed' and missing_device_stored[1] == 400 and missing_device_count == 1 and missing_device_history_after == missing_device_history and missing_device_audits_after == missing_device_audits
            assert missing_device_kept is True, missing_device_code
            bad_device_number_id = 'afafafaf-afaf-4afa-8afa-afafafafafaf'
            with connect() as db:
                bad_device_total = db.execute('SELECT count(*) FROM phone_devices').fetchone()[0]
                bad_device_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device'").fetchone()[0]
            bad_device_number = urllib.request.Request(base + '/api/phone-device', data=json.dumps(dict(id='bad-device', state='待确认', request_id=bad_device_number_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_bad_device_number():
                try:
                    with current_owner.open(bad_device_number, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            bad_device_code, bad_device_body = post_bad_device_number()
            assert bad_device_code == 400 and bad_device_body.get('error') == '手机编号请填1至6位正整数', bad_device_code
            bad_device_again_code, bad_device_again_body = post_bad_device_number()
            assert bad_device_again_code == 400 and bad_device_again_body.get('error') == '手机编号请填1至6位正整数', bad_device_again_code
            with connect() as db:
                bad_device_total_after = db.execute('SELECT count(*) FROM phone_devices').fetchone()[0]
                bad_device_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (bad_device_number_id,)).fetchone()
                bad_device_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (bad_device_number_id,)).fetchone()[0]
                bad_device_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device'").fetchone()[0]
            bad_device_kept = bad_device_total_after == bad_device_total and bad_device_stored[0] == 'failed' and bad_device_stored[1] == 400 and bad_device_count == 1 and bad_device_audits_after == bad_device_audits
            assert bad_device_kept is True, bad_device_code
            duplicate_device_id = 'bfbfbfbf-bfbf-4bfb-8bfb-bfbfbfbfbfbf'
            with connect() as db:
                duplicate_device_before = db.execute('SELECT state, name FROM phone_devices WHERE id=?', (idle_device,)).fetchone()
                duplicate_device_total = db.execute('SELECT count(*) FROM phone_devices').fetchone()[0]
                duplicate_device_link = db.execute('SELECT phone_device_id FROM accounts WHERE id=?', (account,)).fetchone()[0]
                duplicate_device_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('device:' + idle_device,)).fetchone()[0]
                duplicate_device_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device/create'").fetchone()[0]
            assert duplicate_device_before is not None, duplicate_device_total
            duplicate_device = urllib.request.Request(base + '/api/phone-device/create', data=json.dumps(dict(id=idle_device, state='待确认', request_id=duplicate_device_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_duplicate_device():
                try:
                    with current_owner.open(duplicate_device, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            duplicate_device_code, duplicate_device_body = post_duplicate_device()
            assert duplicate_device_code == 400 and duplicate_device_body.get('error') == '这个手机编号已经存在，请编辑已有设备', duplicate_device_code
            duplicate_device_again_code, duplicate_device_again_body = post_duplicate_device()
            assert duplicate_device_again_code == 400 and duplicate_device_again_body.get('error') == '这个手机编号已经存在，请编辑已有设备', duplicate_device_again_code
            with connect() as db:
                duplicate_device_after = db.execute('SELECT state, name FROM phone_devices WHERE id=?', (idle_device,)).fetchone()
                duplicate_device_total_after = db.execute('SELECT count(*) FROM phone_devices').fetchone()[0]
                duplicate_device_link_after = db.execute('SELECT phone_device_id FROM accounts WHERE id=?', (account,)).fetchone()[0]
                duplicate_device_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (duplicate_device_id,)).fetchone()
                duplicate_device_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (duplicate_device_id,)).fetchone()[0]
                duplicate_device_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('device:' + idle_device,)).fetchone()[0]
                duplicate_device_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device/create'").fetchone()[0]
            duplicate_device_kept = duplicate_device_after[0] == duplicate_device_before[0] and duplicate_device_after[1] == duplicate_device_before[1] and duplicate_device_total_after == duplicate_device_total and duplicate_device_link_after == duplicate_device_link and duplicate_device_stored[0] == 'failed' and duplicate_device_stored[1] == 400 and duplicate_device_count == 1 and duplicate_device_history_after == duplicate_device_history and duplicate_device_audits_after == duplicate_device_audits
            assert duplicate_device_kept is True, duplicate_device_code
            long_device_id = 'd0d0d0d0-d0d0-4d0d-8d0d-d0d0d0d0d0d0'
            long_device_name = 'longdev0341' + ('a' * 90)
            with connect() as db:
                long_device_before = db.execute('SELECT state, name FROM phone_devices WHERE id=?', (idle_device,)).fetchone()
                long_device_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('device:' + idle_device,)).fetchone()[0]
                long_device_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device'").fetchone()[0]
            assert long_device_before is not None and len(long_device_name) == 101, len(long_device_name)
            long_device = urllib.request.Request(base + '/api/phone-device', data=json.dumps(dict(id=idle_device, state=long_device_before[0], name=long_device_name, request_id=long_device_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_long_device():
                try:
                    with current_owner.open(long_device, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            long_device_code, long_device_body = post_long_device()
            assert long_device_code == 400 and long_device_body.get('error') == '手机资料内容过长', long_device_code
            long_device_again_code, long_device_again_body = post_long_device()
            assert long_device_again_code == 400 and long_device_again_body.get('error') == '手机资料内容过长', long_device_again_code
            with connect() as db:
                long_device_after = db.execute('SELECT state, name FROM phone_devices WHERE id=?', (idle_device,)).fetchone()
                long_device_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (long_device_id,)).fetchone()
                long_device_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (long_device_id,)).fetchone()[0]
                long_device_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('device:' + idle_device,)).fetchone()[0]
                long_device_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-device'").fetchone()[0]
            long_device_kept = long_device_after[0] == long_device_before[0] and long_device_after[1] == long_device_before[1] and long_device_stored[0] == 'failed' and long_device_stored[1] == 400 and long_device_count == 1 and long_device_history_after == long_device_history and long_device_audits_after == long_device_audits
            assert long_device_kept is True, long_device_code
            bad_card_phone_id = 'cfcfcfcf-cfcf-4cfc-8cfc-cfcfcfcfcfcf'
            with connect() as db:
                bad_card_total = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                bad_card_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            bad_card_phone = urllib.request.Request(base + '/api/phone-card', data=json.dumps(dict(phone='not-a-phone', request_id=bad_card_phone_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_bad_card_phone():
                try:
                    with current_owner.open(bad_card_phone, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            bad_card_code, bad_card_body = post_bad_card_phone()
            assert bad_card_code == 400 and bad_card_body.get('error') == '请填写11位手机号码', bad_card_code
            bad_card_again_code, bad_card_again_body = post_bad_card_phone()
            assert bad_card_again_code == 400 and bad_card_again_body.get('error') == '请填写11位手机号码', bad_card_again_code
            with connect() as db:
                bad_card_total_after = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                bad_card_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (bad_card_phone_id,)).fetchone()
                bad_card_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (bad_card_phone_id,)).fetchone()[0]
                bad_card_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            bad_card_kept = bad_card_total_after == bad_card_total and bad_card_stored[0] == 'failed' and bad_card_stored[1] == 400 and bad_card_count == 1 and bad_card_audits_after == bad_card_audits
            assert bad_card_kept is True, bad_card_code
            missing_card_request = 'dfdfdfdf-dfdf-4dfd-8dfd-dfdfdfdfdfdf'
            missing_card_phone = '13800003431'
            missing_card_key = 999999
            with connect() as db:
                missing_card_present = db.execute('SELECT count(*) FROM phone_cards WHERE id=?', (missing_card_key,)).fetchone()[0]
                missing_card_phone_present = db.execute('SELECT count(*) FROM phone_cards WHERE phone=?', (missing_card_phone,)).fetchone()[0]
                missing_card_total = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                missing_card_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            assert missing_card_present == 0 and missing_card_phone_present == 0, missing_card_present
            missing_card = urllib.request.Request(base + '/api/phone-card', data=json.dumps(dict(id=missing_card_key, phone=missing_card_phone, request_id=missing_card_request)).encode(), headers={'Content-Type': 'application/json'})
            def post_missing_card():
                try:
                    with current_owner.open(missing_card, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            missing_card_code, missing_card_body = post_missing_card()
            assert missing_card_code == 400 and missing_card_body.get('error') == '手机卡记录不存在，请刷新后重试', missing_card_code
            missing_card_again_code, missing_card_again_body = post_missing_card()
            assert missing_card_again_code == 400 and missing_card_again_body.get('error') == '手机卡记录不存在，请刷新后重试', missing_card_again_code
            with connect() as db:
                missing_card_present_after = db.execute('SELECT count(*) FROM phone_cards WHERE id=?', (missing_card_key,)).fetchone()[0]
                missing_card_phone_after = db.execute('SELECT count(*) FROM phone_cards WHERE phone=?', (missing_card_phone,)).fetchone()[0]
                missing_card_total_after = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                missing_card_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (missing_card_request,)).fetchone()
                missing_card_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (missing_card_request,)).fetchone()[0]
                missing_card_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            missing_card_kept = missing_card_present_after == 0 and missing_card_phone_after == 0 and missing_card_total_after == missing_card_total and missing_card_stored[0] == 'failed' and missing_card_stored[1] == 400 and missing_card_count == 1 and missing_card_audits_after == missing_card_audits
            assert missing_card_kept is True, missing_card_code
            bad_carrier_id = '34343434-3434-4343-8343-343434343434'
            bad_carrier_phone = '13800003441'
            with connect() as db:
                bad_carrier_total = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                bad_carrier_phone_present = db.execute('SELECT count(*) FROM phone_cards WHERE phone=?', (bad_carrier_phone,)).fetchone()[0]
                bad_carrier_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            assert bad_carrier_phone_present == 0, bad_carrier_phone_present
            bad_carrier = urllib.request.Request(base + '/api/phone-card', data=json.dumps(dict(phone=bad_carrier_phone, carrier='不是运营商', request_id=bad_carrier_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_bad_carrier():
                try:
                    with current_owner.open(bad_carrier, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            bad_carrier_code, bad_carrier_body = post_bad_carrier()
            assert bad_carrier_code == 400 and bad_carrier_body.get('error') == '运营商或手机卡状态不正确', bad_carrier_code
            bad_carrier_again_code, bad_carrier_again_body = post_bad_carrier()
            assert bad_carrier_again_code == 400 and bad_carrier_again_body.get('error') == '运营商或手机卡状态不正确', bad_carrier_again_code
            with connect() as db:
                bad_carrier_total_after = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                bad_carrier_phone_after = db.execute('SELECT count(*) FROM phone_cards WHERE phone=?', (bad_carrier_phone,)).fetchone()[0]
                bad_carrier_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (bad_carrier_id,)).fetchone()
                bad_carrier_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (bad_carrier_id,)).fetchone()[0]
                bad_carrier_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            bad_carrier_kept = bad_carrier_total_after == bad_carrier_total and bad_carrier_phone_after == 0 and bad_carrier_stored[0] == 'failed' and bad_carrier_stored[1] == 400 and bad_carrier_count == 1 and bad_carrier_audits_after == bad_carrier_audits
            assert bad_carrier_kept is True, bad_carrier_code
            bad_expiry_id = '45454545-4545-4454-8454-454545454545'
            bad_expiry_phone = '13800003451'
            with connect() as db:
                bad_expiry_total = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                bad_expiry_phone_present = db.execute('SELECT count(*) FROM phone_cards WHERE phone=?', (bad_expiry_phone,)).fetchone()[0]
                bad_expiry_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            assert bad_expiry_phone_present == 0, bad_expiry_phone_present
            bad_expiry = urllib.request.Request(base + '/api/phone-card', data=json.dumps(dict(phone=bad_expiry_phone, expires_on='not-a-date', request_id=bad_expiry_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_bad_expiry():
                try:
                    with current_owner.open(bad_expiry, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            bad_expiry_code, bad_expiry_body = post_bad_expiry()
            assert bad_expiry_code == 400 and bad_expiry_body.get('error') == '到期日期不正确', bad_expiry_code
            bad_expiry_again_code, bad_expiry_again_body = post_bad_expiry()
            assert bad_expiry_again_code == 400 and bad_expiry_again_body.get('error') == '到期日期不正确', bad_expiry_again_code
            with connect() as db:
                bad_expiry_total_after = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                bad_expiry_phone_after = db.execute('SELECT count(*) FROM phone_cards WHERE phone=?', (bad_expiry_phone,)).fetchone()[0]
                bad_expiry_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (bad_expiry_id,)).fetchone()
                bad_expiry_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (bad_expiry_id,)).fetchone()[0]
                bad_expiry_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            bad_expiry_kept = bad_expiry_total_after == bad_expiry_total and bad_expiry_phone_after == 0 and bad_expiry_stored[0] == 'failed' and bad_expiry_stored[1] == 400 and bad_expiry_count == 1 and bad_expiry_audits_after == bad_expiry_audits
            assert bad_expiry_kept is True, bad_expiry_code
            bad_ownership_id = '56565656-5656-4565-8565-565656565656'
            bad_ownership_phone = '13800003461'
            with connect() as db:
                bad_ownership_total = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                bad_ownership_phone_present = db.execute('SELECT count(*) FROM phone_cards WHERE phone=?', (bad_ownership_phone,)).fetchone()[0]
                bad_ownership_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            assert bad_ownership_phone_present == 0, bad_ownership_phone_present
            bad_ownership = urllib.request.Request(base + '/api/phone-card', data=json.dumps(dict(phone=bad_ownership_phone, ownership='不是归属', request_id=bad_ownership_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_bad_ownership():
                try:
                    with current_owner.open(bad_ownership, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            bad_ownership_code, bad_ownership_body = post_bad_ownership()
            assert bad_ownership_code == 400 and bad_ownership_body.get('error') == '归属、接码或归还状态不正确', bad_ownership_code
            bad_ownership_again_code, bad_ownership_again_body = post_bad_ownership()
            assert bad_ownership_again_code == 400 and bad_ownership_again_body.get('error') == '归属、接码或归还状态不正确', bad_ownership_again_code
            with connect() as db:
                bad_ownership_total_after = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                bad_ownership_phone_after = db.execute('SELECT count(*) FROM phone_cards WHERE phone=?', (bad_ownership_phone,)).fetchone()[0]
                bad_ownership_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (bad_ownership_id,)).fetchone()
                bad_ownership_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (bad_ownership_id,)).fetchone()[0]
                bad_ownership_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            bad_ownership_kept = bad_ownership_total_after == bad_ownership_total and bad_ownership_phone_after == 0 and bad_ownership_stored[0] == 'failed' and bad_ownership_stored[1] == 400 and bad_ownership_count == 1 and bad_ownership_audits_after == bad_ownership_audits
            assert bad_ownership_kept is True, bad_ownership_code
            returned_missing_id = '67676767-6767-4676-8676-676767676767'
            returned_missing_phone = '13800003471'
            with connect() as db:
                returned_missing_total = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                returned_missing_phone_present = db.execute('SELECT count(*) FROM phone_cards WHERE phone=?', (returned_missing_phone,)).fetchone()[0]
                returned_missing_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            assert returned_missing_phone_present == 0, returned_missing_phone_present
            returned_missing = urllib.request.Request(base + '/api/phone-card', data=json.dumps(dict(phone=returned_missing_phone, return_state='已归还', request_id=returned_missing_id)).encode(), headers={'Content-Type': 'application/json'})
            def post_returned_missing():
                try:
                    with current_owner.open(returned_missing, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            returned_missing_code, returned_missing_body = post_returned_missing()
            assert returned_missing_code == 400 and returned_missing_body.get('error') == '标记已归还时请填写实际归还日期', returned_missing_code
            returned_missing_again_code, returned_missing_again_body = post_returned_missing()
            assert returned_missing_again_code == 400 and returned_missing_again_body.get('error') == '标记已归还时请填写实际归还日期', returned_missing_again_code
            with connect() as db:
                returned_missing_total_after = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                returned_missing_phone_after = db.execute('SELECT count(*) FROM phone_cards WHERE phone=?', (returned_missing_phone,)).fetchone()[0]
                returned_missing_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (returned_missing_id,)).fetchone()
                returned_missing_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (returned_missing_id,)).fetchone()[0]
                returned_missing_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            returned_missing_kept = returned_missing_total_after == returned_missing_total and returned_missing_phone_after == 0 and returned_missing_stored[0] == 'failed' and returned_missing_stored[1] == 400 and returned_missing_count == 1 and returned_missing_audits_after == returned_missing_audits
            assert returned_missing_kept is True, returned_missing_code
            first_card_request = '78787878-7878-4787-8787-787878787878'
            duplicate_card_request = '79797979-7979-4797-8797-797979797979'
            duplicate_card_phone = '13800003481'
            with connect() as db:
                duplicate_card_total = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                duplicate_card_phone_present = db.execute('SELECT count(*) FROM phone_cards WHERE phone=?', (duplicate_card_phone,)).fetchone()[0]
                duplicate_card_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
                duplicate_card_history = db.execute('SELECT count(*) FROM operations_history').fetchone()[0]
            assert duplicate_card_phone_present == 0, duplicate_card_phone_present
            first_card = urllib.request.Request(base + '/api/phone-card', data=json.dumps(dict(phone=duplicate_card_phone, request_id=first_card_request)).encode(), headers={'Content-Type': 'application/json'})
            duplicate_card = urllib.request.Request(base + '/api/phone-card', data=json.dumps(dict(phone=duplicate_card_phone, request_id=duplicate_card_request)).encode(), headers={'Content-Type': 'application/json'})
            def post_card(request):
                try:
                    with current_owner.open(request, timeout=10) as response:
                        return response.status, json.load(response)
                except urllib.error.HTTPError as error:
                    return error.code, json.load(error)
            try:
                first_card_code, first_card_body = post_card(first_card)
                assert first_card_code == 200 and first_card_body.get('ok') is True, first_card_code
                first_card_again_code, first_card_again_body = post_card(first_card)
                assert first_card_again_code == 200 and first_card_again_body.get('ok') is True, first_card_again_code
                with connect() as db:
                    duplicate_card_after_first = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                    duplicate_card_phone_after_first = db.execute('SELECT count(*) FROM phone_cards WHERE phone=?', (duplicate_card_phone,)).fetchone()[0]
                    duplicate_card_audits_after_first = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
                    duplicate_card_history_after_first = db.execute('SELECT count(*) FROM operations_history').fetchone()[0]
                assert duplicate_card_after_first == duplicate_card_total + 1 and duplicate_card_phone_after_first == 1 and duplicate_card_audits_after_first == duplicate_card_audits + 1, first_card_code
                duplicate_card_code, duplicate_card_body = post_card(duplicate_card)
                assert duplicate_card_code == 400 and duplicate_card_body.get('error') == '这个手机号已经录入，请编辑已有手机卡', duplicate_card_code
                duplicate_card_again_code, duplicate_card_again_body = post_card(duplicate_card)
                assert duplicate_card_again_code == 400 and duplicate_card_again_body.get('error') == '这个手机号已经录入，请编辑已有手机卡', duplicate_card_again_code
                with connect() as db:
                    duplicate_card_after = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                    duplicate_card_phone_after = db.execute('SELECT count(*) FROM phone_cards WHERE phone=?', (duplicate_card_phone,)).fetchone()[0]
                    duplicate_card_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (duplicate_card_request,)).fetchone()
                    duplicate_card_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (duplicate_card_request,)).fetchone()[0]
                    duplicate_card_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
                    duplicate_card_history_after = db.execute('SELECT count(*) FROM operations_history').fetchone()[0]
                duplicate_card_kept = duplicate_card_after == duplicate_card_after_first and duplicate_card_phone_after == 1 and duplicate_card_stored[0] == 'failed' and duplicate_card_stored[1] == 400 and duplicate_card_count == 1 and duplicate_card_audits_after == duplicate_card_audits_after_first and duplicate_card_history_after == duplicate_card_history_after_first
                assert duplicate_card_kept is True, duplicate_card_code
            finally:
                with connect() as db:
                    db.execute('DELETE FROM phone_cards WHERE phone=?', (duplicate_card_phone,))
            extra_card_id = '8c8c8c8c-8c8c-48c8-88c8-8c8c8c8c8c8c'
            extra_card_phone = '13800003491'
            extra_card_keeper = 'longkeep0349' + ('a' * 89)
            with connect() as db:
                extra_card_total = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                extra_card_phone_before = db.execute('SELECT count(*) FROM phone_cards WHERE phone=?', (extra_card_phone,)).fetchone()[0]
                extra_card_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            assert len(extra_card_keeper) == 101 and extra_card_phone_before == 0, extra_card_phone_before
            extra_card = urllib.request.Request(base + '/api/phone-card', data=json.dumps(dict(phone=extra_card_phone, keeper=extra_card_keeper, request_id=extra_card_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(extra_card, timeout=10) as response:
                    extra_card_code, extra_card_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                extra_card_code, extra_card_body = error.code, json.load(error)
            assert extra_card_code == 400 and extra_card_body.get('error') == '接码或负责人信息过长，请缩短后保存', extra_card_code
            try:
                with current_owner.open(extra_card, timeout=10) as response:
                    extra_card_again_code, extra_card_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                extra_card_again_code, extra_card_again_body = error.code, json.load(error)
            assert extra_card_again_code == 400 and extra_card_again_body.get('error') == '接码或负责人信息过长，请缩短后保存', extra_card_again_code
            with connect() as db:
                extra_card_after = db.execute('SELECT count(*) FROM phone_cards').fetchone()[0]
                extra_card_phone_after = db.execute('SELECT count(*) FROM phone_cards WHERE phone=?', (extra_card_phone,)).fetchone()[0]
                extra_card_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (extra_card_id,)).fetchone()
                extra_card_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (extra_card_id,)).fetchone()[0]
                extra_card_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/phone-card'").fetchone()[0]
            extra_card_kept = extra_card_after == extra_card_total and extra_card_phone_after == 0 and extra_card_stored[0] == 'failed' and extra_card_stored[1] == 400 and extra_card_count == 1 and extra_card_audits_after == extra_card_audits
            assert extra_card_kept is True, extra_card_code
            text_batch_id = '91919191-9191-4919-8919-919191919191'
            with connect() as db:
                text_batch_status = db.execute('SELECT status FROM accounts WHERE id=?', (account,)).fetchone()[0]
                text_batch_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
                text_batch_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
            assert text_batch_status is not None, text_batch_status
            text_batch = urllib.request.Request(base + '/api/batch-edit', data=json.dumps(dict(kind='accounts', ids=[str(account)], patch={'status': 1}, request_id=text_batch_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(text_batch, timeout=10) as response:
                    text_batch_code, text_batch_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                text_batch_code, text_batch_body = error.code, json.load(error)
            assert text_batch_code == 400 and text_batch_body.get('error') == '字段应为文本', text_batch_code
            try:
                with current_owner.open(text_batch, timeout=10) as response:
                    text_batch_again_code, text_batch_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                text_batch_again_code, text_batch_again_body = error.code, json.load(error)
            assert text_batch_again_code == 400 and text_batch_again_body.get('error') == '字段应为文本', text_batch_again_code
            with connect() as db:
                text_batch_status_after = db.execute('SELECT status FROM accounts WHERE id=?', (account,)).fetchone()[0]
                text_batch_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
                text_batch_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                text_batch_saved = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (text_batch_id,)).fetchone()[0]
            text_batch_kept = text_batch_status_after == text_batch_status and text_batch_audits_after == text_batch_audits and text_batch_history_after == text_batch_history and text_batch_saved == 0
            assert text_batch_kept is True, text_batch_code
            kind_batch_id = '92929292-9292-4929-8929-929292929292'
            with connect() as db:
                kind_batch_status = db.execute('SELECT status FROM accounts WHERE id=?', (account,)).fetchone()[0]
                kind_batch_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
                kind_batch_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
            assert kind_batch_status is not None, kind_batch_status
            kind_batch = urllib.request.Request(base + '/api/batch-edit', data=json.dumps(dict(kind='not-batch', ids=[str(account)], patch={'status': 1}, request_id=kind_batch_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(kind_batch, timeout=10) as response:
                    kind_batch_code, kind_batch_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                kind_batch_code, kind_batch_body = error.code, json.load(error)
            assert kind_batch_code == 400 and kind_batch_body.get('error') == '批量修改类型不正确', kind_batch_code
            try:
                with current_owner.open(kind_batch, timeout=10) as response:
                    kind_batch_again_code, kind_batch_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                kind_batch_again_code, kind_batch_again_body = error.code, json.load(error)
            assert kind_batch_again_code == 400 and kind_batch_again_body.get('error') == '批量修改类型不正确', kind_batch_again_code
            with connect() as db:
                kind_batch_status_after = db.execute('SELECT status FROM accounts WHERE id=?', (account,)).fetchone()[0]
                kind_batch_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
                kind_batch_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                kind_batch_saved = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (kind_batch_id,)).fetchone()[0]
            kind_batch_kept = kind_batch_status_after == kind_batch_status and kind_batch_audits_after == kind_batch_audits and kind_batch_history_after == kind_batch_history and kind_batch_saved == 0
            assert kind_batch_kept is True, kind_batch_code
            selection_batch_id = '94949494-9494-4949-8949-949494949494'
            with connect() as db:
                selection_batch_status = db.execute('SELECT status FROM accounts WHERE id=?', (account,)).fetchone()[0]
                selection_batch_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
                selection_batch_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
            assert selection_batch_status is not None, selection_batch_status
            selection_batch = urllib.request.Request(base + '/api/batch-edit', data=json.dumps(dict(kind='accounts', ids=[], patch={'status': selection_batch_status}, request_id=selection_batch_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(selection_batch, timeout=10) as response:
                    selection_batch_code, selection_batch_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                selection_batch_code, selection_batch_body = error.code, json.load(error)
            assert selection_batch_code == 400 and selection_batch_body.get('error') == '请选择1至2000条不重复的记录', selection_batch_code
            try:
                with current_owner.open(selection_batch, timeout=10) as response:
                    selection_batch_again_code, selection_batch_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                selection_batch_again_code, selection_batch_again_body = error.code, json.load(error)
            assert selection_batch_again_code == 400 and selection_batch_again_body.get('error') == '请选择1至2000条不重复的记录', selection_batch_again_code
            with connect() as db:
                selection_batch_status_after = db.execute('SELECT status FROM accounts WHERE id=?', (account,)).fetchone()[0]
                selection_batch_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
                selection_batch_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                selection_batch_saved = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (selection_batch_id,)).fetchone()[0]
            selection_batch_kept = selection_batch_status_after == selection_batch_status and selection_batch_audits_after == selection_batch_audits and selection_batch_history_after == selection_batch_history and selection_batch_saved == 0
            assert selection_batch_kept is True, selection_batch_code
            daily_batch_id = '96969696-9696-4969-8969-969696969696'
            with connect() as db:
                daily_batch_status = db.execute('SELECT status FROM accounts WHERE id=?', (account,)).fetchone()[0]
                daily_batch_rows = db.execute('SELECT count(*) FROM daily WHERE account_id=?', (account,)).fetchone()[0]
                daily_batch_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
                daily_batch_history = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
            assert daily_batch_status is not None, daily_batch_status
            daily_batch = urllib.request.Request(base + '/api/batch-edit', data=json.dumps(dict(kind='daily', ids=[str(account)], patch={'published': 'not-bool', 'date': 'not-a-day'}, request_id=daily_batch_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(daily_batch, timeout=10) as response:
                    daily_batch_code, daily_batch_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                daily_batch_code, daily_batch_body = error.code, json.load(error)
            assert daily_batch_code == 400 and daily_batch_body.get('error') == '请选择发布日期及已发/未发', daily_batch_code
            try:
                with current_owner.open(daily_batch, timeout=10) as response:
                    daily_batch_again_code, daily_batch_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                daily_batch_again_code, daily_batch_again_body = error.code, json.load(error)
            assert daily_batch_again_code == 400 and daily_batch_again_body.get('error') == '请选择发布日期及已发/未发', daily_batch_again_code
            with connect() as db:
                daily_batch_status_after = db.execute('SELECT status FROM accounts WHERE id=?', (account,)).fetchone()[0]
                daily_batch_rows_after = db.execute('SELECT count(*) FROM daily WHERE account_id=?', (account,)).fetchone()[0]
                daily_batch_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/batch-edit'").fetchone()[0]
                daily_batch_history_after = db.execute('SELECT count(*) FROM operations_history WHERE entity=?', ('account:' + account,)).fetchone()[0]
                daily_batch_saved = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (daily_batch_id,)).fetchone()[0]
            daily_batch_kept = daily_batch_status_after == daily_batch_status and daily_batch_rows_after == daily_batch_rows and daily_batch_audits_after == daily_batch_audits and daily_batch_history_after == daily_batch_history and daily_batch_saved == 0
            assert daily_batch_kept is True, daily_batch_code
            user_login_id = '97979797-9797-4979-8979-979797979797'
            with connect() as db:
                user_login_total = db.execute('SELECT count(*) FROM auth_users').fetchone()[0]
                user_login_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='新增人员'").fetchone()[0]
            user_login = urllib.request.Request(base + '/api/auth/users', data=json.dumps(dict(username='bad login', name='person0354', role='employee', request_id=user_login_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(user_login, timeout=10) as response:
                    user_login_code, user_login_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                user_login_code, user_login_body = error.code, json.load(error)
            assert user_login_code == 400 and user_login_body.get('error') == '登录名请使用3至40位字母、数字、点、横线或下划线', user_login_code
            try:
                with current_owner.open(user_login, timeout=10) as response:
                    user_login_again_code, user_login_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                user_login_again_code, user_login_again_body = error.code, json.load(error)
            assert user_login_again_code == 400 and user_login_again_body.get('error') == '登录名请使用3至40位字母、数字、点、横线或下划线', user_login_again_code
            with connect() as db:
                user_login_total_after = db.execute('SELECT count(*) FROM auth_users').fetchone()[0]
                user_login_named = db.execute('SELECT count(*) FROM auth_users WHERE name=?', ('person0354',)).fetchone()[0]
                user_login_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (user_login_id,)).fetchone()
                user_login_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (user_login_id,)).fetchone()[0]
                user_login_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='新增人员'").fetchone()[0]
            user_login_kept = user_login_total_after == user_login_total and user_login_named == 0 and user_login_stored[0] == 'failed' and user_login_stored[1] == 400 and user_login_count == 1 and user_login_audits_after == user_login_audits
            assert user_login_kept is True, user_login_code
            user_name_id = '9d9d9d9d-9d9d-49d9-89d9-9d9d9d9d9d9d'
            user_name_value = 'longuser0355' + ('a' * 49)
            with connect() as db:
                user_name_total = db.execute('SELECT count(*) FROM auth_users').fetchone()[0]
                user_name_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='新增人员'").fetchone()[0]
            assert len(user_name_value) == 61, len(user_name_value)
            user_name = urllib.request.Request(base + '/api/auth/users', data=json.dumps(dict(username='user0355', name=user_name_value, role='employee', request_id=user_name_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(user_name, timeout=10) as response:
                    user_name_code, user_name_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                user_name_code, user_name_body = error.code, json.load(error)
            assert user_name_code == 400 and user_name_body.get('error') == '姓名不能超过60字', user_name_code
            try:
                with current_owner.open(user_name, timeout=10) as response:
                    user_name_again_code, user_name_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                user_name_again_code, user_name_again_body = error.code, json.load(error)
            assert user_name_again_code == 400 and user_name_again_body.get('error') == '姓名不能超过60字', user_name_again_code
            with connect() as db:
                user_name_total_after = db.execute('SELECT count(*) FROM auth_users').fetchone()[0]
                user_name_named = db.execute('SELECT count(*) FROM auth_users WHERE username=?', ('user0355',)).fetchone()[0]
                user_name_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (user_name_id,)).fetchone()
                user_name_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (user_name_id,)).fetchone()[0]
                user_name_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='新增人员'").fetchone()[0]
            user_name_kept = user_name_total_after == user_name_total and user_name_named == 0 and user_name_stored[0] == 'failed' and user_name_stored[1] == 400 and user_name_count == 1 and user_name_audits_after == user_name_audits
            assert user_name_kept is True, user_name_code
            user_password_id = '9e9e9e9e-9e9e-49e9-89e9-9e9e9e9e9e9e'
            with connect() as db:
                user_password_total = db.execute('SELECT count(*) FROM auth_users').fetchone()[0]
                user_password_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='新增人员'").fetchone()[0]
            user_password = urllib.request.Request(base + '/api/auth/users', data=json.dumps(dict(username='user0356', name='person0356', role='employee', request_id=user_password_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(user_password, timeout=10) as response:
                    user_password_code, user_password_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                user_password_code, user_password_body = error.code, json.load(error)
            assert user_password_code == 400 and user_password_body.get('error') == '请设置初始密码（10至128位）', user_password_code
            try:
                with current_owner.open(user_password, timeout=10) as response:
                    user_password_again_code, user_password_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                user_password_again_code, user_password_again_body = error.code, json.load(error)
            assert user_password_again_code == 400 and user_password_again_body.get('error') == '请设置初始密码（10至128位）', user_password_again_code
            with connect() as db:
                user_password_total_after = db.execute('SELECT count(*) FROM auth_users').fetchone()[0]
                user_password_named = db.execute('SELECT count(*) FROM auth_users WHERE username=?', ('user0356',)).fetchone()[0]
                user_password_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (user_password_id,)).fetchone()
                user_password_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (user_password_id,)).fetchone()[0]
                user_password_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='新增人员'").fetchone()[0]
            user_password_kept = user_password_total_after == user_password_total and user_password_named == 0 and user_password_stored[0] == 'failed' and user_password_stored[1] == 400 and user_password_count == 1 and user_password_audits_after == user_password_audits
            assert user_password_kept is True, user_password_code
            user_role_id = '9f9f9f9f-9f9f-49f9-89f9-9f9f9f9f9f9f'
            with connect() as db:
                user_role_total = db.execute('SELECT count(*) FROM auth_users').fetchone()[0]
                user_role_owners = db.execute("SELECT count(*) FROM auth_users WHERE role='owner'").fetchone()[0]
                user_role_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='新增人员'").fetchone()[0]
            user_role = urllib.request.Request(base + '/api/auth/users', data=json.dumps(dict(username='user0357', name='person0357', role='owner', request_id=user_role_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(user_role, timeout=10) as response:
                    user_role_code, user_role_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                user_role_code, user_role_body = error.code, json.load(error)
            assert user_role_code == 400 and user_role_body.get('error') == '角色或权限不正确；不能新增总管理员', user_role_code
            try:
                with current_owner.open(user_role, timeout=10) as response:
                    user_role_again_code, user_role_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                user_role_again_code, user_role_again_body = error.code, json.load(error)
            assert user_role_again_code == 400 and user_role_again_body.get('error') == '角色或权限不正确；不能新增总管理员', user_role_again_code
            with connect() as db:
                user_role_total_after = db.execute('SELECT count(*) FROM auth_users').fetchone()[0]
                user_role_named = db.execute("SELECT count(*) FROM auth_users WHERE role='owner'").fetchone()[0]
                user_role_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (user_role_id,)).fetchone()
                user_role_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (user_role_id,)).fetchone()[0]
                user_role_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='新增人员'").fetchone()[0]
            user_role_kept = user_role_total_after == user_role_total and user_role_named == user_role_owners and user_role_stored[0] == 'failed' and user_role_stored[1] == 400 and user_role_count == 1 and user_role_audits_after == user_role_audits
            assert user_role_kept is True, user_role_code
            user_active_id = '8e8e8e8e-8e8e-48e8-88e8-8e8e8e8e8e8e'
            with connect() as db:
                user_active_total = db.execute('SELECT count(*) FROM auth_users').fetchone()[0]
                user_active_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='新增人员'").fetchone()[0]
            user_active = urllib.request.Request(base + '/api/auth/users', data=json.dumps(dict(username='user0358', name='person0358', role='employee', active=1, request_id=user_active_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(user_active, timeout=10) as response:
                    user_active_code, user_active_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                user_active_code, user_active_body = error.code, json.load(error)
            assert user_active_code == 400 and user_active_body.get('error') == '启用状态不正确', user_active_code
            try:
                with current_owner.open(user_active, timeout=10) as response:
                    user_active_again_code, user_active_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                user_active_again_code, user_active_again_body = error.code, json.load(error)
            assert user_active_again_code == 400 and user_active_again_body.get('error') == '启用状态不正确', user_active_again_code
            with connect() as db:
                user_active_total_after = db.execute('SELECT count(*) FROM auth_users').fetchone()[0]
                user_active_named = db.execute('SELECT count(*) FROM auth_users WHERE username=?', ('user0358',)).fetchone()[0]
                user_active_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (user_active_id,)).fetchone()
                user_active_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (user_active_id,)).fetchone()[0]
                user_active_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='新增人员'").fetchone()[0]
            user_active_kept = user_active_total_after == user_active_total and user_active_named == 0 and user_active_stored[0] == 'failed' and user_active_stored[1] == 400 and user_active_count == 1 and user_active_audits_after == user_active_audits
            assert user_active_kept is True, user_active_code
            missing_user_id = '8d8d8d8d-8d8d-48d8-88d8-8d8d8d8d8d8d'
            with connect() as db:
                missing_user_total = db.execute('SELECT count(*) FROM auth_users').fetchone()[0]
                missing_user_row = db.execute('SELECT count(*) FROM auth_users WHERE id=?', (999992,)).fetchone()[0]
                missing_user_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action IN ('新增人员','更新人员与权限')").fetchone()[0]
            assert missing_user_row == 0, missing_user_row
            missing_user = urllib.request.Request(base + '/api/auth/users', data=json.dumps(dict(id=999992, username='user0359', name='person0359', role='employee', active=True, request_id=missing_user_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(missing_user, timeout=10) as response:
                    missing_user_code, missing_user_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                missing_user_code, missing_user_body = error.code, json.load(error)
            assert missing_user_code == 400 and missing_user_body.get('error') == '人员不存在', missing_user_code
            try:
                with current_owner.open(missing_user, timeout=10) as response:
                    missing_user_again_code, missing_user_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                missing_user_again_code, missing_user_again_body = error.code, json.load(error)
            assert missing_user_again_code == 400 and missing_user_again_body.get('error') == '人员不存在', missing_user_again_code
            with connect() as db:
                missing_user_total_after = db.execute('SELECT count(*) FROM auth_users').fetchone()[0]
                missing_user_row_after = db.execute('SELECT count(*) FROM auth_users WHERE id=?', (999992,)).fetchone()[0]
                missing_user_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (missing_user_id,)).fetchone()
                missing_user_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (missing_user_id,)).fetchone()[0]
                missing_user_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action IN ('新增人员','更新人员与权限')").fetchone()[0]
            missing_user_kept = missing_user_total_after == missing_user_total and missing_user_row_after == 0 and missing_user_stored[0] == 'failed' and missing_user_stored[1] == 400 and missing_user_count == 1 and missing_user_audits_after == missing_user_audits
            assert missing_user_kept is True, missing_user_code
            owner_edit_id = '7e7e7e7e-7e7e-47e7-87e7-7e7e7e7e7e7e'
            with connect() as db:
                owner_edit_before = db.execute("SELECT id, role, name, active FROM auth_users WHERE username='test_owner'").fetchone()
                owner_edit_total = db.execute('SELECT count(*) FROM auth_users').fetchone()[0]
                owner_edit_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='更新人员与权限'").fetchone()[0]
            assert owner_edit_before is not None and owner_edit_before[1] == 'owner', owner_edit_total
            owner_edit = urllib.request.Request(base + '/api/auth/users', data=json.dumps(dict(id=owner_edit_before[0], username='user0360', name='person0360', role='employee', active=True, request_id=owner_edit_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(owner_edit, timeout=10) as response:
                    owner_edit_code, owner_edit_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                owner_edit_code, owner_edit_body = error.code, json.load(error)
            assert owner_edit_code == 403 and owner_edit_body.get('error') == '总管理员不能被修改角色、停用或由人员管理重置密码', owner_edit_code
            try:
                with current_owner.open(owner_edit, timeout=10) as response:
                    owner_edit_again_code, owner_edit_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                owner_edit_again_code, owner_edit_again_body = error.code, json.load(error)
            assert owner_edit_again_code == 403 and owner_edit_again_body.get('error') == '总管理员不能被修改角色、停用或由人员管理重置密码', owner_edit_again_code
            with connect() as db:
                owner_edit_after = db.execute("SELECT id, role, name, active FROM auth_users WHERE username='test_owner'").fetchone()
                owner_edit_total_after = db.execute('SELECT count(*) FROM auth_users').fetchone()[0]
                owner_edit_named = db.execute('SELECT count(*) FROM auth_users WHERE username=?', ('user0360',)).fetchone()[0]
                owner_edit_stored = db.execute('SELECT state, result_status, result_json IS NULL FROM idempotency_records WHERE request_id=?', (owner_edit_id,)).fetchone()
                owner_edit_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (owner_edit_id,)).fetchone()[0]
                owner_edit_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='更新人员与权限'").fetchone()[0]
            owner_edit_kept = owner_edit_after[1] == owner_edit_before[1] and owner_edit_after[2] == owner_edit_before[2] and owner_edit_after[3] == owner_edit_before[3] and owner_edit_total_after == owner_edit_total and owner_edit_named == 0 and owner_edit_stored[0] == 'pending' and owner_edit_stored[1] is None and owner_edit_stored[2] is True and owner_edit_count == 1 and owner_edit_audits_after == owner_edit_audits
            assert owner_edit_kept is True, owner_edit_code
            duplicate_login_id = '6d6d6d6d-6d6d-46d6-86d6-6d6d6d6d6d6d'
            duplicate_login_again_id = '5d5d5d5d-5d5d-45d5-85d5-5d5d5d5d5d5d'
            duplicate_login_secret = 'synthetic-user-0361-pass'
            with connect() as db:
                duplicate_login_total = db.execute('SELECT count(*) FROM auth_users').fetchone()[0]
                duplicate_login_named = db.execute('SELECT count(*) FROM auth_users WHERE username=?', ('user0361',)).fetchone()[0]
                duplicate_login_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='新增人员'").fetchone()[0]
            assert duplicate_login_named == 0, duplicate_login_total
            duplicate_login = urllib.request.Request(base + '/api/auth/users', data=json.dumps(dict(username='user0361', name='person0361', role='employee', active=True, password=duplicate_login_secret, request_id=duplicate_login_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(duplicate_login, timeout=10) as response:
                    duplicate_login_code, duplicate_login_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                duplicate_login_code, duplicate_login_body = error.code, json.load(error)
            assert duplicate_login_code == 200 and duplicate_login_body.get('ok') is True and 'password' not in duplicate_login_body, duplicate_login_code
            try:
                with current_owner.open(duplicate_login, timeout=10) as response:
                    duplicate_login_replay_code, duplicate_login_replay_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                duplicate_login_replay_code, duplicate_login_replay_body = error.code, json.load(error)
            assert duplicate_login_replay_code == 200 and duplicate_login_replay_body.get('ok') is True and 'password' not in duplicate_login_replay_body, duplicate_login_replay_code
            duplicate_login_taken = urllib.request.Request(base + '/api/auth/users', data=json.dumps(dict(username='user0361', name='person0361', role='employee', active=True, password=duplicate_login_secret, request_id=duplicate_login_again_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(duplicate_login_taken, timeout=10) as response:
                    duplicate_login_taken_code, duplicate_login_taken_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                duplicate_login_taken_code, duplicate_login_taken_body = error.code, json.load(error)
            assert duplicate_login_taken_code == 400 and duplicate_login_taken_body.get('error') == '登录名已存在', duplicate_login_taken_code
            try:
                with current_owner.open(duplicate_login_taken, timeout=10) as response:
                    duplicate_login_taken_again_code, duplicate_login_taken_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                duplicate_login_taken_again_code, duplicate_login_taken_again_body = error.code, json.load(error)
            assert duplicate_login_taken_again_code == 400 and duplicate_login_taken_again_body.get('error') == '登录名已存在', duplicate_login_taken_again_code
            with connect() as db:
                duplicate_login_total_after = db.execute('SELECT count(*) FROM auth_users').fetchone()[0]
                duplicate_login_named_after = db.execute('SELECT count(*) FROM auth_users WHERE username=?', ('user0361',)).fetchone()[0]
                duplicate_login_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (duplicate_login_id,)).fetchone()
                duplicate_login_taken_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (duplicate_login_again_id,)).fetchone()
                duplicate_login_taken_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (duplicate_login_again_id,)).fetchone()[0]
                duplicate_login_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='新增人员'").fetchone()[0]
                duplicate_login_secret_stored = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id IN (?, ?) AND result_json LIKE ?', (duplicate_login_id, duplicate_login_again_id, '%synthetic-user-0361-pass%')).fetchone()[0]
                db.execute('DELETE FROM auth_sessions WHERE user_id IN (SELECT id FROM auth_users WHERE username=?)', ('user0361',))
                db.execute('DELETE FROM auth_users WHERE username=?', ('user0361',))
            duplicate_login_kept = duplicate_login_total_after == duplicate_login_total + 1 and duplicate_login_named_after == 1 and duplicate_login_stored[0] == 'succeeded' and duplicate_login_stored[1] == 200 and duplicate_login_taken_stored[0] == 'failed' and duplicate_login_taken_stored[1] == 400 and duplicate_login_taken_count == 1 and duplicate_login_audits_after == duplicate_login_audits + 1 and duplicate_login_secret_stored == 0
            assert duplicate_login_kept is True, duplicate_login_taken_code
            rename_id = '3d3d3d3d-3d3d-43d3-83d3-3d3d3d3d3d3d'
            with connect() as db:
                rename_before = db.execute("SELECT id, username, name, role, active FROM auth_users WHERE username='synthetic_peer'").fetchone()
                rename_sessions = db.execute('SELECT count(*) FROM auth_sessions WHERE user_id=?', (rename_before[0],)).fetchone()[0]
                rename_owner_sessions = db.execute("SELECT count(*) FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id WHERE u.username='test_owner'").fetchone()[0]
                rename_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='更新人员与权限'").fetchone()[0]
            assert rename_before is not None and rename_sessions >= 1 and rename_before[2] != 'person0362', rename_sessions
            peer_me = urllib.request.Request(base + '/api/auth/me')
            try:
                with other_client.open(peer_me, timeout=10) as response:
                    peer_me_code, peer_me_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                peer_me_code, peer_me_body = error.code, json.load(error)
            peer_me_alive = peer_me_code == 200 and isinstance(peer_me_body.get('user'), dict) and peer_me_body['user'].get('username') == 'synthetic_peer'
            assert peer_me_alive is True, peer_me_code
            rename_user = urllib.request.Request(base + '/api/auth/users', data=json.dumps(dict(id=rename_before[0], username=rename_before[1], name='person0362', role='employee', active=True, request_id=rename_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(rename_user, timeout=10) as response:
                    rename_code, rename_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                rename_code, rename_body = error.code, json.load(error)
            assert rename_code == 200 and rename_body.get('ok') is True and 'password' not in rename_body, rename_code
            try:
                with other_client.open(urllib.request.Request(base + '/api/auth/me'), timeout=10) as response:
                    renamed_session_code, renamed_me_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                renamed_session_code, renamed_me_body = error.code, json.load(error)
            try:
                with current_owner.open(rename_user, timeout=10) as response:
                    rename_again_code, rename_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                rename_again_code, rename_again_body = error.code, json.load(error)
            assert rename_again_code == 200 and rename_again_body.get('ok') is True and 'password' not in rename_again_body, rename_again_code
            with connect() as db:
                rename_after = db.execute("SELECT id, username, name, role, active FROM auth_users WHERE id=?", (rename_before[0],)).fetchone()
                rename_sessions_after = db.execute('SELECT count(*) FROM auth_sessions WHERE user_id=?', (rename_before[0],)).fetchone()[0]
                rename_owner_sessions_after = db.execute("SELECT count(*) FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id WHERE u.username='test_owner'").fetchone()[0]
                rename_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='更新人员与权限'").fetchone()[0]
                rename_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (rename_id,)).fetchone()
                rename_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (rename_id,)).fetchone()[0]
            rename_kept = renamed_session_code == 200 and renamed_me_body.get('user') is None and rename_after[1] == rename_before[1] and rename_after[2] == 'person0362' and rename_after[2] != rename_before[2] and rename_after[3] == rename_before[3] and rename_after[4] == rename_before[4] and rename_sessions_after == 0 and rename_owner_sessions_after == rename_owner_sessions and rename_audits_after == rename_audits + 1 and rename_stored[0] == 'succeeded' and rename_stored[1] == 200 and rename_count == 1
            assert rename_kept is True, (rename_code, renamed_session_code, renamed_me_body.get('user') is None)
            slot_id = '2c2c2c2c-2c2c-42c2-82c2-2c2c2c2c2c2c'
            with connect() as db:
                slot_state = db.execute('SELECT count(*) FROM publishing_layout_state').fetchone()[0]
                slot_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                slot_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment'").fetchone()[0]
                assert slot_state == 0 and slot_cells == 0, slot_state
                db.execute("INSERT INTO publishing_layout_state(id,batch) VALUES(1,'slot-batch-0364')")
            slot_move = urllib.request.Request(base + '/api/publishing/assignment', data=json.dumps(dict(batch='slot-batch-0364', computer_id='synthetic-pc', slot='not-a-slot', action='remove', request_id=slot_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(slot_move, timeout=10) as response:
                    slot_code, slot_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                slot_code, slot_body = error.code, json.load(error)
            assert slot_code == 400 and slot_body.get('error') == '发布位置不正确', slot_code
            try:
                with current_owner.open(slot_move, timeout=10) as response:
                    slot_again_code, slot_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                slot_again_code, slot_again_body = error.code, json.load(error)
            assert slot_again_code == 400 and slot_again_body.get('error') == '发布位置不正确', slot_again_code
            with connect() as db:
                slot_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                slot_batch_after = db.execute('SELECT batch FROM publishing_layout_state WHERE id=1').fetchone()[0]
                slot_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (slot_id,)).fetchone()
                slot_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (slot_id,)).fetchone()[0]
                slot_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment'").fetchone()[0]
            slot_kept = slot_cells_after == 0 and slot_batch_after == 'slot-batch-0364' and slot_stored[0] == 'failed' and slot_stored[1] == 400 and slot_count == 1 and slot_audits_after == slot_audits
            assert slot_kept is True, slot_code
            source_id = '1b1b1b1b-1b1b-41b1-81b1-1b1b1b1b1b1b'
            with connect() as db:
                source_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                source_batch = db.execute('SELECT batch FROM publishing_layout_state WHERE id=1').fetchone()
                source_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment'").fetchone()[0]
            assert source_batch is not None and source_batch[0] == 'slot-batch-0364' and source_cells == 0, source_cells
            source_move = urllib.request.Request(base + '/api/publishing/assignment', data=json.dumps(dict(batch='slot-batch-0364', computer_id='synthetic-pc', slot=1, action='remove', request_id=source_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(source_move, timeout=10) as response:
                    source_code, source_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                source_code, source_body = error.code, json.load(error)
            assert source_code == 400 and source_body.get('error') == '请选择已关联账号的发布位置', source_code
            try:
                with current_owner.open(source_move, timeout=10) as response:
                    source_again_code, source_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                source_again_code, source_again_body = error.code, json.load(error)
            assert source_again_code == 400 and source_again_body.get('error') == '请选择已关联账号的发布位置', source_again_code
            with connect() as db:
                source_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                source_batch_after = db.execute('SELECT batch FROM publishing_layout_state WHERE id=1').fetchone()[0]
                source_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (source_id,)).fetchone()
                source_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (source_id,)).fetchone()[0]
                source_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment'").fetchone()[0]
            source_kept = source_cells_after == 0 and source_batch_after == 'slot-batch-0364' and source_stored[0] == 'failed' and source_stored[1] == 400 and source_count == 1 and source_audits_after == source_audits
            assert source_kept is True, source_code
            target_id = '1a1a1a1a-1a1a-41a1-81a1-1a1a1a1a1a1a'
            with connect() as db:
                target_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                target_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment'").fetchone()[0]
                assert target_cells == 0, target_cells
                db.execute("INSERT INTO publishing_layout(batch,computer_id,slot,account_id,label,office,source_cell,source_url,reason) VALUES('slot-batch-0364','synthetic-pc',2,?,?,?,?,?,?)", (account, '', '', '', '', ''))
            target_move = urllib.request.Request(base + '/api/publishing/assignment', data=json.dumps(dict(batch='slot-batch-0364', computer_id='synthetic-pc', slot=2, action='move', target_computer='synthetic-pc', target_slot='not-a-target', request_id=target_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(target_move, timeout=10) as response:
                    target_code, target_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                target_code, target_body = error.code, json.load(error)
            assert target_code == 400 and target_body.get('error') == '目标位置不正确', target_code
            try:
                with current_owner.open(target_move, timeout=10) as response:
                    target_again_code, target_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                target_again_code, target_again_body = error.code, json.load(error)
            assert target_again_code == 400 and target_again_body.get('error') == '目标位置不正确', target_again_code
            with connect() as db:
                target_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                target_row = db.execute("SELECT account_id, slot FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                target_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (target_id,)).fetchone()
                target_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (target_id,)).fetchone()[0]
                target_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment'").fetchone()[0]
            target_kept = target_cells_after == 1 and target_row is not None and target_row[0] == account and target_row[1] == 2 and target_stored[0] == 'failed' and target_stored[1] == 400 and target_count == 1 and target_audits_after == target_audits
            assert target_kept is True, target_code
            blank_id = '0c0c0c0c-0c0c-40c0-80c0-0c0c0c0c0c0c'
            with connect() as db:
                blank_before = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                blank_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                blank_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment'").fetchone()[0]
            assert blank_before is not None and blank_before[0] == account and blank_cells == 1, blank_cells
            blank_move = urllib.request.Request(base + '/api/publishing/assignment', data=json.dumps(dict(batch='slot-batch-0364', computer_id='synthetic-pc', slot=2, action='move', target_computer='synthetic-pc', target_slot=3, request_id=blank_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(blank_move, timeout=10) as response:
                    blank_code, blank_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                blank_code, blank_body = error.code, json.load(error)
            assert blank_code == 409 and blank_body.get('error') == '目标不是空白发布格，请选择空白位置', blank_code
            try:
                with current_owner.open(blank_move, timeout=10) as response:
                    blank_again_code, blank_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                blank_again_code, blank_again_body = error.code, json.load(error)
            assert blank_again_code == 409 and blank_again_body.get('error') == '目标不是空白发布格，请选择空白位置', blank_again_code
            with connect() as db:
                blank_after = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                blank_missing = db.execute("SELECT count(*) FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=3").fetchone()[0]
                blank_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                blank_stored = db.execute('SELECT state, result_status, result_json IS NULL FROM idempotency_records WHERE request_id=?', (blank_id,)).fetchone()
                blank_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (blank_id,)).fetchone()[0]
                blank_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment'").fetchone()[0]
            blank_kept = blank_after is not None and blank_after[0] == account and blank_missing == 0 and blank_cells_after == 1 and blank_stored[0] == 'pending' and blank_stored[1] is None and blank_stored[2] is True and blank_count == 1 and blank_audits_after == blank_audits
            assert blank_kept is True, blank_code
            mix_id = '0b0b0b0b-0b0b-40b0-80b0-0b0b0b0b0b0b'
            with connect() as db:
                mix_source = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                mix_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                mix_computer = db.execute("SELECT count(*) FROM computers WHERE id='office3:9'").fetchone()[0]
                mix_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment'").fetchone()[0]
                assert mix_source is not None and mix_source[0] == account and mix_cells == 1 and mix_computer == 0, mix_cells
                db.execute("INSERT INTO computers(id) VALUES('office3:9')")
                db.execute("INSERT INTO publishing_layout(batch,computer_id,slot,account_id,label,office,source_cell,source_url,reason) VALUES('slot-batch-0364','office3:9',4,NULL,'','','','','')")
            mix_move = urllib.request.Request(base + '/api/publishing/assignment', data=json.dumps(dict(batch='slot-batch-0364', computer_id='synthetic-pc', slot=2, action='move', target_computer='office3:9', target_slot=4, request_id=mix_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(mix_move, timeout=10) as response:
                    mix_code, mix_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                mix_code, mix_body = error.code, json.load(error)
            assert mix_code == 400 and mix_body.get('error') == '不同设备批次不能混用发布电脑', mix_code
            try:
                with current_owner.open(mix_move, timeout=10) as response:
                    mix_again_code, mix_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                mix_again_code, mix_again_body = error.code, json.load(error)
            assert mix_again_code == 400 and mix_again_body.get('error') == '不同设备批次不能混用发布电脑', mix_again_code
            with connect() as db:
                mix_source_after = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                mix_target_after = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='office3:9' AND slot=4").fetchone()
                mix_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                mix_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (mix_id,)).fetchone()
                mix_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (mix_id,)).fetchone()[0]
                mix_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment'").fetchone()[0]
            mix_kept = mix_source_after is not None and mix_source_after[0] == account and mix_target_after is not None and mix_target_after[0] is None and mix_target_after[1] == '' and mix_cells_after == 2 and mix_stored[0] == 'failed' and mix_stored[1] == 400 and mix_count == 1 and mix_audits_after == mix_audits
            assert mix_kept is True, mix_code
            stale_batch_id = '5b5b5b5b-5b5b-45b5-85b5-5b5b5b5b5b5b'
            with connect() as db:
                stale_batch_source = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                stale_batch_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                stale_batch_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment'").fetchone()[0]
            assert stale_batch_source is not None and stale_batch_source[0] == account and stale_batch_cells == 2, stale_batch_cells
            stale_batch_move = urllib.request.Request(base + '/api/publishing/assignment', data=json.dumps(dict(batch='stale-batch-0370', computer_id='synthetic-pc', slot=2, action='remove', request_id=stale_batch_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(stale_batch_move, timeout=10) as response:
                    stale_batch_code, stale_batch_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                stale_batch_code, stale_batch_body = error.code, json.load(error)
            assert stale_batch_code == 409 and stale_batch_body.get('error') == '发布安排批次已更新，请刷新', stale_batch_code
            try:
                with current_owner.open(stale_batch_move, timeout=10) as response:
                    stale_batch_again_code, stale_batch_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                stale_batch_again_code, stale_batch_again_body = error.code, json.load(error)
            assert stale_batch_again_code == 409 and stale_batch_again_body.get('error') == '发布安排批次已更新，请刷新', stale_batch_again_code
            with connect() as db:
                stale_batch_source_after = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                stale_batch_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                stale_batch_stored = db.execute('SELECT state, result_status, result_json IS NULL FROM idempotency_records WHERE request_id=?', (stale_batch_id,)).fetchone()
                stale_batch_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (stale_batch_id,)).fetchone()[0]
                stale_batch_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment'").fetchone()[0]
            stale_batch_kept = stale_batch_source_after is not None and stale_batch_source_after[0] == account and stale_batch_cells_after == stale_batch_cells and stale_batch_stored[0] == 'pending' and stale_batch_stored[1] is None and stale_batch_stored[2] is True and stale_batch_count == 1 and stale_batch_audits_after == stale_batch_audits
            assert stale_batch_kept is True, stale_batch_code
            move_once_id = '3a3a3a3a-3a3a-43a3-83a3-3a3a3a3a3a3a'
            with connect() as db:
                move_once_source = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                move_once_target = db.execute("SELECT count(*) FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()[0]
                move_once_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                move_once_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment'").fetchone()[0]
                move_once_history = db.execute("SELECT count(*) FROM operations_history WHERE entity IN ('layout:slot-batch-0364:synthetic-pc:2','layout:slot-batch-0364:synthetic-pc:5')").fetchone()[0]
                assert move_once_source is not None and move_once_source[0] == account and move_once_target == 0 and move_once_cells == 2, move_once_cells
                db.execute("INSERT INTO publishing_layout(batch,computer_id,slot,account_id,label,office,source_cell,source_url,reason) VALUES('slot-batch-0364','synthetic-pc',5,NULL,'','','','','')")
            move_once = urllib.request.Request(base + '/api/publishing/assignment', data=json.dumps(dict(batch='slot-batch-0364', computer_id='synthetic-pc', slot=2, action='move', target_computer='synthetic-pc', target_slot=5, request_id=move_once_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(move_once, timeout=10) as response:
                    move_once_code, move_once_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                move_once_code, move_once_body = error.code, json.load(error)
            assert move_once_code == 200 and move_once_body.get('ok') is True and 'password' not in move_once_body, move_once_code
            try:
                with current_owner.open(move_once, timeout=10) as response:
                    move_once_again_code, move_once_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                move_once_again_code, move_once_again_body = error.code, json.load(error)
            assert move_once_again_code == 200 and move_once_again_body.get('ok') is True and 'password' not in move_once_again_body, move_once_again_code
            with connect() as db:
                move_once_source_after = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                move_once_target_after = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()
                move_once_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                move_once_links = db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()[0]
                move_once_source_links = db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()[0]
                move_once_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (move_once_id,)).fetchone()
                move_once_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (move_once_id,)).fetchone()[0]
                move_once_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='/api/publishing/assignment'").fetchone()[0]
                move_once_history_after = db.execute("SELECT count(*) FROM operations_history WHERE entity IN ('layout:slot-batch-0364:synthetic-pc:2','layout:slot-batch-0364:synthetic-pc:5')").fetchone()[0]
            move_once_kept = move_once_source_after is not None and move_once_source_after[0] is None and move_once_target_after is not None and move_once_target_after[0] == account and move_once_cells_after == move_once_cells + 1 and move_once_links == 1 and move_once_source_links == 0 and move_once_stored[0] == 'succeeded' and move_once_stored[1] == 200 and move_once_count == 1 and move_once_audits_after == move_once_audits + 1 and move_once_history_after == move_once_history + 2
            assert move_once_kept is True, move_once_code
            link_stale_id = '4a4a4a4a-4a4a-44a4-84a4-4a4a4a4a4a4a'
            with connect() as db:
                link_stale_target = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()
                link_stale_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_stale_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
            assert link_stale_target is not None and link_stale_target[0] == account, link_stale_cells
            link_stale = urllib.request.Request(base + '/api/publishing/link', data=json.dumps(dict(batch='stale-link-0372', computer_id='synthetic-pc', slot=5, action='link', account_id=account, expected_account_id=account, request_id=link_stale_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(link_stale, timeout=10) as response:
                    link_stale_code, link_stale_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_stale_code, link_stale_body = error.code, json.load(error)
            assert link_stale_code == 409 and link_stale_body.get('error') == '发布安排已更新，请刷新后重新选择', link_stale_code
            try:
                with current_owner.open(link_stale, timeout=10) as response:
                    link_stale_again_code, link_stale_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_stale_again_code, link_stale_again_body = error.code, json.load(error)
            assert link_stale_again_code == 409 and link_stale_again_body.get('error') == '发布安排已更新，请刷新后重新选择', link_stale_again_code
            with connect() as db:
                link_stale_target_after = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()
                link_stale_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_stale_stored = db.execute('SELECT state, result_status, result_json IS NULL FROM idempotency_records WHERE request_id=?', (link_stale_id,)).fetchone()
                link_stale_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (link_stale_id,)).fetchone()[0]
                link_stale_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
            link_stale_kept = link_stale_target_after is not None and link_stale_target_after[0] == account and link_stale_cells_after == link_stale_cells and link_stale_stored[0] == 'pending' and link_stale_stored[1] is None and link_stale_stored[2] is True and link_stale_count == 1 and link_stale_audits_after == link_stale_audits
            assert link_stale_kept is True, link_stale_code
            link_blank_id = '6b6b6b6b-6b6b-46b6-86b6-6b6b6b6b6b6b'
            with connect() as db:
                link_blank_cell = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                link_blank_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_blank_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
            assert link_blank_cell is not None and link_blank_cell[0] is None and link_blank_cell[1] == '' and link_blank_cells > 0, link_blank_cells
            link_blank = urllib.request.Request(base + '/api/publishing/link', data=json.dumps(dict(batch='slot-batch-0364', computer_id='synthetic-pc', slot=2, action='link', request_id=link_blank_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(link_blank, timeout=10) as response:
                    link_blank_code, link_blank_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_blank_code, link_blank_body = error.code, json.load(error)
            assert link_blank_code == 400 and link_blank_body.get('error') == '只能处理已有文字的待匹配格子', link_blank_code
            try:
                with current_owner.open(link_blank, timeout=10) as response:
                    link_blank_again_code, link_blank_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_blank_again_code, link_blank_again_body = error.code, json.load(error)
            assert link_blank_again_code == 400 and link_blank_again_body.get('error') == '只能处理已有文字的待匹配格子', link_blank_again_code
            with connect() as db:
                link_blank_cell_after = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                link_blank_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_blank_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (link_blank_id,)).fetchone()
                link_blank_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (link_blank_id,)).fetchone()[0]
                link_blank_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
            link_blank_kept = link_blank_cell_after is not None and link_blank_cell_after[0] is None and link_blank_cell_after[1] == '' and link_blank_cells_after == link_blank_cells and link_blank_stored[0] == 'failed' and link_blank_stored[1] == 400 and link_blank_count == 1 and link_blank_audits_after == link_blank_audits
            assert link_blank_kept is True, link_blank_code
            link_changed_id = '7c7c7c7c-7c7c-47c7-87c7-7c7c7c7c7c7c'
            with connect() as db:
                link_changed_cell = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                link_changed_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_changed_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
                assert link_changed_cell is not None and link_changed_cell[0] is None and link_changed_cell[1] == '' and link_changed_cells > 0, link_changed_cells
                db.execute("UPDATE publishing_layout SET label='pending-label-0374' WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2")
            link_changed = urllib.request.Request(base + '/api/publishing/link', data=json.dumps(dict(batch='slot-batch-0364', computer_id='synthetic-pc', slot=2, action='link', request_id=link_changed_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(link_changed, timeout=10) as response:
                    link_changed_code, link_changed_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_changed_code, link_changed_body = error.code, json.load(error)
            assert link_changed_code == 409 and link_changed_body.get('error') == '这个格子已被其他人处理，请刷新后重试', link_changed_code
            try:
                with current_owner.open(link_changed, timeout=10) as response:
                    link_changed_again_code, link_changed_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_changed_again_code, link_changed_again_body = error.code, json.load(error)
            assert link_changed_again_code == 409 and link_changed_again_body.get('error') == '这个格子已被其他人处理，请刷新后重试', link_changed_again_code
            with connect() as db:
                link_changed_cell_after = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                link_changed_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_changed_stored = db.execute('SELECT state, result_status, result_json IS NULL FROM idempotency_records WHERE request_id=?', (link_changed_id,)).fetchone()
                link_changed_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (link_changed_id,)).fetchone()[0]
                link_changed_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
            link_changed_kept = link_changed_cell_after is not None and link_changed_cell_after[0] is None and link_changed_cell_after[1] == 'pending-label-0374' and link_changed_cells_after == link_changed_cells and link_changed_stored[0] == 'pending' and link_changed_stored[1] is None and link_changed_stored[2] is True and link_changed_count == 1 and link_changed_audits_after == link_changed_audits
            assert link_changed_kept is True, link_changed_code
            link_clear_id = '9c9c9c9c-9c9c-49c9-89c9-9c9c9c9c9c9c'
            with connect() as db:
                link_clear_cell = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                link_clear_links = db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()[0]
                link_clear_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_clear_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
            assert link_clear_cell is not None and link_clear_cell[0] is None and bool(str(link_clear_cell[1]).strip()) and link_clear_links == 0 and link_clear_cells > 0, link_clear_cells
            link_clear = urllib.request.Request(base + '/api/publishing/link', data=json.dumps(dict(batch='slot-batch-0364', computer_id='synthetic-pc', slot=2, action='clear', expected_account_id=None, request_id=link_clear_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(link_clear, timeout=10) as response:
                    link_clear_code, link_clear_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_clear_code, link_clear_body = error.code, json.load(error)
            assert link_clear_code == 400 and link_clear_body.get('error') == '该格子没有可取消的手动关联', link_clear_code
            try:
                with current_owner.open(link_clear, timeout=10) as response:
                    link_clear_again_code, link_clear_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_clear_again_code, link_clear_again_body = error.code, json.load(error)
            assert link_clear_again_code == 400 and link_clear_again_body.get('error') == '该格子没有可取消的手动关联', link_clear_again_code
            with connect() as db:
                link_clear_cell_after = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                link_clear_links_after = db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()[0]
                link_clear_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_clear_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (link_clear_id,)).fetchone()
                link_clear_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (link_clear_id,)).fetchone()[0]
                link_clear_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
            link_clear_kept = link_clear_cell_after is not None and link_clear_cell_after[0] is None and link_clear_cell_after[1] == link_clear_cell[1] and link_clear_links_after == 0 and link_clear_cells_after == link_clear_cells and link_clear_stored[0] == 'failed' and link_clear_stored[1] == 400 and link_clear_count == 1 and link_clear_audits_after == link_clear_audits
            assert link_clear_kept is True, link_clear_code
            link_missing_id = '1c1c1c1c-1c1c-41c1-81c1-1c1c1c1c1c1c'
            with connect() as db:
                link_missing_cell = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                link_missing_account = db.execute("SELECT count(*) FROM accounts WHERE id='missing-link-0376'").fetchone()[0]
                link_missing_links = db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()[0]
                link_missing_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_missing_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
            assert link_missing_cell is not None and link_missing_cell[0] is None and bool(str(link_missing_cell[1]).strip()) and link_missing_account == 0 and link_missing_links == 0 and link_missing_cells > 0, link_missing_cells
            link_missing = urllib.request.Request(base + '/api/publishing/link', data=json.dumps(dict(batch='slot-batch-0364', computer_id='synthetic-pc', slot=2, action='link', account_id='missing-link-0376', expected_account_id=None, request_id=link_missing_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(link_missing, timeout=10) as response:
                    link_missing_code, link_missing_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_missing_code, link_missing_body = error.code, json.load(error)
            assert link_missing_code == 400 and link_missing_body.get('error') == '请选择工作台中已存在的账号', link_missing_code
            try:
                with current_owner.open(link_missing, timeout=10) as response:
                    link_missing_again_code, link_missing_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_missing_again_code, link_missing_again_body = error.code, json.load(error)
            assert link_missing_again_code == 400 and link_missing_again_body.get('error') == '请选择工作台中已存在的账号', link_missing_again_code
            with connect() as db:
                link_missing_cell_after = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                link_missing_account_after = db.execute("SELECT count(*) FROM accounts WHERE id='missing-link-0376'").fetchone()[0]
                link_missing_links_after = db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()[0]
                link_missing_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_missing_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (link_missing_id,)).fetchone()
                link_missing_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (link_missing_id,)).fetchone()[0]
                link_missing_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
            link_missing_kept = link_missing_cell_after is not None and link_missing_cell_after[0] is None and link_missing_cell_after[1] == link_missing_cell[1] and link_missing_account_after == 0 and link_missing_links_after == 0 and link_missing_cells_after == link_missing_cells and link_missing_stored[0] == 'failed' and link_missing_stored[1] == 400 and link_missing_count == 1 and link_missing_audits_after == link_missing_audits
            assert link_missing_kept is True, link_missing_code
            link_duplicate_id = '2e2e2e2e-2e2e-42e2-82e2-2e2e2e2e2e2e'
            with connect() as db:
                link_duplicate_target = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                link_duplicate_source = db.execute("SELECT account_id, source_cell FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()
                link_duplicate_office = db.execute('SELECT office_group, phone_device_id FROM accounts WHERE id=?', (account,)).fetchone()
                link_duplicate_links = db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()[0]
                link_duplicate_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_duplicate_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
            link_duplicate_ready = link_duplicate_target is not None and link_duplicate_target[0] is None and bool(str(link_duplicate_target[1]).strip()) and link_duplicate_source is not None and link_duplicate_source[0] == account and link_duplicate_office is not None and link_duplicate_office[0] != 'three' and not str(link_duplicate_office[1] or '').startswith('office3:') and link_duplicate_links == 0 and link_duplicate_cells > 0
            assert link_duplicate_ready is True, link_duplicate_cells
            link_duplicate = urllib.request.Request(base + '/api/publishing/link', data=json.dumps(dict(batch='slot-batch-0364', computer_id='synthetic-pc', slot=2, action='link', account_id=account, expected_account_id=None, request_id=link_duplicate_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(link_duplicate, timeout=10) as response:
                    link_duplicate_code, link_duplicate_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_duplicate_code, link_duplicate_body = error.code, json.load(error)
            assert link_duplicate_code == 409 and link_duplicate_body.get('error') == '该账号已有发布位置 '+link_duplicate_source[1]+'，请先核对，不能重复关联', link_duplicate_code
            try:
                with current_owner.open(link_duplicate, timeout=10) as response:
                    link_duplicate_again_code, link_duplicate_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_duplicate_again_code, link_duplicate_again_body = error.code, json.load(error)
            assert link_duplicate_again_code == 409 and link_duplicate_again_body.get('error') == '该账号已有发布位置 '+link_duplicate_source[1]+'，请先核对，不能重复关联', link_duplicate_again_code
            with connect() as db:
                link_duplicate_target_after = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                link_duplicate_source_after = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()
                link_duplicate_placed = db.execute("SELECT count(*) FROM publishing_layout WHERE batch='slot-batch-0364' AND account_id=?", (account,)).fetchone()[0]
                link_duplicate_links_after = db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()[0]
                link_duplicate_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_duplicate_stored = db.execute('SELECT state, result_status, result_json IS NULL FROM idempotency_records WHERE request_id=?', (link_duplicate_id,)).fetchone()
                link_duplicate_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (link_duplicate_id,)).fetchone()[0]
                link_duplicate_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
            link_duplicate_kept = link_duplicate_target_after is not None and link_duplicate_target_after[0] is None and link_duplicate_target_after[1] == link_duplicate_target[1] and link_duplicate_source_after is not None and link_duplicate_source_after[0] == account and link_duplicate_placed == 1 and link_duplicate_links_after == 0 and link_duplicate_cells_after == link_duplicate_cells and link_duplicate_stored[0] == 'pending' and link_duplicate_stored[1] is None and link_duplicate_stored[2] is True and link_duplicate_count == 1 and link_duplicate_audits_after == link_duplicate_audits
            assert link_duplicate_kept is True, link_duplicate_code
            link_office_id = '3e3e3e3e-3e3e-43e3-83e3-3e3e3e3e3e3e'
            with connect() as db:
                link_office_target = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='office3:9' AND slot=4").fetchone()
                link_office_source = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()
                link_office_account = db.execute('SELECT office_group, phone_device_id FROM accounts WHERE id=?', (account,)).fetchone()
                link_office_links = db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='office3:9' AND slot=4").fetchone()[0]
                link_office_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_office_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
                link_office_ready = link_office_target is not None and link_office_target[0] is None and link_office_target[1] == '' and link_office_source is not None and link_office_source[0] == account and link_office_account is not None and link_office_account[0] != 'three' and not str(link_office_account[1] or '').startswith('office3:') and link_office_links == 0 and link_office_cells > 0
                assert link_office_ready is True, link_office_cells
                db.execute("UPDATE publishing_layout SET label='office-label-0378' WHERE batch='slot-batch-0364' AND computer_id='office3:9' AND slot=4")
            link_office = urllib.request.Request(base + '/api/publishing/link', data=json.dumps(dict(batch='slot-batch-0364', computer_id='office3:9', slot=4, action='link', account_id=account, expected_account_id=None, request_id=link_office_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(link_office, timeout=10) as response:
                    link_office_code, link_office_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_office_code, link_office_body = error.code, json.load(error)
            assert link_office_code == 400 and link_office_body.get('error') == '来源批次不同：办公室三与原办公室的重号设备必须分开处理', link_office_code
            try:
                with current_owner.open(link_office, timeout=10) as response:
                    link_office_again_code, link_office_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_office_again_code, link_office_again_body = error.code, json.load(error)
            assert link_office_again_code == 400 and link_office_again_body.get('error') == '来源批次不同：办公室三与原办公室的重号设备必须分开处理', link_office_again_code
            with connect() as db:
                link_office_target_after = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='office3:9' AND slot=4").fetchone()
                link_office_source_after = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()
                link_office_links_after = db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='office3:9' AND slot=4").fetchone()[0]
                link_office_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_office_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (link_office_id,)).fetchone()
                link_office_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (link_office_id,)).fetchone()[0]
                link_office_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
            link_office_kept = link_office_target_after is not None and link_office_target_after[0] is None and link_office_target_after[1] == 'office-label-0378' and link_office_source_after is not None and link_office_source_after[0] == account and link_office_links_after == 0 and link_office_cells_after == link_office_cells and link_office_stored[0] == 'failed' and link_office_stored[1] == 400 and link_office_count == 1 and link_office_audits_after == link_office_audits
            assert link_office_kept is True, link_office_code
            link_matched_id = '5e5e5e5e-5e5e-45e5-85e5-5e5e5e5e5e5e'
            with connect() as db:
                link_matched_cell = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()
                link_matched_links = db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()[0]
                link_matched_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_matched_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
                assert link_matched_cell is not None and link_matched_cell[0] == account and link_matched_links == 1 and link_matched_cells > 0, link_matched_cells
                db.execute("UPDATE publishing_layout SET label='source-label-0379' WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5")
                db.execute("DELETE FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5")
            link_matched = urllib.request.Request(base + '/api/publishing/link', data=json.dumps(dict(batch='slot-batch-0364', computer_id='synthetic-pc', slot=5, action='link', account_id=account, expected_account_id=account, request_id=link_matched_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(link_matched, timeout=10) as response:
                    link_matched_code, link_matched_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_matched_code, link_matched_body = error.code, json.load(error)
            assert link_matched_code == 400 and link_matched_body.get('error') == '此格子已由来源表匹配，本入口仅处理待匹配和手动关联的格子', link_matched_code
            try:
                with current_owner.open(link_matched, timeout=10) as response:
                    link_matched_again_code, link_matched_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_matched_again_code, link_matched_again_body = error.code, json.load(error)
            assert link_matched_again_code == 400 and link_matched_again_body.get('error') == '此格子已由来源表匹配，本入口仅处理待匹配和手动关联的格子', link_matched_again_code
            with connect() as db:
                link_matched_cell_after = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()
                link_matched_links_after = db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()[0]
                link_matched_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_matched_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (link_matched_id,)).fetchone()
                link_matched_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (link_matched_id,)).fetchone()[0]
                link_matched_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
            link_matched_kept = link_matched_cell_after is not None and link_matched_cell_after[0] == account and link_matched_cell_after[1] == 'source-label-0379' and link_matched_links_after == 0 and link_matched_cells_after == link_matched_cells and link_matched_stored[0] == 'failed' and link_matched_stored[1] == 400 and link_matched_count == 1 and link_matched_audits_after == link_matched_audits
            assert link_matched_kept is True, link_matched_code
            link_new_id = '6f6f6f6f-6f6f-46f6-86f6-6f6f6f6f6f6f'
            link_apply_id = '4d4d4d4d-4d4d-44d4-84d4-4d4d4d4d4d4d'
            with connect() as db:
                link_apply_slot = db.execute("SELECT count(*) FROM accounts WHERE computer_id='synthetic-pc' AND slot=6").fetchone()[0]
                link_apply_target = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                link_apply_links = db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()[0]
                link_apply_source = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()
                link_apply_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
            assert link_apply_slot == 0 and link_apply_target is not None and link_apply_target[0] is None and bool(str(link_apply_target[1]).strip()) and link_apply_links == 0 and link_apply_source is not None and link_apply_source[0] == account and link_apply_cells > 0, link_apply_cells
            link_new = urllib.request.Request(base + '/api/account/create', data=json.dumps(dict(name='link-person-0380', status='正常', computer_id='synthetic-pc', slot=6, request_id=link_new_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(link_new, timeout=10) as response:
                    link_new_code, link_new_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_new_code, link_new_body = error.code, json.load(error)
            link_new_account = link_new_body.get('id')
            assert link_new_code == 200 and link_new_body.get('ok') is True and bool(link_new_account) and 'password' not in link_new_body, link_new_code
            try:
                with current_owner.open(link_new, timeout=10) as response:
                    link_new_again_code, link_new_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_new_again_code, link_new_again_body = error.code, json.load(error)
            assert link_new_again_code == 200 and link_new_again_body.get('id') == link_new_account and 'password' not in link_new_again_body, link_new_again_code
            with connect() as db:
                link_apply_created = db.execute("SELECT count(*) FROM accounts WHERE id=? AND computer_id='synthetic-pc' AND slot=6", (link_new_account,)).fetchone()[0]
                link_apply_named = db.execute("SELECT count(*) FROM accounts WHERE name='link-person-0380'").fetchone()[0]
                link_apply_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
                link_apply_history = db.execute("SELECT count(*) FROM operations_history WHERE entity='layout:slot-batch-0364:synthetic-pc:2'").fetchone()[0]
            assert link_apply_created == 1 and link_apply_named == 1, link_new_code
            link_apply = urllib.request.Request(base + '/api/publishing/link', data=json.dumps(dict(batch='slot-batch-0364', computer_id='synthetic-pc', slot=2, action='link', account_id=link_new_account, expected_account_id=None, request_id=link_apply_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(link_apply, timeout=10) as response:
                    link_apply_code, link_apply_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_apply_code, link_apply_body = error.code, json.load(error)
            assert link_apply_code == 200 and link_apply_body.get('ok') is True and link_apply_body.get('account_id') == link_new_account and 'password' not in link_apply_body, link_apply_code
            try:
                with current_owner.open(link_apply, timeout=10) as response:
                    link_apply_again_code, link_apply_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_apply_again_code, link_apply_again_body = error.code, json.load(error)
            assert link_apply_again_code == 200 and link_apply_again_body.get('ok') is True and link_apply_again_body.get('account_id') == link_new_account and 'password' not in link_apply_again_body, link_apply_again_code
            with connect() as db:
                link_apply_target_after = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                link_apply_links_after = db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()[0]
                link_apply_source_after = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()
                link_apply_placed = db.execute("SELECT count(*) FROM publishing_layout WHERE batch='slot-batch-0364' AND account_id=?", (link_new_account,)).fetchone()[0]
                link_apply_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_apply_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (link_apply_id,)).fetchone()
                link_apply_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (link_apply_id,)).fetchone()[0]
                link_apply_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
                link_apply_history_after = db.execute("SELECT count(*) FROM operations_history WHERE entity='layout:slot-batch-0364:synthetic-pc:2'").fetchone()[0]
            link_apply_kept = link_apply_target_after is not None and link_apply_target_after[0] == link_new_account and link_apply_target_after[1] == link_apply_target[1] and link_apply_links_after == 1 and link_apply_source_after is not None and link_apply_source_after[0] == account and link_apply_placed == 1 and link_apply_cells_after == link_apply_cells and link_apply_stored[0] == 'succeeded' and link_apply_stored[1] == 200 and link_apply_count == 1 and link_apply_audits_after == link_apply_audits + 1 and link_apply_history_after == link_apply_history + 1
            assert link_apply_kept is True, link_apply_code
            link_clear_once_id = '3f3f3f3f-3f3f-43f3-83f3-3f3f3f3f3f3f'
            with connect() as db:
                link_clear_once_cell = db.execute("SELECT account_id, label FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                link_clear_once_manual = db.execute("SELECT previous_reason FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                link_clear_once_links = db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()[0]
                link_clear_once_source = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()
                link_clear_once_cells = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_clear_once_audits = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
                link_clear_once_history = db.execute("SELECT count(*) FROM operations_history WHERE entity='layout:slot-batch-0364:synthetic-pc:2'").fetchone()[0]
            link_clear_once_ready = link_clear_once_cell is not None and link_clear_once_cell[0] == link_new_account and bool(str(link_clear_once_cell[1]).strip()) and link_clear_once_manual is not None and link_clear_once_links == 1 and link_clear_once_source is not None and link_clear_once_source[0] == account and link_clear_once_cells > 0
            assert link_clear_once_ready is True, link_clear_once_cells
            link_clear_once = urllib.request.Request(base + '/api/publishing/link', data=json.dumps(dict(batch='slot-batch-0364', computer_id='synthetic-pc', slot=2, action='clear', expected_account_id=link_new_account, request_id=link_clear_once_id)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(link_clear_once, timeout=10) as response:
                    link_clear_once_code, link_clear_once_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_clear_once_code, link_clear_once_body = error.code, json.load(error)
            assert link_clear_once_code == 200 and link_clear_once_body.get('ok') is True and link_clear_once_body.get('account_id') is None and link_clear_once_body.get('previous_account_id') == link_new_account and 'password' not in link_clear_once_body, link_clear_once_code
            try:
                with current_owner.open(link_clear_once, timeout=10) as response:
                    link_clear_once_again_code, link_clear_once_again_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                link_clear_once_again_code, link_clear_once_again_body = error.code, json.load(error)
            assert link_clear_once_again_code == 200 and link_clear_once_again_body.get('ok') is True and link_clear_once_again_body.get('account_id') is None and 'password' not in link_clear_once_again_body, link_clear_once_again_code
            with connect() as db:
                link_clear_once_cell_after = db.execute("SELECT account_id, label, reason FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()
                link_clear_once_links_after = db.execute("SELECT count(*) FROM publishing_layout_links WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=2").fetchone()[0]
                link_clear_once_placed = db.execute("SELECT count(*) FROM publishing_layout WHERE batch='slot-batch-0364' AND account_id=?", (link_new_account,)).fetchone()[0]
                link_clear_once_source_after = db.execute("SELECT account_id FROM publishing_layout WHERE batch='slot-batch-0364' AND computer_id='synthetic-pc' AND slot=5").fetchone()
                link_clear_once_cells_after = db.execute('SELECT count(*) FROM publishing_layout').fetchone()[0]
                link_clear_once_stored = db.execute('SELECT state, result_status FROM idempotency_records WHERE request_id=?', (link_clear_once_id,)).fetchone()
                link_clear_once_count = db.execute('SELECT count(*) FROM idempotency_records WHERE request_id=?', (link_clear_once_id,)).fetchone()[0]
                link_clear_once_audits_after = db.execute("SELECT count(*) FROM auth_audit WHERE action='调整发布格子关联'").fetchone()[0]
                link_clear_once_history_after = db.execute("SELECT count(*) FROM operations_history WHERE entity='layout:slot-batch-0364:synthetic-pc:2'").fetchone()[0]
            link_clear_once_kept = link_clear_once_cell_after is not None and link_clear_once_cell_after[0] is None and link_clear_once_cell_after[1] == link_clear_once_cell[1] and link_clear_once_cell_after[2] == link_clear_once_manual[0] and link_clear_once_links_after == 0 and link_clear_once_placed == 0 and link_clear_once_source_after is not None and link_clear_once_source_after[0] == account and link_clear_once_cells_after == link_clear_once_cells and link_clear_once_stored[0] == 'succeeded' and link_clear_once_stored[1] == 200 and link_clear_once_count == 1 and link_clear_once_audits_after == link_clear_once_audits + 1 and link_clear_once_history_after == link_clear_once_history + 1
            assert link_clear_once_kept is True, link_clear_once_code
            other_owner = test_auth_support.client(base, 'test_owner', new_password)
            replacement = 'synthetic-second-password'
            with connect() as db:
                sessions_two = db.execute("SELECT count(*) FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id WHERE u.username='test_owner'").fetchone()[0]
                password_audits_before = db.execute("SELECT count(*) FROM auth_audit WHERE action='修改本人密码'").fetchone()[0]
            assert sessions_two >= 2, sessions_two
            replaced = urllib.request.Request(base + '/api/auth/password', data=json.dumps(dict(old_password=new_password, password=replacement)).encode(), headers={'Content-Type': 'application/json'})
            try:
                with current_owner.open(replaced, timeout=10) as response:
                    replaced_status, replaced_body = response.status, json.load(response)
            except urllib.error.HTTPError as error:
                replaced_status, replaced_body = error.code, json.load(error)
            replaced_text = json.dumps(replaced_body, ensure_ascii=False)
            replaced_leaked = (new_password in replaced_text) or (replacement in replaced_text)
            assert replaced_status == 200 and replaced_body == {'ok': True} and replaced_leaked is False, replaced_status
            def read_status(client):
                try:
                    get_board(client)
                    return 200
                except urllib.error.HTTPError as error:
                    error.read()
                    return error.code
            old_status = read_status(owner_client)
            other_status = read_status(other_owner)
            assert old_status == 401 and other_status == 401, (old_status, other_status)
            with connect() as db:
                sessions_left = db.execute("SELECT count(*) FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id WHERE u.username='test_owner'").fetchone()[0]
                password_rows = db.execute("SELECT action, COALESCE(target,'') FROM auth_audit WHERE action='修改本人密码'").fetchall()
            password_text = '\n'.join((row[0] or '') + (row[1] or '') for row in password_rows)
            password_leaked = (new_password in password_text) or (replacement in password_text) or (test_auth_support.TEST_PASSWORD in password_text)
            assert sessions_left == 1 and len(password_rows) == password_audits_before + 1 and password_leaked is False, (sessions_two, sessions_left)
            assert get_board(current_owner)['accounts'][0]['published'] == published_before
            logged = '\n'.join(website_logs)
            secret_in_log = (test_auth_support.TEST_PASSWORD in logged) or (new_password in logged) or ('invalid-synthetic' in logged) or ('board_session=' in logged) or ('not-the-old-password' in logged) or ('synthetic-rejected-password' in logged) or ('synthetic-second-password' in logged) or ('form-body-marker' in logged) or ('array-body-marker' in logged) or ('broken-body-marker' in logged) or ('基础资料标记' in logged) or ('基础资料已改' in logged) or ('账号备注标记' in logged) or ('missing-device' in logged) or ('合成负责人' in logged) or ('13800002999' in logged) or ('13800003151' in logged) or ('13800003152' in logged) or ('13800003181' in logged) or ('13800003182' in logged) or ('13800003191' in logged) or ('13800003201' in logged) or ('bad-phone' in logged) or ('不是类型' in logged) or ('13800003231' in logged) or ('synthetic-id-0324' in logged) or ('bad id' in logged) or ('longid0326' in logged) or ('longname0328' in logged) or ('未列出字段' in logged) or ('未编号' in logged) or ('合成缺席' in logged) or ('bad-group' in logged) or ('分组不对' in logged) or ('999991' in logged) or ('bad-device' in logged) or ('longdev0341' in logged) or ('not-a-phone' in logged) or ('13800003431' in logged) or ('13800003441' in logged) or ('不是运营商' in logged) or ('13800003451' in logged) or ('not-a-date' in logged) or ('13800003461' in logged) or ('不是归属' in logged) or ('13800003471' in logged) or ('13800003481' in logged) or ('13800003491' in logged) or ('longkeep0349' in logged) or ('not-batch' in logged) or ('not-bool' in logged) or ('not-a-day' in logged) or ('bad login' in logged) or ('person0354' in logged) or ('user0355' in logged) or ('longuser0355' in logged) or ('user0356' in logged) or ('person0356' in logged) or ('user0357' in logged) or ('person0357' in logged) or ('user0358' in logged) or ('person0358' in logged) or ('user0359' in logged) or ('person0359' in logged) or ('999992' in logged) or ('user0360' in logged) or ('person0360' in logged) or ('user0361' in logged) or ('person0361' in logged) or ('synthetic-user-0361-pass' in logged) or ('person0362' in logged) or ('not-a-slot' in logged) or ('slot-batch-0364' in logged) or ('not-a-target' in logged) or ('office3:9' in logged) or ('stale-batch-0370' in logged) or ('stale-link-0372' in logged) or ('pending-label-0374' in logged) or ('missing-link-0376' in logged) or ('office-label-0378' in logged) or ('source-label-0379' in logged) or ('link-person-0380' in logged)
            assert '/api/auth/password' in logged and '/api/daily' in logged and secret_in_log is False, len(website_logs)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()
    print(json.dumps({'passed': True, 'kind': 'synthetic-http-injected-pg-not-deployment', 'checks': [
        'independent-http-logins-shared-board', 'barrier-http-same-version-one-winner',
        'single-history-entry', 'observed-http-lock-wait-revocation-denied', 'saved-value-preserved',
        'http-maintenance-epoch-generation-fences', 'ready-open-maintenance-and-unreachable', 'real-lock-timeout-stable-error',
        'database-unreachable-stable-error', 'export-timeout-stable-error', 'statement-timeout-stable-error', 'export-file-failed-stable-error', 'repeatable-read-snapshot', 'password-change-not-cached','password-audit-omits-secret', 'idempotency-result-rejects-secret-keys', 'shared-address-user-lock-does-not-block-peer', 'oversized-and-empty-body-rejected', 'foreign-origin-rejected', 'stalled-connection-and-body-read-rejected', 'concurrency-limit-rejects-extra-write', 'batch-missing-row-rolls-back', 'batch-verification-insert-rolls-back', 'batch-status-write-rolls-back-on-bad-readiness', 'batch-card-second-row-rolls-back-first', 'website-log-omits-secrets', 'login-audit-and-limit-omit-secret', 'logout-cookie-max-age-zero', 'session-cookie-httponly-samesite-without-secure', 'session-token-stored-as-hash', 'expired-session-rejected', 'csrf-mismatch-keeps-session', 'csrf-mismatch-keeps-daily', 'inactive-user-session-rejected', 'wrong-old-password-keeps-credential', 'illegal-password-length-keeps-credential', 'non-json-write-rejected', 'idempotency-result-hidden-from-other-user', 'idempotency-lookup-rechecks-permission', 'idempotency-pending-lookup-does-not-continue', 'idempotency-lookup-requires-login', 'json-body-must-be-object', 'password-change-revokes-other-sessions', 'logout-audit-omits-secret', 'card-tasks-cannot-change-base-fields', 'card-manage-cannot-change-task-fields', 'account-manage-cannot-change-status', 'pending-account-save-replay-does-not-store-failure', 'account-progress-and-device-field-gates', 'card-task-cannot-batch-base-or-create', 'batch-mixed-account-fields-reject-together', 'batch-unknown-field-rejected-before-reserve', 'batch-card-id-format-stores-failure', 'pending-account-save-continues-after-permission-grant', 'stale-version-conflict-stays-pending', 'pending-conflict-rejects-different-version', 'fresh-daily-id-still-publishes-once', 'linked-device-cannot-be-marked-idle', 'linked-main-cannot-be-marked-unregistered', 'second-child-rejected-without-placeholder', 'future-registration-date-rejected', 'parent-phone-change-rejected-when-child-differs', 'child-cannot-link-account-that-is-not-main', 'child-phone-must-match-parent', 'ambiguous-phone-does-not-auto-link', 'single-matching-phone-auto-links-once', 'invalid-registration-phone-rejected', 'invalid-account-type-rejected', 'blank-registration-phone-clears-once', 'duplicate-douyin-id-rejected', 'spaced-douyin-id-rejected', 'long-douyin-id-rejected', 'blank-account-name-rejected', 'long-account-name-rejected', 'unknown-account-field-rejected-before-reserve', 'office-patch-rejected-when-scope-disabled', 'account-slot-must-be-integer', 'account-name-must-be-text', 'account-patch-requires-changes', 'missing-account-patch-stores-failure', 'account-slot-out-of-range', 'account-office-group-rejected', 'missing-phone-device-stores-failure', 'invalid-phone-device-number', 'duplicate-phone-device-rejected', 'phone-device-field-too-long', 'invalid-phone-card-number', 'missing-phone-card-stores-failure', 'invalid-phone-card-carrier', 'invalid-phone-card-expiry', 'invalid-phone-card-ownership', 'phone-card-returned-without-date', 'duplicate-phone-card-rejected', 'phone-card-extra-too-long', 'batch-field-must-be-text', 'batch-kind-rejected', 'batch-selection-rejected', 'batch-daily-shape-rejected', 'user-login-rejected', 'user-name-too-long', 'user-initial-password-required', 'user-role-rejected', 'user-active-rejected', 'missing-user-stores-failure', 'owner-user-edit-stays-pending', 'duplicate-login-rejected', 'user-rename-revokes-session', 'assignment-slot-rejected', 'assignment-source-missing', 'assignment-target-slot-rejected', 'assignment-target-not-blank', 'assignment-batch-mismatch', 'assignment-batch-stale', 'assignment-move-once', 'link-batch-stale', 'link-blank-label', 'link-cell-changed', 'link-clear-without-manual', 'link-account-missing', 'link-account-duplicate', 'link-office-mismatch', 'link-source-matched', 'link-once', 'link-clear-once']}, ensure_ascii=True))


if __name__ == '__main__':
    main()
