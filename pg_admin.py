"""Explicit synthetic-only administrator tool; never accepts production DSNs."""
import argparse
import getpass
import json
from pathlib import Path
import sys
import time
import auth
from pg_service_adapter import ServiceConnection
from pg_synthetic_migration import connect_synthetic, TABLES
from test_isolation import checked_root
from pg_schema import expected_versions


def manage_owner(connection, *, operation, username, name, password, operator, reason):
    username, name = auth.identity(dict(username=username, name=name))
    if operation not in ('initialize-owner', 'reset-owner'):
        raise ValueError('Unknown administrator operation')
    if not 1 <= len(operator.strip()) <= 60 or not 1 <= len(reason.strip()) <= 120:
        raise ValueError('Operator and reason are required and must fit the audit record')
    encoded = auth.password_hash(password)
    with connection.transaction():
        connection.execute("SET LOCAL lock_timeout='5s'")
        connection.execute('SET LOCAL search_path=board_candidate,pg_catalog')
        if connection.execute('SELECT version,sha256 FROM schema_migrations ORDER BY version').fetchall() != expected_versions():
            raise ValueError('Reviewed migration version is required')
        control = connection.execute('SELECT mode FROM runtime_control WHERE id=1 FOR UPDATE').fetchone()
        if control != ('maintenance',):
            raise ValueError('Administrator recovery requires maintenance')
        db = ServiceConnection(connection)
        owner = db.execute("SELECT * FROM auth_users WHERE role='owner'").fetchone()
        if operation == 'initialize-owner':
            if owner:
                raise ValueError('Owner already exists; initialization refuses overwrite')
            for table in TABLES + ('offices','user_office_grants','account_offices','computer_offices','phone_card_offices','phone_device_offices'):
                if db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone():
                    raise ValueError('Initialization requires an explicitly empty environment')
            owner_id = db.execute("INSERT INTO auth_users(username,name,role,password_hash,created_at) VALUES(?,?,'owner',?,?) RETURNING id",
                                  (username, name, encoded, int(time.time()))).fetchone()[0]
            db.execute("INSERT INTO collection_settings(id,auto_enabled,paused,pause_reason) VALUES(1,0,1,'collection-disabled')")
            db.execute('INSERT INTO collection_worker(id) VALUES(1)')
        else:
            if owner is None or owner['username'] != username:
                raise ValueError('Existing owner username must match exactly')
            owner_id = owner['id']
            db.execute('UPDATE auth_users SET password_hash=? WHERE id=?', (encoded, owner_id))
            db.execute('DELETE FROM auth_sessions WHERE user_id=?', (owner_id,))
            db.execute('DELETE FROM auth_setup')
        auth.audit(db, dict(id=None, name=operator.strip()), operation, reason.strip())
    return dict(ok=True, operation=operation, owner_id=owner_id, username=username, mode='maintenance')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['initialize-owner', 'reset-owner'])
    parser.add_argument('--synthetic-root', required=True)
    parser.add_argument('--username', required=True)
    parser.add_argument('--name', default='')
    parser.add_argument('--operator', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--password-stdin', action='store_true', help='Read one password line from a protected pipe, never an argument')
    args = parser.parse_args(argv)
    if Path(args.synthetic_root).resolve() != checked_root():
        raise ValueError('Synthetic root must match the probe environment')
    password = sys.stdin.readline().rstrip('\r\n') if args.password_stdin else getpass.getpass('New password: ')
    try:
        with connect_synthetic() as connection:
            result = manage_owner(connection, operation=args.operation, username=args.username, name=args.name,
                                  password=password, operator=args.operator, reason=args.reason)
    finally:
        password = None  # Avoid retaining an additional reference; not a memory-erasure claim.
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
