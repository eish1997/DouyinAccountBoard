"""Scope component checks; response filtering and field permissions remain separate."""
from contextlib import contextmanager
import json
from test_isolation import checked_root, install
from pg_synthetic_migration import connect_synthetic, install_schema
from pg_service_adapter import ServiceConnection


def main():
    install(checked_root())
    import auth
    import office_scope as scope
    with connect_synthetic() as conn:
        install_schema(conn)
        conn.execute('SET search_path=board_candidate,pg_catalog')
        db = ServiceConnection(conn, writer_epoch=1, restore_generation='synthetic-scope-1', schema='board_candidate')
        with conn.transaction():
            setup = auth.mint_setup(db)
            token = auth.setup_owner(db, dict(username='synthetic_owner',password='Synthetic-scope-password-91'), 'board_setup='+setup)
            owner = auth.session(db, 'board_session='+token)
            employee_id = auth.save_user(db, owner, dict(username='synthetic_employee',password='Synthetic-scope-password-91'))
            admin_id = auth.save_user(db, owner, dict(username='synthetic_admin',role='admin',password='Synthetic-scope-password-91'))
            employee = {'id': employee_id}
            admin = {'id': admin_id}
            for ident in ('synthetic-a','synthetic-b','synthetic-unassigned','synthetic-new'):
                db.execute('INSERT INTO accounts(id,name) VALUES(?,?)', (ident, '合成账号'))
            db.execute("INSERT INTO phone_cards(id,phone) VALUES(1,'13800000001')")
            db.execute("INSERT INTO phone_devices(id,name) VALUES('1','合成手机')")
            db.execute("UPDATE runtime_control SET mode='open',restore_generation='synthetic-scope-1'")
        @contextmanager
        def write():
            db.begin_write()
            try:
                yield
                db.commit()
            except BaseException:
                db.rollback()
                raise
        def denied(action):
            try:
                action()
                raise AssertionError('Out-of-scope action accepted')
            except auth.AccessError:
                pass
        assert scope.visible_ids(db, employee, 'accounts') == set()
        with write():
            office_a = scope.create_office(db, owner, '合成办公室A')
            office_b = scope.create_office(db, owner, '合成办公室B')
            scope.assign_entity(db, owner, 'accounts', 'synthetic-a', [office_a])
            scope.assign_entity(db, owner, 'accounts', 'synthetic-b', [office_b])
            scope.assign_entity(db, owner, 'cards', 1, [office_a, office_b])
            scope.assign_entity(db, owner, 'devices', '1', [office_b])
            old_session = auth.new_session(db, employee_id)
            assert scope.grant_user(db, owner, employee_id, [office_a]) == 2
        assert auth.session(db, 'board_session='+old_session) is None
        assert scope.visible_ids(db, employee, 'accounts') == {'synthetic-a'}
        assert scope.visible_ids(db, employee, 'devices') == set()
        assert scope.visible_ids(db, employee, 'cards') == {1}
        denied(lambda: scope.require_entity(db, employee, 'accounts', 'synthetic-b'))
        denied(lambda: scope.require_entity(db, employee, 'accounts', 'synthetic-unassigned'))
        scope.require_entity(db, admin, 'accounts', 'synthetic-unassigned')
        denied(lambda: scope.require_entity(db, employee, 'cards', 1, write=True))
        with write():
            denied(lambda: scope.assign_entity(db, employee, 'accounts', 'synthetic-a', [office_b]))
            denied(lambda: scope.assign_entity(db, employee, 'accounts', 'synthetic-new', [], newly_created=True))
            denied(lambda: scope.assign_entity(db, employee, 'accounts', 'synthetic-new', [office_b], newly_created=True))
            scope.assign_entity(db, employee, 'accounts', 'synthetic-new', [office_a], newly_created=True)
            denied(lambda: scope.grant_user(db, admin, employee_id, [office_b]))
            denied(lambda: scope.grant_user(db, {'id': employee_id, 'role': 'owner'}, employee_id, [office_b]))
            assert scope.grant_user(db, owner, employee_id, [office_a, office_b]) == 3
        scope.require_entity(db, employee, 'cards', 1, write=True)
        assert scope.entity_offices(db, 'accounts', 'synthetic-a') == {office_a}
        audit = db.execute("SELECT target FROM auth_audit WHERE action='更新办公室授权' ORDER BY id DESC LIMIT 1").fetchone()[0]
        assert json.loads(audit)['authorization_version'] == 3
        try:
            scope.assign_entity(db, owner, 'accounts', 'synthetic-a', [office_b])
            raise AssertionError('Scope mutation without admission accepted')
        except ValueError:
            pass
        with write():
            db.execute('UPDATE auth_users SET active=0 WHERE id=?', (employee_id,))
        denied(lambda: scope.visible_ids(db, employee, 'accounts'))
    print(json.dumps({'passed':True,'kind':'scope-component-not-http-acceptance','checks':[
        'new-employee-no-scope','office-filtered-ids','unknown-scope-admin-only','cross-office-read-denied',
        'shared-read-intersection-write-all','old-and-new-assignment-scope','new-record-explicit-scope',
        'only-owner-grants-no-forged-role','grant-version-audit-and-session-revocation',
        'scope-mutation-requires-admission','disabled-user-reread-denied']},ensure_ascii=True))


if __name__=='__main__':main()
