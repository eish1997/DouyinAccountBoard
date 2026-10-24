import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from uuid import uuid4
from unittest.mock import patch
import test_isolation as isolation


class IsolationTest(unittest.TestCase):
    def directory(self):
        path = isolation.checked_root() / ('case-' + uuid4().hex)
        path.mkdir()
        return path

    def test_missing_and_protected_roots(self):
        for value in ('', isolation.PROJECT / 'data', isolation.PROJECT / '.synthetic-tests'):
            with self.assertRaises(ValueError):
                isolation.validate_root(value)

    def test_protected_reads_denied_before_open(self):
        for directory in isolation.PROTECTED:
            with self.assertRaises(PermissionError):
                (directory / 'never-read-probe').read_bytes()

    def test_database_escape_denied(self):
        with self.assertRaises(ValueError):
            sqlite3.connect(isolation.PROJECT / 'data' / 'never-open.db')

    def test_non_loopback_bind_denied(self):
        with socket.socket() as sock:
            with self.assertRaises(PermissionError):
                sock.bind(('0.0.0.0', 0))

    def test_marker_is_required(self):
        tmp = self.directory()
        with patch.dict(os.environ, {'DOUYIN_BOARD_TEST_ROOT': str(tmp)}):
                with self.assertRaises(FileNotFoundError):
                    isolation.checked_root()

    def test_initialize_uses_only_synthetic_seed_and_persists(self):
        import app
        with patch.object(app, 'DB', self.directory() / 'board.db'):
            app.initialize()
            with app.connect() as db:
                self.assertEqual(db.execute('SELECT id FROM accounts').fetchall()[0][0], 'synth-account-1')
                db.execute("UPDATE accounts SET note='synthetic persistence'")
            app.initialize()
            with app.connect() as db:
                self.assertEqual(db.execute('SELECT note FROM accounts').fetchone()[0], 'synthetic persistence')

    def test_runtime_override_cannot_escape(self):
        import app
        with patch.object(app, 'DB', isolation.PROJECT / 'data' / 'never-open.db'):
            with self.assertRaises(ValueError):
                with app.connect():
                    self.fail('Opened protected database')

    def test_missing_runtime_configuration_fails_closed(self):
        import app
        import desktop_server
        with patch.object(app, 'DB', None):
            with self.assertRaises(ValueError):
                app.initialize()
            with self.assertRaises(ValueError):
                desktop_server.main()
            with self.assertRaises(ValueError):
                with app.connect():
                    self.fail('Unexpected fallback')

    def test_website_requires_explicit_configuration(self):
        import contextlib
        import io
        import web_server
        with patch('sys.argv', ['web_server.py']), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                web_server.main()
            self.assertEqual(error.exception.code, 2)

    def test_non_test_initialization_cannot_create_database(self):
        import app
        target = self.directory() / 'must-not-create.db'
        with patch.dict(os.environ, {'DOUYIN_BOARD_TEST_ROOT': ''}), patch.object(app, 'DB', target):
            with self.assertRaises(ValueError):
                app.initialize()
        self.assertFalse(target.exists())
