"""Synthetic SQLite snapshot baseline only; not disaster recovery acceptance."""
from contextlib import closing
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4
import app
import backup
from test_isolation import checked_root
from test_release_core import png


def summary(path):
    with closing(sqlite3.connect(path)) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result = {}
        for table in tables:
            # Names come from a synthetic schema; quote identifiers, never interpolate user SQL.
            quoted = '"' + table.replace('"', '""') + '"'
            rows = [[{'blob_sha256': hashlib.sha256(value).hexdigest()} if isinstance(value, bytes) else value for value in row]
                    for row in db.execute('SELECT * FROM ' + quoted)]
            encoded = sorted(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
            result[table] = {'count': len(rows), 'digest': hashlib.sha256('\n'.join(encoded).encode()).hexdigest()}
        return result


class SnapshotBaselineTest(unittest.TestCase):
    def test_snapshot_and_copy_restore_preserve_rows_and_blob(self):
        folder = checked_root() / ('snapshot-' + uuid4().hex)
        folder.mkdir()
        source, snapshot, restored = (folder / name for name in ('source.db', 'snapshot.db', 'restored.db'))
        with patch.object(app, 'DB', source):
            app.initialize()
            with app.connect() as db:
                db.execute('CREATE TABLE synthetic_blob_probe(id TEXT PRIMARY KEY, content BLOB, nullable TEXT, leading_zero TEXT)')
                db.execute('INSERT INTO synthetic_blob_probe VALUES(?,?,?,?)', ('合成图片', png(), None, '0000123'))
                db.execute("UPDATE accounts SET note='合成快照之前'")
            expected = summary(source)
            backup.snapshot(source, snapshot)
            with app.connect() as db:
                db.execute("UPDATE accounts SET note='合成快照之后'")
            backup.snapshot(snapshot, restored)
            self.assertTrue(summary(restored) == expected, 'Restored table counts or digests differ')
            self.assertFalse(summary(source) == expected, 'Source mutation was not preserved')
            with closing(sqlite3.connect(restored)) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
            with self.assertRaises(ValueError):
                backup.snapshot(source, source)
