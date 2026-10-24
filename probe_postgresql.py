"""Disposable PostgreSQL capability probe. Does not connect to CloudBase or app data."""
import argparse
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from test_isolation import PROJECT, create_root

BIN = PROJECT / '.local-tools/postgresql-16/pgsql/bin'


def source_hashes():
    from server_package import STATIC_FILES
    paths = ['probe_postgresql.py', 'pg_http_checks.py', 'pg_service_checks.py', 'pg_migration_checks.py', 'pg_admin.py', 'pg_admin_checks.py', 'pg_runtime.py', 'pg_runtime_checks.py', 'pg_process_checks.py', 'pg_cutover_checks.py', 'pg_schema.py', 'pg_schema_checks.py', 'office_scope.py', 'pg_scope_checks.py', 'pg_scope_read_checks.py', 'web_server.py',
             'pg_synthetic_migration.py', 'pg_service_adapter.py', 'pg_protocol_checks.py', 'pg_write_protocol.py',
             'test_isolation.py', 'test_auth_support.py', 'test_release_core.py', 'sql_dialect.py',
             'app.py', 'auth.py', 'account_service.py', 'account_lifecycle.py', 'phone_cards.py', 'phone_devices.py',
             'collection.py', 'release_policy.py', 'operations.py', 'publishing_layout.py', 'nurture.py',
             'data_exchange.py', 'workspace_service.py', 'backup.py', 'backup_management.py', 'batch_edit.py',
             'migrations/postgresql/0001_legacy_baseline.sql', 'migrations/postgresql/0002_office_scope.sql', 'migrations/postgresql/0003_idempotency.sql', 'migrations/postgresql/legacy-sqlite-schema.json', 'idempotency.py']
    paths+=['pg_browser_checks.py','browser_scope_checks.cjs']+['static/'+name for name in STATIC_FILES]
    return {path: hashlib.sha256((PROJECT / path).read_bytes()).hexdigest() for path in paths}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--protocol-checks', action='store_true')
    parser.add_argument('--migration-checks', action='store_true')
    parser.add_argument('--service-checks', action='store_true')
    parser.add_argument('--http-checks', action='store_true')
    parser.add_argument('--admin-checks', action='store_true')
    parser.add_argument('--runtime-checks', action='store_true')
    parser.add_argument('--process-checks', action='store_true')
    parser.add_argument('--cutover-checks', action='store_true')
    parser.add_argument('--schema-checks', action='store_true')
    parser.add_argument('--scope-checks', action='store_true')
    parser.add_argument('--scope-read-checks', action='store_true')
    parser.add_argument('--scope-write-checks', action='store_true')
    parser.add_argument('--browser-checks', action='store_true')
    parser.add_argument('--role-checks', action='store_true')
    args = parser.parse_args()
    if sum((args.migration_checks, args.service_checks, args.http_checks, args.admin_checks, args.runtime_checks, args.process_checks, args.cutover_checks, args.schema_checks, args.scope_checks, args.scope_read_checks, args.scope_write_checks, args.browser_checks, args.role_checks)) > 1:
        parser.error('Migration and service checks each require their own empty candidate schema')
    required = ('initdb', 'pg_ctl', 'psql', 'pg_dump', 'pg_restore', 'postgres')
    for name in required:
        if not (BIN / (name + '.exe')).is_file():
            raise ValueError('Project-local PostgreSQL binary is missing: ' + name)
    root = create_root(args.root)
    cluster = root / 'cluster'
    password_file = root / 'synthetic-password.txt'
    password = secrets.token_urlsafe(32)
    password_file.write_text(password, encoding='utf-8')
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith('PG')}
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    environment.update(PGHOST='127.0.0.1', PGPORT=str(port), PGUSER='synthetic_owner',
                       PGPASSWORD=password, PGDATABASE='synthetic_probe', PGCONNECT_TIMEOUT='5', PGCLIENTENCODING='UTF8',
                       PGPASSFILE=str(root / 'unused-pgpass'), PGSYSCONFDIR=str(root), PGAPPNAME='synthetic-capability-probe')
    environment['DOUYIN_BOARD_TEST_ROOT'] = str(root)
    environment['PYTHONIOENCODING'] = 'utf-8'
    environment['PGSSLMODE'] = 'disable'  # Only the newly generated loopback test cluster.
    started = datetime.now(timezone.utc).isoformat()
    def run(name, *arguments, timeout=60):
        command = [str(BIN / (name + '.exe')), *map(str, arguments)]
        options = dict(env=environment, cwd=root, timeout=timeout,
                       stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if name == 'pg_ctl':
            # Windows background postgres can inherit a pipe and prevent communicate() EOF.
            log = root / ('pg_ctl-' + secrets.token_hex(4) + '.log')
            with log.open('wb') as output:
                result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, **options)
            result.stdout = log.read_bytes()
            result.stderr = result.stdout
        else:
            result = subprocess.run(command, capture_output=True, **options)
        if result.returncode:
            # Report only synthetic diagnostics, never command environment or password.
            message = result.stderr.decode('utf-8', errors='replace').replace(password, '[redacted]')
            raise RuntimeError(name + ': ' + message[-2000:])
        return result.stdout.decode('utf-8', errors='replace').strip()
    report = {'kind': 'local-postgresql-capability-probe-not-platform-acceptance',
              'started_utc': started, 'host': '127.0.0.1', 'port': port, 'passed': False,
              'source_sha256': source_hashes()}
    cli_secret = ''
    try:
        report['version'] = run('postgres', '--version')
        run('initdb', '-D', cluster, '-U', 'synthetic_owner', '--encoding=UTF8', '--locale=C',
            '--auth-host=scram-sha-256', '--auth-local=scram-sha-256', '--pwfile', password_file)
        run('pg_ctl', '-D', cluster, '-l', root / 'postgres.log', '-w', '-t', '20',
            '-o', '-h 127.0.0.1 -p ' + str(port), 'start', timeout=30)
        def sql(text, database='synthetic_probe'):
            script = root / ('synthetic-' + secrets.token_hex(4) + '.sql')
            script.write_text(text, encoding='utf-8')
            # Windows -c arguments can be re-encoded using the active ANSI code page.
            return run('psql', '-X', '-v', 'ON_ERROR_STOP=1', '-d', database, '-At', '-f', script)
        sql('CREATE DATABASE synthetic_probe TEMPLATE template0', 'postgres')
        sql('''CREATE SCHEMA board_app;
            CREATE TABLE board_app.environment(kind text PRIMARY KEY CHECK(kind='synthetic-only'));
            INSERT INTO board_app.environment VALUES('synthetic-only');
            CREATE TABLE board_app.entities(id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
              name text NOT NULL, version bigint NOT NULL, content bytea, nullable text, leading_zero text);
            INSERT INTO board_app.entities(name,version,content,nullable,leading_zero)
              VALUES('合成资料',1,decode('89504e470d0a1a0a','hex'),NULL,'000123');
            BEGIN; UPDATE board_app.entities SET name='rollback'; ROLLBACK;
            UPDATE board_app.entities SET version=version+1 WHERE id=1 AND version=1;
            UPDATE board_app.entities SET name='stale' WHERE id=1 AND version=1;''')
        check = "SELECT count(*) FROM board_app.entities WHERE name='合成资料' AND version=2 AND nullable IS NULL AND leading_zero='000123' AND encode(content,'hex')='89504e470d0a1a0a'"
        if sql(check) != '1':
            raise RuntimeError('Rollback, conditional update or value preservation failed')
        run('pg_dump', '-d', 'synthetic_probe', '-n', 'board_app', '-Fc', '-f', root / 'synthetic.dump')
        sql('CREATE DATABASE synthetic_restore TEMPLATE template0', 'postgres')
        run('pg_restore', '--exit-on-error', '--no-owner', '-d', 'synthetic_restore', root / 'synthetic.dump')
        if sql(check, 'synthetic_restore') != '1':
            raise RuntimeError('Synthetic restore differs')
        if sql("INSERT INTO board_app.entities(name,version) VALUES('合成新增',1) RETURNING id", 'synthetic_restore').splitlines()[0] != '2':
            raise RuntimeError('Restored identity sequence did not advance')
        if args.role_checks:
            login_roles = sql("SELECT rolname || ' ' || CASE WHEN rolsuper THEN 'superuser' ELSE 'nosuper' END || ' ' || CASE WHEN rolcreatedb THEN 'createdb' ELSE 'nocreatedb' END || ' ' || CASE WHEN rolcreaterole THEN 'createrole' ELSE 'nocreaterole' END || ' ' || CASE WHEN rolbypassrls THEN 'bypassrls' ELSE 'nobypassrls' END || ' ' || CASE WHEN rolreplication THEN 'replication' ELSE 'noreplication' END || ' ' || CASE WHEN rolcanlogin THEN 'login' ELSE 'nologin' END FROM pg_roles WHERE rolcanlogin AND rolname !~ '^pg_' ORDER BY rolname")
            if login_roles != 'synthetic_owner superuser createdb createrole bypassrls replication login':
                raise RuntimeError('Synthetic login role attributes differed')
            sql('CREATE ROLE synthetic_app NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOREPLICATION')
            def expect_denied(statement, failure):
                script = root / ('synthetic-' + secrets.token_hex(4) + '.sql')
                script.write_text('SET ROLE synthetic_app;\n' + statement + ';\n', encoding='utf-8')
                denied = subprocess.run([str(BIN / 'psql.exe'), '-X', '-v', 'ON_ERROR_STOP=1', '-d', 'synthetic_probe', '-At', '-f', str(script)], env=environment, cwd=root, capture_output=True, timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if denied.returncode == 0 or 'permission denied' not in denied.stderr.decode('utf-8', errors='replace').lower():
                    raise RuntimeError(failure)
            expect_denied('CREATE ROLE synthetic_should_fail NOLOGIN', 'Restricted role was not denied role creation')
            expect_denied('CREATE DATABASE synthetic_should_fail', 'Restricted role was not denied database creation')
            expect_denied('SHOW data_directory', 'Restricted role was not denied data directory inspection')
            expect_denied('SELECT kind FROM board_app.environment', 'Restricted role was not denied marker read')
            if sql("SELECT count(*) FROM pg_roles WHERE rolname='synthetic_should_fail'") != '0':
                raise RuntimeError('Restricted role left a created role')
            if sql("SELECT count(*) FROM pg_database WHERE datname='synthetic_should_fail'") != '0':
                raise RuntimeError('Restricted role left a created database')
            restricted = sql("SELECT rolname || ' ' || CASE WHEN rolsuper THEN 'superuser' ELSE 'nosuper' END || ' ' || CASE WHEN rolcreatedb THEN 'createdb' ELSE 'nocreatedb' END || ' ' || CASE WHEN rolcreaterole THEN 'createrole' ELSE 'nocreaterole' END || ' ' || CASE WHEN rolbypassrls THEN 'bypassrls' ELSE 'nobypassrls' END || ' ' || CASE WHEN rolreplication THEN 'replication' ELSE 'noreplication' END || ' ' || CASE WHEN rolcanlogin THEN 'login' ELSE 'nologin' END FROM pg_roles WHERE rolname='synthetic_app'")
            if restricted != 'synthetic_app nosuper nocreatedb nocreaterole nobypassrls noreplication nologin':
                raise RuntimeError('Restricted role attributes differed')
            report['database_roles'] = {'passed': True, 'kind': 'synthetic-login-is-superuser-not-service-least-privilege', 'login_role_count': 1, 'service_login_is_superuser': True, 'service_login_is_minimum_privilege': False, 'restricted_nologin_role_cannot_create_database_or_role': True, 'service_still_uses_superuser': True}
            report['runtime_privileges'] = {'passed': True, 'kind': 'inventory-service-connection-unchanged', 'service_still_uses_superuser': True, 'service_login_is_minimum_privilege': False, 'show_data_directory_denied_for_nologin_role': True, 'marker_select_denied_without_grant': True}
        if args.protocol_checks:
            result = subprocess.run([sys.executable, '-B', str(PROJECT / 'pg_protocol_checks.py')],
                                    env=environment, cwd=PROJECT, capture_output=True, timeout=60,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise RuntimeError(result.stderr.decode('utf-8', errors='replace').replace(password, '[redacted]')[-2500:])
            report['protocol'] = json.loads(result.stdout)
        if args.migration_checks:
            result = subprocess.run([sys.executable, '-B', str(PROJECT / 'pg_migration_checks.py')],
                                    env=environment, cwd=PROJECT, capture_output=True, timeout=60,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise RuntimeError(result.stderr.decode('utf-8', errors='replace').replace(password, '[redacted]')[-2500:])
            report['migration'] = json.loads(result.stdout)
        if args.service_checks:
            result = subprocess.run([sys.executable, '-B', str(PROJECT / 'pg_service_checks.py')],
                                    env=environment, cwd=PROJECT, capture_output=True, timeout=60,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise RuntimeError(result.stderr.decode('utf-8', errors='replace').replace(password, '[redacted]')[-2500:])
            report['services'] = json.loads(result.stdout)
        if args.http_checks:
            result = subprocess.run([sys.executable, '-B', str(PROJECT / 'pg_http_checks.py')],
                                    env=environment, cwd=PROJECT, capture_output=True, timeout=90,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise RuntimeError(result.stderr.decode('utf-8', errors='replace').replace(password, '[redacted]')[-3500:])
            report['http'] = json.loads(result.stdout)
        if args.admin_checks:
            result = subprocess.run([sys.executable, '-B', str(PROJECT / 'pg_admin_checks.py')],
                                    env=environment, cwd=PROJECT, capture_output=True, timeout=60,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise RuntimeError(result.stderr.decode('utf-8', errors='replace').replace(password, '[redacted]')[-3500:])
            report['admin'] = json.loads(result.stdout)
            cli_secret = secrets.token_urlsafe(24)
            completed = subprocess.run([sys.executable, '-B', str(PROJECT / 'pg_admin.py'), 'reset-owner', '--synthetic-root', str(root), '--username', 'synthetic_owner', '--operator', 'synthetic-operator', '--reason', 'synthetic-drill', '--password-stdin'], input=(cli_secret + '\n').encode(), capture_output=True, timeout=30, env=environment, cwd=PROJECT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            text = completed.stdout.decode('utf-8', errors='replace')
            if cli_secret in text or cli_secret.encode() in completed.stderr or 'password_hash' in text:
                raise RuntimeError('Administrator process output included a secret')
            if completed.returncode:
                message = completed.stderr.decode('utf-8', errors='replace').replace(password, '[redacted]').replace(cli_secret, '[redacted]')
                raise RuntimeError(message[-1500:])
            payload = json.loads(text)
            if payload.get('ok') is not True or payload.get('operation') != 'reset-owner' or payload.get('mode') != 'maintenance' or payload.get('username') != 'synthetic_owner':
                raise RuntimeError('Administrator process did not complete the maintenance reset')
            report['admin_process'] = {'passed': True, 'kind': 'real-cli-process-reset-not-production', 'operation': 'reset-owner'}
        if args.runtime_checks:
            result = subprocess.run([sys.executable, '-B', str(PROJECT / 'pg_runtime_checks.py')],
                                    env=environment, cwd=PROJECT, capture_output=True, timeout=60,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise RuntimeError(result.stderr.decode('utf-8', errors='replace').replace(password, '[redacted]')[-3500:])
            report['runtime'] = json.loads(result.stdout)
        if args.process_checks:
            result = subprocess.run([sys.executable, '-B', str(PROJECT / 'pg_process_checks.py')],
                                    env=environment, cwd=PROJECT, capture_output=True, timeout=90,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise RuntimeError(result.stderr.decode('utf-8', errors='replace').replace(password, '[redacted]')[-3500:])
            report['process'] = json.loads(result.stdout)
        if args.cutover_checks:
            result = subprocess.run([sys.executable, '-B', str(PROJECT / 'pg_cutover_checks.py')],
                                    env=environment, cwd=PROJECT, capture_output=True, timeout=60,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise RuntimeError(result.stderr.decode('utf-8', errors='replace').replace(password, '[redacted]')[-3500:])
            report['cutover'] = json.loads(result.stdout)
        if args.schema_checks:
            result = subprocess.run([sys.executable, '-B', str(PROJECT / 'pg_schema_checks.py')],
                                    env=environment, cwd=PROJECT, capture_output=True, timeout=60,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise RuntimeError(result.stderr.decode('utf-8', errors='replace').replace(password, '[redacted]')[-3500:])
            report['schema'] = json.loads(result.stdout)
        if args.scope_checks:
            result = subprocess.run([sys.executable, '-B', str(PROJECT / 'pg_scope_checks.py')],
                                    env=environment, cwd=PROJECT, capture_output=True, timeout=60,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise RuntimeError(result.stderr.decode('utf-8', errors='replace').replace(password, '[redacted]')[-3500:])
            report['scope'] = json.loads(result.stdout)
        if args.scope_read_checks or args.scope_write_checks:
            result = subprocess.run([sys.executable, '-B', str(PROJECT / 'pg_scope_read_checks.py'), *(['--writes'] if args.scope_write_checks else [])],
                                    env=environment, cwd=PROJECT, capture_output=True, timeout=240 if args.scope_write_checks else 90,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode:
                raise RuntimeError(result.stderr.decode('utf-8', errors='replace').replace(password, '[redacted]')[-3500:])
            report['scope_read'] = json.loads(result.stdout)
        if args.browser_checks:
            result=subprocess.run([sys.executable,'-B',str(PROJECT/'pg_browser_checks.py')],env=environment,cwd=PROJECT,capture_output=True,timeout=220,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode:raise RuntimeError(result.stderr.decode('utf-8',errors='replace').replace(password,'[redacted]')[-4500:])
            report['browser']=json.loads(result.stdout)
        report['source_stable'] = report['source_sha256'] == source_hashes()
        if not report['source_stable']:
            raise RuntimeError('Source changed during test; rerun required')
        report.update(passed=True, checks=['UTF8', 'NULL', 'leading-zero', 'bytea', 'rollback',
                      'conditional-update', 'schema-dump-restore', 'restored-identity-sequence'])
    except Exception as error:
        message = str(error).replace(password, '[redacted]')
        if cli_secret:
            message = message.replace(cli_secret, '[redacted]')
        report['failure'] = {'type': type(error).__name__, 'message': message[-3500:]}
        raise
    finally:
        try:
            if (cluster / 'postmaster.pid').exists():
                run('pg_ctl', '-D', cluster, '-m', 'fast', '-w', '-t', '45', 'stop', timeout=55)
        except Exception as error:
            report['passed']=False
            message=str(error).replace(password,'[redacted]')
            if cli_secret:message=message.replace(cli_secret,'[redacted]')
            report['cleanup_failure']={'type':type(error).__name__,'message':message[-1500:]}
            raise
        finally:
            password_file.unlink(missing_ok=True)
            report['ended_utc'] = datetime.now(timezone.utc).isoformat()
            report['server_stopped'] = not (cluster / 'postmaster.pid').exists()
            if not report['server_stopped']:report['passed']=False
            (root / 'probe-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
