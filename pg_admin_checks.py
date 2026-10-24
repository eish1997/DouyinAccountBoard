"""Exercise the CLI entrypoint using generated passwords and disposable PG."""
from contextlib import redirect_stdout
import io
import json
from unittest.mock import patch
from test_isolation import checked_root, install
from pg_synthetic_migration import connect_synthetic, install_schema
from pg_service_adapter import ServiceConnection


def main():
    root = checked_root()
    install(root)
    import auth
    import pg_admin
    old_password = 'Synthetic-initial-password-71'
    new_password = 'Synthetic-reset-password-72'
    with connect_synthetic() as connection:
        install_schema(connection)
    def cli(operation, password, username='synthetic_owner'):
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO(password + '\n')), redirect_stdout(output):
            pg_admin.main([operation, '--synthetic-root', str(root), '--username', username,
                           '--operator', 'synthetic-operator', '--reason', 'synthetic-drill', '--password-stdin'])
        text = output.getvalue()
        assert password not in text and 'password_hash' not in text
        return json.loads(text)
    result = cli('initialize-owner', old_password)
    owner_id = result['owner_id']
    assert result['mode'] == 'maintenance'
    try:
        cli('initialize-owner', new_password)
        raise AssertionError('Duplicate initialization accepted')
    except ValueError as error:
        assert 'already exists' in str(error)
    with connect_synthetic() as connection:
        connection.execute('SET search_path=board_candidate,pg_catalog')
        db = ServiceConnection(connection)
        with connection.transaction():
            assert auth.password_matches(old_password, db.execute('SELECT password_hash FROM auth_users WHERE id=?', (owner_id,)).fetchone()[0])
            owner_token = auth.new_session(db, owner_id)
            owner = auth.session(db, 'board_session=' + owner_token)
            employee_id = auth.save_user(db, owner, dict(username='synthetic_employee', password=old_password))
            employee_token = auth.new_session(db, employee_id)
            db.execute("INSERT INTO computers(id,name) VALUES('synthetic-pc','合成保留电脑')")
            db.execute("UPDATE runtime_control SET mode='open'")
        try:
            cli('reset-owner', new_password)
            raise AssertionError('Reset outside maintenance accepted')
        except ValueError as error:
            assert 'maintenance' in str(error)
        db.execute("UPDATE runtime_control SET mode='maintenance'")
        try:
            cli('reset-owner', new_password, 'wrong_synthetic_owner')
            raise AssertionError('Wrong owner reset accepted')
        except ValueError as error:
            assert 'match' in str(error)
        assert cli('reset-owner', new_password)['owner_id'] == owner_id
        assert auth.session(db, 'board_session=' + owner_token) is None
        assert auth.session(db, 'board_session=' + employee_token)['id'] == employee_id
        assert db.execute('SELECT name FROM computers').fetchone()[0] == '合成保留电脑'
        with connection.transaction():
            assert auth.login(db, dict(username='synthetic_owner', password=old_password), '127.0.0.1') is None
            assert auth.login(db, dict(username='synthetic_owner', password=new_password), '127.0.0.1')
        audits = [dict(row) for row in db.execute("SELECT actor_name,action,target FROM auth_audit WHERE action IN ('initialize-owner','reset-owner') ORDER BY id")]
        assert [row['action'] for row in audits] == ['initialize-owner', 'reset-owner']
        assert all(row['actor_name'] == 'synthetic-operator' and row['target'] == 'synthetic-drill' for row in audits)
        serialized = json.dumps(audits)
        assert old_password not in serialized and new_password not in serialized
        assert db.execute('SELECT mode FROM runtime_control').fetchone()[0] == 'maintenance'
    print(json.dumps({'passed': True, 'kind': 'synthetic-admin-cli-not-production-ready', 'checks': [
        'stdin-password-not-output', 'empty-environment-owner-initialization', 'duplicate-initialization-rejected',
        'maintenance-required', 'exact-owner-required', 'reset-revokes-owner-only', 'business-preserved',
        'new-password-login-old-rejected', 'operator-reason-audit-no-password', 'maintenance-retained']}, ensure_ascii=True))


if __name__ == '__main__':
    main()
