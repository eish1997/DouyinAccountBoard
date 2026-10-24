"""Explicit, ordered schema migrations. Invocation is controlled, never startup."""
import hashlib
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent / 'migrations/postgresql'
FILES = ('0001_legacy_baseline.sql', '0002_office_scope.sql', '0003_idempotency.sql')


def expected_versions():
    return [(index, hashlib.sha256((MIGRATIONS / name).read_bytes()).hexdigest()) for index, name in enumerate(FILES, 1)]


def apply_migrations(connection):
    expected = expected_versions()
    with connection.transaction():
        connection.execute("SET LOCAL lock_timeout='5s'")
        connection.execute('SELECT pg_advisory_xact_lock(811032001)')
        exists = connection.execute("SELECT 1 FROM pg_namespace WHERE nspname='board_candidate'").fetchone()
        if not exists:
            connection.execute('CREATE SCHEMA board_candidate')
            connection.execute('CREATE TABLE board_candidate.schema_migrations(version bigint PRIMARY KEY,sha256 text NOT NULL)')
        current = connection.execute('SELECT version,sha256 FROM board_candidate.schema_migrations ORDER BY version').fetchall()
        if current != expected[:len(current)] or len(current) > len(expected):
            raise ValueError('Migration checksum/version mismatch; never overwrite')
        if current == expected:
            return 'already-applied'
        if current:
            mode = connection.execute('SELECT mode FROM board_candidate.runtime_control WHERE id=1 FOR UPDATE').fetchone()
            if mode != ('maintenance',):
                raise ValueError('Schema migration requires maintenance')
        connection.execute('SET LOCAL search_path=board_candidate,pg_catalog')
        for version, digest in expected[len(current):]:
            connection.execute((MIGRATIONS / FILES[version-1]).read_text(encoding='utf-8'))
            connection.execute('INSERT INTO schema_migrations VALUES(%s,%s)', (version, digest))
    return 'applied'
