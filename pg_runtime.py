"""Candidate PG runtime restricted to marked synthetic loopback environments.

No environment/DSN fallback, automatic migration or production enablement.
"""
from contextlib import contextmanager
from pathlib import Path
import threading
import psycopg
from psycopg import sql
from pg_service_adapter import ServiceConnection
from sql_dialect import AdmissionRejected
from test_isolation import checked_root
from pg_schema import expected_versions


class PostgresRuntime:
    def __init__(self, config, environment):
        # Reject non-synthetic use before looking up any credential.
        self.root = checked_root()
        if config.get('synthetic_root') != str(self.root):
            raise ValueError('Candidate PostgreSQL runtime requires the explicit synthetic root')
        required = {'host', 'port', 'database', 'user', 'schema', 'writer_epoch', 'restore_generation', 'connection_limit'}
        if set(config) != required | {'synthetic_root'}:
            raise ValueError('Missing or unrecognized database configuration fields')
        if config['host'] != '127.0.0.1' or config['database'] != 'synthetic_probe' or config['user'] != 'synthetic_owner' or config['schema'] != 'board_candidate':
            raise ValueError('Only the new synthetic cluster is supported by this candidate runtime')
        if type(config['port']) is not int or not 1024 <= config['port'] <= 65535:
            raise ValueError('Explicit PostgreSQL port required')
        if type(config['connection_limit']) is not int or not 1 <= config['connection_limit'] <= 16:
            raise ValueError('Synthetic connection limit must be between 1 and 16')
        if type(config['writer_epoch']) is not int or config['writer_epoch'] < 1:
            raise ValueError('Explicit positive writer epoch required')
        if not isinstance(config['restore_generation'], str) or not config['restore_generation']:
            raise ValueError('Explicit restore generation required')
        if environment.get('PGSERVICE') or environment.get('PGSERVICEFILE'):
            raise ValueError('Inherited libpq service files are prohibited')
        password = environment.get('DOUYIN_BOARD_DATABASE_PASSWORD')
        if not password:
            raise ValueError('Explicit injected database password required')
        self.config = dict(config)
        self._password = password
        self._slots = threading.BoundedSemaphore(config['connection_limit'])

    @contextmanager
    def connect(self):
        if not self._slots.acquire(timeout=1):
            raise AdmissionRejected('DB_CONNECTION_LIMIT')
        connection = None
        try:
            connection = psycopg.connect(host='127.0.0.1', hostaddr='127.0.0.1', port=self.config['port'],
                dbname='synthetic_probe', user='synthetic_owner', password=self._password,
                connect_timeout=5, sslmode='disable', options='', application_name='synthetic-board-runtime',
                passfile=str(self.root / 'unused-pgpass'), autocommit=True)
            actual = Path(connection.execute('SHOW data_directory').fetchone()[0]).resolve()
            if actual != self.root / 'cluster' or connection.execute('SELECT kind FROM board_app.environment').fetchone() != ('synthetic-only',):
                raise ValueError('Synthetic cluster location/marker mismatch')
            connection.execute(sql.SQL('SET search_path={},pg_catalog').format(sql.Identifier(self.config['schema'])))
            db = ServiceConnection(connection, writer_epoch=self.config['writer_epoch'],
                restore_generation=self.config['restore_generation'], schema=self.config['schema'], scope_enabled=True)
            try:
                yield db
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            if connection is not None:
                connection.close()
            self._slots.release()

    def validate_startup(self):
        with self.connect() as db:
            rows = [tuple(row.values) for row in db.execute('SELECT version,sha256 FROM schema_migrations ORDER BY version')]
            if rows != expected_versions():
                raise ValueError('Schema version does not match the reviewed runtime')
            if not db.execute("SELECT 1 FROM auth_users WHERE role='owner'").fetchone():
                raise ValueError('Controlled owner initialization is required before startup')
            if not db.execute('SELECT 1 FROM runtime_control WHERE id=1').fetchone():
                raise ValueError('Runtime control missing')
            if not db.execute('SELECT 1 FROM collection_settings WHERE id=1').fetchone() or not db.execute('SELECT 1 FROM collection_worker WHERE id=1').fetchone():
                raise ValueError('Controlled collection-disabled settings are required')
