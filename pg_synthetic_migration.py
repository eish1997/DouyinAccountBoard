"""Synthetic-only migration rehearsal. Not a production migration entrypoint."""
import json
import os
import uuid
from pathlib import Path
import psycopg
from psycopg import sql
from test_isolation import PROJECT, checked_root
from pg_schema import apply_migrations

MIGRATIONS = PROJECT / 'migrations/postgresql'
SCHEMA = 'board_candidate'
TABLES = ('computers', 'phone_devices', 'accounts', 'daily', 'imports', 'auth_users',
          'auth_sessions', 'auth_setup', 'auth_limits', 'auth_audit', 'auth_migrations',
          'phone_cards', 'collection_jobs', 'collection_settings', 'collection_worker',
          'publishing_layout', 'publishing_layout_state', 'publishing_layout_links',
          'operations_history', 'operations_previews', 'nurture_records', 'nurture_images')
IDENTITIES = ('auth_users', 'auth_audit', 'phone_cards', 'operations_history')


def connect_synthetic():
    root = checked_root()
    if any(os.environ.get(key) != value for key, value in {
        'PGHOST': '127.0.0.1', 'PGDATABASE': 'synthetic_probe', 'PGUSER': 'synthetic_owner'
    }.items()):
        raise ValueError('Explicit synthetic endpoint required')
    conn = psycopg.connect(autocommit=True, connect_timeout=5)
    try:
        if Path(conn.execute('SHOW data_directory').fetchone()[0]).resolve() != root / 'cluster':
            raise ValueError('Wrong synthetic cluster directory')
        if conn.execute('SELECT kind FROM board_app.environment').fetchone() != ('synthetic-only',):
            raise ValueError('Wrong synthetic cluster marker')
    except BaseException:
        conn.close()
        raise
    return conn


def install_schema(conn):
    return apply_migrations(conn)


def copy_synthetic(conn, source):
    """Caller supplies a freshly generated SQLite connection, never a file path.

    All tables retained for type-fidelity evaluation, including synthetic sessions.
    Production session invalidation/recovery classification is a different contract.
    """
    databases = source.execute('PRAGMA database_list').fetchall()
    if len(databases) != 1 or Path(databases[0][2]).resolve() != checked_root() / 'data' / 'board.db':
        raise ValueError('Only this run freshly generated SQLite source is allowed')
    if source.in_transaction:
        raise ValueError('Source must not have a pending transaction')
    source.execute('BEGIN')
    try:
        return _copy_snapshot(conn, source)
    finally:
        source.rollback()  # End read snapshot; never change the generated source.


def _copy_snapshot(conn, source):
    actual = source.execute('SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name').fetchall()
    expected = json.loads((MIGRATIONS / 'legacy-sqlite-schema.json').read_text(encoding='utf-8-sig'))
    if [list(row) for row in actual] != expected:
        raise ValueError('Unreviewed SQLite schema; migration refused')
    with conn.transaction():
        conn.execute('SET LOCAL search_path=board_candidate,pg_catalog')
        conn.execute('SET CONSTRAINTS accounts_parent_account_id_fkey DEFERRED')
        conn.execute("SET LOCAL lock_timeout='5s'")
        control = conn.execute('SELECT mode FROM runtime_control WHERE id=1 FOR UPDATE').fetchone()
        if control != ('maintenance',):
            raise ValueError('Migration requires maintenance')
        for table in TABLES + ('offices','user_office_grants','account_offices','computer_offices','phone_card_offices','phone_device_offices'):
            if conn.execute(sql.SQL('SELECT 1 FROM {} LIMIT 1').format(sql.Identifier(table))).fetchone():
                raise ValueError('Migration target must be empty')
        counts = {}
        for table in TABLES:
            cursor = source.execute('SELECT * FROM "' + table + '"')
            columns = [column[0] for column in cursor.description]
            target_columns = [row[0] for row in conn.execute(
                'SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position',
                (SCHEMA, table))]
            extras = {'auth_users': ['authorization_version'], 'operations_history': ['scope_offices']}.get(table, [])
            if columns + extras != target_columns:
                raise ValueError('Column drift: ' + table)
            rows = [tuple(row) for row in cursor.fetchall()]
            query = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(sql.Identifier(table),
                sql.SQL(',').join(map(sql.Identifier, columns)), sql.SQL(',').join(sql.Placeholder() for _ in columns))
            with conn.cursor() as target:
                target.executemany(query, rows)
            # Compare full values, including bytes, NULL and empty strings, before commit.
            copied = conn.execute(sql.SQL('SELECT {} FROM {}').format(sql.SQL(',').join(map(sql.Identifier, columns)), sql.Identifier(table))).fetchall()
            if sorted(rows, key=repr) != sorted(copied, key=repr):
                raise ValueError('Field mismatch: ' + table)
            counts[table] = len(rows)
        for table in IDENTITIES:
            maximum = conn.execute(sql.SQL('SELECT max(id) FROM {}').format(sql.Identifier(table))).fetchone()[0]
            # RESTART is transactional, unlike setval; failed rehearsals must not leak sequence changes.
            sequence = conn.execute('SELECT pg_get_serial_sequence(%s,%s)', (SCHEMA + '.' + table, 'id')).fetchone()[0]
            conn.execute(sql.SQL('ALTER SEQUENCE {} RESTART WITH {}').format(
                sql.Identifier(*sequence.split('.')), sql.Literal(max(1, (maximum or 0) + 1))))
    return counts


def first_cutover_synthetic(conn, source):
    """First-switch policy, distinct from field-fidelity and same-DB restart.

    Keep credentials and history; revoke sessions, previews and worker execution.
    Changes remain in the same transaction as the verified copy.
    """
    with conn.transaction():
        counts = copy_synthetic(conn, source)
        conn.execute('DELETE FROM board_candidate.auth_sessions')
        conn.execute('DELETE FROM board_candidate.auth_setup')
        conn.execute('DELETE FROM board_candidate.operations_previews')
        conn.execute("UPDATE board_candidate.collection_jobs SET token=''")
        conn.execute("UPDATE board_candidate.collection_worker SET seen=0,status='',command=''")
        conn.execute("UPDATE board_candidate.collection_settings SET auto_enabled=0,paused=1,pause_reason='initial-cutover'")
        generation = str(uuid.uuid4())
        conn.execute("UPDATE board_candidate.runtime_control SET mode='maintenance',restore_generation=%s WHERE id=1", (generation,))
    return dict(source_counts=counts, removed_sessions=counts['auth_sessions'],
                removed_setup_tokens=counts['auth_setup'], removed_previews=counts['operations_previews'],
                invalidated_worker_tokens=counts['collection_jobs'], restore_generation= generation, mode='maintenance')
