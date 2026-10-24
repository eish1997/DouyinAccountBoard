"""Existing service functions against real, disposable PostgreSQL; no HTTP claim."""
import json
import sqlite3
from types import SimpleNamespace
from test_isolation import checked_root, install
from pg_synthetic_migration import connect_synthetic, install_schema
from pg_service_adapter import ServiceConnection, bind_qmarks


def main():
    install(checked_root())
    import auth
    import phone_cards
    import account_service
    import phone_devices
    import publishing_layout
    import operations
    import nurture
    import data_exchange
    from sql_dialect import integer_order
    with connect_synthetic() as connection:
        install_schema(connection)
        db = ServiceConnection(connection)
        connection.execute('SET search_path=board_candidate,pg_catalog')
        quoted = "SELECT '?' AS literal, ?::text AS value, '20%' AS percent, $$?%$$ AS dollar /* ? /* ? */ ? */ -- ?\n"
        row = db.execute(quoted, ("合成'; DROP TABLE auth_users; --",)).fetchone()
        assert dict(row) == {'literal': '?', 'value': "合成'; DROP TABLE auth_users; --", 'percent': '20%', 'dollar': '?%'}
        assert row[0] == '?' and row[-1] == '?%'
        for malformed in ("SELECT '", 'SELECT /*', 'SELECT $tag$?', 'SELECT ?', 'SELECT 1'):
            try:
                bind_qmarks(malformed, (1, 2))
                raise AssertionError('Malformed binding accepted')
            except ValueError:
                pass
        password = 'Synthetic-only-password-51'
        with connection.transaction():
            setup = auth.mint_setup(db)
            token = auth.setup_owner(db, dict(username='synthetic-owner', name='合成管理员', password=password), 'board_setup=' + setup)
            owner = auth.session(db, 'board_session=' + token)
            assert owner['role'] == 'owner' and owner['name'] == '合成管理员'
            employee = auth.save_user(db, owner, dict(username='synthetic-worker', name='合成员工', password=password))
            assert employee > owner['id']
        for _ in range(2):
            with connection.transaction():
                assert auth.login(db, dict(username='synthetic-worker', password='invalid-synthetic'), '127.0.0.1') is None
        assert db.execute('SELECT max(failures) FROM auth_limits').fetchone()[0] == 2
        with connection.transaction():
            employee_token = auth.login(db, dict(username='synthetic-worker', password=password), '127.0.0.1')
            assert auth.session(db, 'board_session=' + employee_token)['id'] == employee
            auth.save_user(db, owner, dict(id=employee, name='合成员工', active=False))
            assert auth.session(db, 'board_session=' + employee_token) is None
        with connection.transaction():
            card = phone_cards.save(db, dict(phone='13800000001', label='合成卡'))
            assert phone_cards.save(db, dict(id=card, phone='13800000001', label='合成更新')) == card
            db.execute("INSERT INTO computers(id,name) VALUES('synthetic-computer','合成电脑')")
            db.execute("INSERT INTO accounts(id,computer_id,registration_phone) VALUES('synthetic-account','synthetic-computer','13800000002')")
            assert phone_cards.import_accounts(db) == 1
            assert phone_cards.import_accounts(db) == 0
        try:
            with connection.transaction():
                phone_cards.save(db, dict(id=card, phone='13800000001', label='must-rollback'))
                raise RuntimeError('synthetic rollback')
        except RuntimeError:
            pass
        assert db.execute('SELECT label FROM phone_cards WHERE id=?', (card,)).fetchone()[0] == '合成更新'
        assert len(phone_cards.overview(db)['cards']) == 2
        # Compare numeric-prefix ordering against the existing SQLite semantics.
        values = [None, '', 'office3:2', '02', '2abc', '-5', '+4', ' 3', '999999999999999999999', '-999999999999999999999', '10']
        with sqlite3.connect(':memory:') as old:
            old.execute('CREATE TABLE sort_probe(id TEXT, position INTEGER)')
            db.execute('CREATE TEMP TABLE sort_probe(id TEXT, position INTEGER)')
            for position, value in enumerate(values):
                old.execute('INSERT INTO sort_probe VALUES(?,?)', (value, position))
                db.execute('INSERT INTO sort_probe VALUES(?,?)', (value, position))
            expected = [row[0] for row in old.execute('SELECT position FROM sort_probe ORDER BY CAST(id AS INTEGER),position')]
            actual = [row[0] for row in db.execute('SELECT position FROM sort_probe ORDER BY ' + integer_order(db, 'id') + ',position')]
            assert actual == expected, (actual, expected)
        with connection.transaction():
            path = '/api/account/create'
            body = dict(computer_id='synthetic-computer', slot=1, name='合成新增账号', status='正常', douyin_id='synthetic_new')
            before = operations.before(db, path, body)
            account = account_service.save(db, body, owner, '2026-10-21', create=True)
            assert account.startswith('account-') and len(account) == 40
            handler = SimpleNamespace()
            operations.finish(handler, db, owner, path, before)
            assert 'account:' + account in handler._version_delta
            assert operations.history(db, 'account:' + account)['entries']
            phone_devices.save(db, dict(id='2', state='在用'), create=True)
            phone_devices.assign(db, account, '2')
            assert phone_devices.overview(db, [dict(row) for row in db.execute('SELECT * FROM accounts')])['devices'][0]['id'] == '2'
            db.execute("INSERT INTO publishing_layout VALUES('synthetic-batch','synthetic-computer',1,NULL,'合成格子','合成办公室','A1','','未关联')")
            db.execute("INSERT INTO publishing_layout_state VALUES(1,'synthetic-batch')")
            state = operations.snapshot(db)
            publishing_layout.link(db, dict(batch='synthetic-batch', computer_id='synthetic-computer', slot=1, expected_account_id=None, account_id=account), owner['id'])
            operations.finish(handler, db, owner, '/api/publishing/link', (state, []))
            assert publishing_layout.overview(db)['cells'][0]['account_id'] == account
            assert any(entry['entity'].startswith('layout:') for entry in operations.history(db, 'account:' + account)['entries'])
            nurture.post(db, owner, '/join', dict(id=account), '2026-10-21')
            nurture.post(db, owner, '/verification', dict(id=account, verification_state='核对已通过'), '2026-10-21')
            nurture.post(db, owner, '/start', dict(id=account), '2026-10-21')
            import base64
            from test_release_core import png
            content = png()
            nurture.post(db, owner, '/upload', dict(id=account, day='2026-10-21', content=base64.b64encode(content).decode()), '2026-10-21')
            assert db.execute('SELECT content FROM nurture_images WHERE account_id=?', (account,)).fetchone()[0] == content
            assert nurture.overview(db, '2026-10-21')['records'][0]['image_count'] == 1
        with connection.transaction():
            csv = '手机卡号码,卡名称,备注\n13800000003,合成导入一,0000123\n13800000004,合成导入二,=synthetic\n'
            rows = data_exchange.read_file('synthetic.csv', base64.b64encode(csv.encode('utf-8-sig')).decode())
            preview = data_exchange.plan_import(db, owner, 'cards', rows, '2026-10-21')
            assert [row['action'] for row in preview['rows']] == ['create', 'create']
            assert db.execute("SELECT count(*) FROM phone_cards WHERE phone IN ('13800000003','13800000004')").fetchone()[0] == 0
        with connection.transaction():
            phone_cards.save(db, dict(phone='13800000004', label='合成并发新增'))
        try:
            with connection.transaction():
                data_exchange.apply_import(db, owner, dict(preview_id=preview['preview_id'], rows=[2, 3]), '2026-10-21')
                raise AssertionError('Late conflict did not abort import')
        except ValueError:
            pass
        assert not db.execute("SELECT 1 FROM phone_cards WHERE phone='13800000003'").fetchone()
        assert db.execute('SELECT 1 FROM operations_previews WHERE id=?', (preview['preview_id'],)).fetchone()
        with connection.transaction():
            assert data_exchange.apply_import(db, owner, dict(preview_id=preview['preview_id'], rows=[2]), '2026-10-21')['imported'] == 1
        assert db.execute("SELECT note FROM phone_cards WHERE phone='13800000003'").fetchone()[0] == '0000123'
        with connection.transaction():
            db.execute("ALTER TABLE phone_cards ADD CONSTRAINT synthetic_preview_failure CHECK(label<>'synthetic-fail')")
            preview = data_exchange.plan_import(db, owner, 'cards', [
                ['手机卡号码', '卡名称'], ['13800000005', 'synthetic-fail'], ['13800000006', '合成后续有效行']], '2026-10-21')
            assert [row['action'] for row in preview['rows']] == ['error', 'create']
            assert preview['rows'][0]['error'] == '数据约束不满足，请核对必填项、唯一值及关联资料'
            db.execute('ALTER TABLE phone_cards DROP CONSTRAINT synthetic_preview_failure')
    print(json.dumps({'passed': True, 'checks': ['quoted-qmark-percent-and-injection-as-value',
        'invalid-binding-rejected', 'owner-setup-and-session', 'employee-create-login-limit-upsert',
        'disable-revokes-session', 'card-returning-id-update', 'card-import-idempotent', 'service-rollback',
        'numeric-order-equivalence', 'account-create-audit-and-history', 'device-assignment',
        'publishing-link-json-history', 'nurture-png-bytea-roundtrip', 'csv-preview-no-business-write',
        'csv-late-conflict-full-rollback-preview-retained', 'csv-leading-zeros',
        'pg-constraint-error-savepoint-recovery']}, ensure_ascii=True))


if __name__ == '__main__':
    main()
