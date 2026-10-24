"""Explicit config, bounded connection count and fail-closed startup checks."""
import json
import os
from test_isolation import checked_root, install
from pg_synthetic_migration import connect_synthetic, install_schema
from pg_runtime import PostgresRuntime
from sql_dialect import AdmissionRejected


def main():
    root = checked_root()
    install(root)
    from pg_admin import manage_owner
    config = dict(host='127.0.0.1', port=int(os.environ['PGPORT']), database='synthetic_probe',
                  user='synthetic_owner', schema='board_candidate', writer_epoch=1,
                  restore_generation='synthetic-runtime-1', connection_limit=1, synthetic_root=str(root))
    environment = {'DOUYIN_BOARD_DATABASE_PASSWORD': os.environ['PGPASSWORD']}
    for invalid in ({}, {**config, 'host': 'example.invalid'}, {**config, 'connection_limit': 0},
                    {**config, 'password': 'must-not-be-in-config'}, {**config, 'writer_epoch': True}):
        try:
            PostgresRuntime(invalid, environment)
            raise AssertionError('Invalid config accepted')
        except ValueError:
            pass
    try:
        PostgresRuntime(config, {})
        raise AssertionError('Missing injected credential accepted')
    except ValueError:
        pass
    with connect_synthetic() as conn:
        install_schema(conn)
    runtime = PostgresRuntime(config, environment)
    try:
        runtime.validate_startup()
        raise AssertionError('Uninitialized owner accepted')
    except ValueError as error:
        assert 'owner' in str(error)
    with connect_synthetic() as conn:
        manage_owner(conn, operation='initialize-owner', username='synthetic_owner', name='合成管理员',
                     password='Synthetic-runtime-password-73', operator='synthetic-test', reason='synthetic-init')
    runtime.validate_startup()
    with runtime.connect() as first:
        try:
            with runtime.connect():
                raise AssertionError('Connection budget exceeded')
        except AdmissionRejected as error:
            assert error.code == 'DB_CONNECTION_LIMIT'
        assert first.execute('SELECT current_schema()').fetchone()[0] == 'board_candidate'
    try:
        with runtime.connect() as db:
            db.execute('BEGIN')
            db.execute("INSERT INTO computers(id,name) VALUES('synthetic-rollback','合成回滚')")
            raise RuntimeError('synthetic failure')
    except RuntimeError:
        pass
    with runtime.connect() as db:
        assert db.execute('SELECT count(*) FROM computers').fetchone()[0] == 0
        assert db.execute('SELECT mode FROM runtime_control').fetchone()[0] == 'maintenance'
    print(json.dumps({'passed': True, 'checks': ['explicit-config-rejections', 'injected-password-required',
        'startup-rejects-missing-owner', 'startup-after-controlled-initialization', 'bounded-connections',
        'connection-slot-released', 'transaction-rollback-on-error', 'startup-does-not-open-maintenance']}, ensure_ascii=True))


if __name__ == '__main__':
    main()
