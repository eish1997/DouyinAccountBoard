"""Run once against an empty CloudBase PostgreSQL database.

Applies the reviewed migrations and, when no owner exists, creates the owner.
Does not read data/board.db and does not copy customer data. A second run does
not create another owner and does not change an already open generation.
"""
import getpass
import json
import os
import sys
from psycopg import sql
from cloudbase_runtime import SCHEMA, connect_database
from pg_admin import manage_owner
from pg_schema import apply_migrations

OPEN_GENERATION = 'cloudbase-initial'


def prepare(environment, username, name, password):
    with connect_database(environment) as connection:
        migration = apply_migrations(connection)
        connection.execute(sql.SQL('SET search_path={},pg_catalog').format(sql.Identifier(SCHEMA)))
        exists = connection.execute("SELECT 1 FROM auth_users WHERE role='owner'").fetchone()
        if exists:
            owner = 'already-present'
        else:
            if not username or not name or not password:
                raise ValueError('初始化总管理员需要姓名、登录名和密码')
            manage_owner(connection, operation='initialize-owner', username=username, name=name, password=password,
                         operator='cloudbase-prepare', reason='初始化空的 CloudBase 数据库')
            owner = 'initialized'
        mode = connection.execute('SELECT mode,restore_generation FROM runtime_control WHERE id=1').fetchone()
        if mode[0] == 'maintenance':
            connection.execute("UPDATE runtime_control SET mode='open', restore_generation=%s WHERE id=1 AND mode='maintenance'",
                               (OPEN_GENERATION,))
            opened = True
        elif mode[0] == 'open':
            opened = False
        else:
            raise ValueError('运行控制状态无法识别')
        current = connection.execute('SELECT mode FROM runtime_control WHERE id=1').fetchone()
    return dict(ok=True, migration=migration, owner=owner, opened=opened, mode=current[0])


def main(argv=None):
    parser_password = os.environ.get('DOUYIN_BOARD_OWNER_PASSWORD')
    username = os.environ.get('DOUYIN_BOARD_OWNER_USERNAME', '').strip()
    name = os.environ.get('DOUYIN_BOARD_OWNER_NAME', '').strip()
    if argv:
        raise ValueError('准备命令不接受参数，避免把密码放进命令行')
    if parser_password is None and not sys.stdin.isatty():
        parser_password = sys.stdin.readline().rstrip('\r\n')
    elif parser_password is None:
        parser_password = getpass.getpass('Owner password: ')
    try:
        result = prepare(os.environ, username, name, parser_password)
    finally:
        parser_password = None
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main(sys.argv[1:])
