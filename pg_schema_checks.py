"""Upgrade a populated synthetic v1 without inferring office grants."""
import json
import psycopg
from test_isolation import checked_root, install
from pg_synthetic_migration import connect_synthetic
from pg_schema import MIGRATIONS, FILES, expected_versions, apply_migrations


def main():
    install(checked_root())
    with connect_synthetic() as conn:
        with conn.transaction():
            conn.execute('CREATE SCHEMA board_candidate')
            conn.execute('SET LOCAL search_path=board_candidate,pg_catalog')
            conn.execute('CREATE TABLE schema_migrations(version bigint PRIMARY KEY,sha256 text NOT NULL)')
            conn.execute((MIGRATIONS/FILES[0]).read_text(encoding='utf-8'))
            conn.execute('INSERT INTO schema_migrations VALUES(%s,%s)',expected_versions()[0])
            conn.execute("INSERT INTO computers(id,name) VALUES('office3:9','合成旧电脑')")
            conn.execute("INSERT INTO accounts(id,computer_id,office_group) VALUES('synthetic-legacy','office3:9','three')")
            conn.execute("INSERT INTO auth_users(username,name,role,password_hash,created_at) VALUES('synthetic_employee','合成员工','employee','synthetic-only-hash',1)")
            conn.execute("UPDATE runtime_control SET mode='open'")
        try:
            apply_migrations(conn)
            raise AssertionError('Open-state migration accepted')
        except ValueError as error:
            assert 'maintenance' in str(error)
        assert conn.execute("SELECT to_regclass('board_candidate.offices')").fetchone()[0] is None
        conn.execute("UPDATE board_candidate.runtime_control SET mode='maintenance'")
        conn.execute('CREATE TABLE board_candidate.phone_card_offices(synthetic_conflict text)')
        try:
            apply_migrations(conn)
            raise AssertionError('Injected DDL conflict did not fail')
        except psycopg.errors.DuplicateTable:
            pass
        assert conn.execute("SELECT to_regclass('board_candidate.offices')").fetchone()[0] is None
        assert conn.execute('SELECT version,sha256 FROM board_candidate.schema_migrations').fetchall()==expected_versions()[:1]
        conn.execute('DROP TABLE board_candidate.phone_card_offices')
        assert apply_migrations(conn)=='applied'
        assert apply_migrations(conn)=='already-applied'
        assert conn.execute('SELECT version,sha256 FROM board_candidate.schema_migrations ORDER BY version').fetchall()==expected_versions()
        for table in ('offices','user_office_grants','account_offices','computer_offices','phone_card_offices','phone_device_offices'):
            assert conn.execute('SELECT count(*) FROM board_candidate.'+table).fetchone()[0]==0
        assert conn.execute('SELECT authorization_version FROM board_candidate.auth_users').fetchone()[0]==1
        assert conn.execute('SELECT id,office_group FROM board_candidate.accounts').fetchone()==('synthetic-legacy','three')
        assert conn.execute('SELECT mode FROM board_candidate.runtime_control').fetchone()[0]=='maintenance'
        conn.execute("INSERT INTO board_candidate.offices(id,name) VALUES('synthetic-a','合成办公室A')")
        conn.execute("INSERT INTO board_candidate.account_offices VALUES('synthetic-legacy','synthetic-a')")
        try:
            with conn.transaction():
                conn.execute("INSERT INTO board_candidate.account_offices VALUES('synthetic-legacy','synthetic-a')")
            raise AssertionError('Duplicate account office accepted')
        except psycopg.errors.UniqueViolation:
            pass
        try:
            with conn.transaction():
                conn.execute("INSERT INTO board_candidate.user_office_grants VALUES(1,'synthetic-missing')")
            raise AssertionError('Missing office grant accepted')
        except psycopg.errors.ForeignKeyViolation:
            pass
    print(json.dumps({'passed':True,'checks':['open-state-upgrade-rejected','failed-ddl-transaction-rollback',
        'populated-v1-to-v2-upgrade','repeat-upgrade-no-op','no-office-prefix-inference','default-no-user-grants',
        'authorization-version-default','single-office-unique-key','grant-office-foreign-key','maintenance-preserved']},ensure_ascii=True))


if __name__=='__main__':main()
