"""Run only within probe_postgresql's disposable, synthetic cluster."""
import json
import sqlite3
import psycopg
from test_isolation import checked_root, install
from pg_synthetic_migration import connect_synthetic, install_schema, copy_synthetic, TABLES


def main():
    root = checked_root()
    if (root / 'data').exists():
        raise ValueError('Fresh generated source required')
    install(root)
    import app
    app.initialize()
    with sqlite3.connect(app.DB) as source:
        source.execute('PRAGMA foreign_keys=ON')
        source.executescript("""
          INSERT INTO phone_devices(id,name) VALUES('synthetic-device','合成手机');
          UPDATE accounts SET phone_device_id='synthetic-device',registration_phone='000123',note='',work_count=4294967296;
          INSERT INTO accounts(id,name,computer_id,slot) VALUES('synthetic-parent','合成主账号','synth-pc-1',2);
          UPDATE accounts SET parent_account_id='synthetic-parent' WHERE id='synth-account-1';
          INSERT INTO daily VALUES('synth-account-1','2026-10-21',1);
          INSERT INTO auth_users VALUES(41,'synthetic-owner','合成管理员','owner','[]',1,'synthetic-not-a-real-hash',1);
          INSERT INTO auth_sessions VALUES('synthetic-session',41,'synthetic-csrf',9999999999);
          INSERT INTO auth_setup VALUES('synthetic-setup',9999999999);
          INSERT INTO auth_limits VALUES('synthetic-limit',1,9999999999);
          INSERT INTO auth_audit VALUES(51,1,41,'合成人员','合成操作','合成对象');
          INSERT INTO phone_cards(id,phone) VALUES(61,'00001234567');
          INSERT INTO collection_jobs(account_id,homepage,token,state) VALUES('synth-account-1','','synthetic-inert-token','done');
          INSERT INTO publishing_layout VALUES('synthetic-batch','synth-pc-1',1,'synth-account-1','合成格子','合成办公室','','','synthetic');
          INSERT INTO publishing_layout_state VALUES(1,'synthetic-batch');
          INSERT INTO publishing_layout_links VALUES('synthetic-batch','synth-pc-1',1,'synthetic',41,1);
          INSERT INTO operations_history VALUES(71,'synthetic-group',1,41,'合成人员','synthetic','accounts:synth-account-1',NULL,'{"note":"合成"}');
          INSERT INTO operations_previews VALUES('synthetic-preview',41,9999999999,'synthetic','{}');
          INSERT INTO nurture_records(account_id) VALUES('synth-account-1') ON CONFLICT(account_id) DO NOTHING;
          INSERT INTO nurture_images VALUES('synthetic-image','synth-account-1',1,'2026-10-21','image/png',X'0001FF','合成人员');
        """)
        source.commit()
        with connect_synthetic() as target:
            assert install_schema(target) == 'applied'
            assert install_schema(target) == 'already-applied'
            source.execute('CREATE TABLE unreviewed_synthetic_table(id INTEGER)')
            source.commit()
            try:
                copy_synthetic(target, source)
                raise AssertionError('Unreviewed schema accepted')
            except ValueError as error:
                assert 'Unreviewed' in str(error)
            source.execute('DROP TABLE unreviewed_synthetic_table')
            source.commit()
            # Force a late-table constraint failure. Earlier inserts must all roll back.
            target.execute('ALTER TABLE board_candidate.nurture_images ADD CONSTRAINT synthetic_failure CHECK(octet_length(content)<1)')
            try:
                copy_synthetic(target, source)
                raise AssertionError('Expected late-table failure')
            except psycopg.errors.CheckViolation:
                pass
            for table in TABLES:
                assert target.execute('SELECT count(*) FROM board_candidate.' + table).fetchone() == (0,)
            target.execute('ALTER TABLE board_candidate.nurture_images DROP CONSTRAINT synthetic_failure')
            # SQLite can historically contain dangling references. PG must reject at commit.
            source.execute('PRAGMA foreign_keys=OFF')
            source.execute("UPDATE accounts SET parent_account_id='synthetic-missing' WHERE id='synth-account-1'")
            source.commit()
            try:
                copy_synthetic(target, source)
                raise AssertionError('Dangling parent accepted')
            except psycopg.errors.ForeignKeyViolation:
                pass
            assert target.execute('SELECT count(*) FROM board_candidate.accounts').fetchone() == (0,)
            source.execute("UPDATE accounts SET parent_account_id='synthetic-parent' WHERE id='synth-account-1'")
            source.commit()
            source.execute('PRAGMA foreign_keys=ON')
            counts = copy_synthetic(target, source)
            assert all(counts.values()), counts
            try:
                copy_synthetic(target, source)
                raise AssertionError('Nonempty target accepted')
            except ValueError as error:
                assert 'empty' in str(error)
            assert target.execute('SELECT mode FROM board_candidate.runtime_control').fetchone() == ('maintenance',)
            assert target.execute("INSERT INTO board_candidate.phone_cards(phone) VALUES('synthetic-next') RETURNING id").fetchone() == (62,)
            try:
                with target.transaction():
                    target.execute("INSERT INTO board_candidate.daily VALUES('missing-account','2026-10-21',1)")
                raise AssertionError('Foreign key not enforced')
            except psycopg.errors.ForeignKeyViolation:
                pass
            target.execute("UPDATE board_candidate.schema_migrations SET sha256='synthetic-drift'")
            try:
                install_schema(target)
                raise AssertionError('Checksum mismatch accepted')
            except ValueError as error:
                assert 'checksum' in str(error)
        print(json.dumps({'passed': True, 'table_counts': counts, 'checks': [
            '22-table-full-field-equality', 'late-failure-full-rollback', 'idempotent-schema',
            'nonempty-target-rejected', 'maintenance-default', 'identity-after-explicit-id',
            'foreign-key-enforced', 'migration-checksum-drift-rejected', 'source-schema-drift-rejected',
            'child-before-parent-preserved', 'dangling-parent-commit-rollback']}, ensure_ascii=True))


if __name__ == '__main__':
    main()
