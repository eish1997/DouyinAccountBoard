"""CloudBase PostgreSQL runtime.

Local synthetic tests stay on 127.0.0.1 and synthetic_probe. This module never
reads a cluster directory, never opens data/board.db, and never creates tables.
"""
from contextlib import contextmanager
from pathlib import Path
import threading
import psycopg
from psycopg import sql
from pg_service_adapter import ServiceConnection
from pg_schema import expected_versions

SCHEMA = 'board_candidate'
ALLOWED_SSL = frozenset({'require', 'verify-ca', 'verify-full'})
PASSFILE = Path(__file__).with_name('.cloudbase-no-pgpass')


def settings(environment):
    if environment.get('PGSERVICE') or environment.get('PGSERVICEFILE') or environment.get('DATABASE_URL'):
        raise ValueError('CloudBase 模式不使用连接串或 libpq 服务文件')
    if environment.get('DOUYIN_BOARD_DATA_DIR') or environment.get('DOUYIN_BOARD_TEST_ROOT'):
        raise ValueError('CloudBase 模式不使用 SQLite 数据目录或本机合成测试根')
    host = environment.get('DOUYIN_BOARD_DATABASE_HOST', '').strip()
    name = environment.get('DOUYIN_BOARD_DATABASE_NAME', '').strip()
    user = environment.get('DOUYIN_BOARD_DATABASE_USER', '').strip()
    sslmode = environment.get('DOUYIN_BOARD_DATABASE_SSLMODE', '').strip()
    port_text = environment.get('DOUYIN_BOARD_DATABASE_PORT', '5432').strip()
    password = environment.get('DOUYIN_BOARD_DATABASE_PASSWORD')
    if not host or any(char in host for char in '/@\\ \t:?') or host.lower() in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('CloudBase 数据库地址必须是云上主机名')
    if not name or name == 'synthetic_probe' or any(char in name for char in '/@\\ \t'):
        raise ValueError('CloudBase 库名无效')
    if not user or user == 'synthetic_owner' or any(char in user for char in '/@\\ \t'):
        raise ValueError('CloudBase 数据库用户无效')
    if sslmode not in ALLOWED_SSL:
        raise ValueError('CloudBase 数据库必须启用 TLS')
    if not port_text.isascii() or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise ValueError('CloudBase 数据库端口无效')
    if not isinstance(password, str) or not password:
        raise ValueError('必须提供 DOUYIN_BOARD_DATABASE_PASSWORD')
    if password in host or password in name or password in user:
        raise ValueError('数据库密码不能写进地址、库名或用户')
    rootcert = environment.get('DOUYIN_BOARD_DATABASE_SSLROOTCERT', '').strip()
    if sslmode == 'require' and rootcert:
        raise ValueError('require 模式不读取 CA 文件')
    if sslmode != 'require' and not rootcert:
        raise ValueError('校验证书时必须提供 CA 文件路径')
    return dict(host=host, port=int(port_text), dbname=name, user=user, sslmode=sslmode,
                sslrootcert=rootcert or None, password=password)


@contextmanager
def connect_database(environment):
    chosen = settings(environment)
    password = chosen.pop('password')
    kwargs = dict(host=chosen['host'], port=chosen['port'], dbname=chosen['dbname'], user=chosen['user'],
                  password=password, sslmode=chosen['sslmode'], connect_timeout=10,
                  application_name='douyin-board-cloudbase', passfile=str(PASSFILE), autocommit=True)
    if chosen['sslrootcert']:
        kwargs['sslrootcert'] = chosen['sslrootcert']
    connection = psycopg.connect(**kwargs)
    try:
        yield connection
    finally:
        connection.close()


class CloudBaseRuntime:
    def __init__(self, environment):
        if environment.get('DOUYIN_BOARD_OWNER_PASSWORD'):
            raise ValueError('网站进程不能携带总管理员密码')
        self._environment = environment
        self._slots = threading.BoundedSemaphore(8)
        self.writer_epoch = None
        self.restore_generation = None
        settings(environment)

    @contextmanager
    def connect(self):
        if self.writer_epoch is None or not self.restore_generation:
            raise ValueError('CloudBase 运行时尚未完成启动检查')
        if not self._slots.acquire(timeout=1):
            from sql_dialect import AdmissionRejected
            raise AdmissionRejected('DB_CONNECTION_LIMIT')
        connection = None
        try:
            with connect_database(self._environment) as connection:
                connection.execute(sql.SQL('SET search_path={},pg_catalog').format(sql.Identifier(SCHEMA)))
                database = ServiceConnection(connection, writer_epoch=self.writer_epoch,
                    restore_generation=self.restore_generation, schema=SCHEMA, scope_enabled=True)
                try:
                    yield database
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        finally:
            self._slots.release()

    def validate_startup(self):
        with connect_database(self._environment) as connection:
            connection.execute(sql.SQL('SET search_path={},pg_catalog').format(sql.Identifier(SCHEMA)))
            rows = [tuple(row) for row in connection.execute('SELECT version,sha256 FROM schema_migrations ORDER BY version')]
            if rows != expected_versions():
                raise ValueError('云上结构未按单独迁移准备好，启动不会自动建表')
            if not connection.execute("SELECT 1 FROM auth_users WHERE role='owner'").fetchone():
                raise ValueError('云上还没有总管理员，启动不会自动创建')
            control = connection.execute('SELECT mode,writer_epoch,restore_generation FROM runtime_control WHERE id=1').fetchone()
            if not control or control[0] != 'open':
                raise ValueError('云上数据库仍在维护状态，请先完成一次性初始化')
            if not connection.execute('SELECT 1 FROM collection_settings WHERE id=1').fetchone():
                raise ValueError('采集关闭设置尚未写入')
            if not connection.execute('SELECT 1 FROM collection_worker WHERE id=1').fetchone():
                raise ValueError('采集关闭设置尚未写入')
            self.writer_epoch = int(control[1])
            self.restore_generation = control[2]
