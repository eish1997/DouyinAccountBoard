import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing
import backup


class BackupTest(unittest.TestCase):
    def test_live_database_interval_and_retention(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'board.db';directory=Path(tmp)/'automatic'
            with closing(sqlite3.connect(source)) as db:
                db.execute('CREATE TABLE example(value TEXT)')
                db.execute("INSERT INTO example VALUES('已保存')")
                db.commit()
                first=backup.automatic(source,directory)
                with closing(sqlite3.connect(first)) as copy:
                    self.assertEqual(copy.execute('SELECT value FROM example').fetchone()[0],'已保存')
                    self.assertEqual(copy.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(backup.automatic(source,directory),first)
                old=time.time()-backup.RETENTION-100
                os.utime(first,(old,old))
                manual=directory/'manual.db';manual.write_bytes(b'keep')
                os.utime(manual,(old,old))
                second=backup.automatic(source,directory)
                self.assertNotEqual(second,first)
                self.assertFalse(first.exists())
                self.assertEqual(manual.read_bytes(),b'keep')

    def test_failed_copy_preserves_existing_backups_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);directory=root/'automatic';directory.mkdir()
            previous=directory/'auto-old.db';previous.write_bytes(b'previous')
            old=time.time()-backup.RETENTION-100;os.utime(previous,(old,old))
            with self.assertRaises(sqlite3.Error):
                backup.automatic(root/'missing.db',directory)
            self.assertEqual(previous.read_bytes(),b'previous')
            self.assertFalse((root/'missing.db').exists())
            invalid=root/'invalid.db';invalid.write_bytes(b'broken database')
            target=root/'manual.db';target.write_bytes(b'existing manual backup')
            with self.assertRaises(sqlite3.Error):backup.snapshot(invalid,target)
            self.assertEqual(target.read_bytes(),b'existing manual backup')
            self.assertEqual(list(root.glob('*.tmp')),[])
            with self.assertRaises(ValueError):backup.snapshot(invalid,invalid)

    def test_failure_status_and_restart_recovery(self):
        original=backup.status()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                source=Path(tmp)/'data'/'board.db';source.parent.mkdir()
                with closing(sqlite3.connect(source)) as db:db.execute('CREATE TABLE example(value TEXT)');db.commit()
                with patch.object(backup.threading,'Thread'),patch.object(backup,'automatic',side_effect=OSError('disk full')):
                    backup._state['enabled']=False;backup.start(source)
                    self.assertTrue(backup.status()['error'])
                with patch.object(backup.threading,'Thread'):
                    backup._state['enabled']=False;backup.start(source)
                    self.assertEqual(backup.status()['error'],'')
                    self.assertTrue(backup.status()['last_backup'])
        finally:
            backup._state.clear();backup._state.update(original)


if __name__=='__main__':unittest.main()
