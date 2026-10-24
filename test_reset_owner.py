import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
import auth
import reset_owner


class RecoveryTest(unittest.TestCase):
    def test_recovery_preserves_owner_and_business_and_revokes_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'data').mkdir();path=root/'data/board.db'
            with closing(sqlite3.connect(path)) as db:
                auth.migrate(db)
                db.execute('CREATE TABLE business(id INTEGER PRIMARY KEY,value TEXT)')
                db.execute("INSERT INTO business VALUES(1,'keep')")
                grant=auth.mint_setup(db)
                auth.setup_owner(db,dict(username='owner_one',password='old-test-password'), 'board_setup='+grant)
                db.commit()
                before=db.execute('SELECT id,username,name,role,permissions,active,created_at FROM auth_users').fetchall()
            result=reset_owner.reset_password(path,'new-test-password')
            self.assertTrue(Path(result['backup']).is_file())
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT id,username,name,role,permissions,active,created_at FROM auth_users').fetchall(),before)
                self.assertEqual(db.execute('SELECT * FROM business').fetchall(),[(1,'keep')])
                self.assertEqual(db.execute('SELECT count(*) FROM auth_sessions').fetchone()[0],0)
                hashed=db.execute('SELECT password_hash FROM auth_users').fetchone()[0]
                self.assertTrue(auth.password_matches('new-test-password',hashed))
                self.assertFalse(auth.password_matches('old-test-password',hashed))
                self.assertEqual(db.execute("SELECT count(*) FROM auth_audit WHERE action='本机管理员重置总账号密码'").fetchone()[0],1)
            with self.assertRaises(ValueError):reset_owner.reset_password(path,'short')
