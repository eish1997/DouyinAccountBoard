"""Reviewed SQL expressions. Arguments must be source-code column expressions."""


class AdmissionRejected(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def transient_errors():
    from sqlite3 import OperationalError as SQLiteOperationalError
    try:
        from psycopg import OperationalError as PostgreSQLOperationalError
    except ImportError:
        return (SQLiteOperationalError,)
    return (SQLiteOperationalError, PostgreSQLOperationalError)


def begin_read(db):
    db.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY' if getattr(db, 'dialect', 'sqlite') == 'postgresql' else 'BEGIN')


def integrity_errors(db=None):
    if db is None:
        from sqlite3 import IntegrityError as SQLiteIntegrityError
        try:
            from psycopg import IntegrityError as PostgreSQLIntegrityError
        except ImportError:
            return (SQLiteIntegrityError,)
        return (SQLiteIntegrityError, PostgreSQLIntegrityError)
    if getattr(db, 'dialect', 'sqlite') == 'postgresql':
        from psycopg import IntegrityError
    else:
        from sqlite3 import IntegrityError
    return (IntegrityError,)


def begin_write(db):
    if getattr(db, 'dialect', 'sqlite') == 'postgresql':
        db.begin_write()
    else:
        db.execute('BEGIN IMMEDIATE')


def integer_order(db, expression):
    if getattr(db, 'dialect', 'sqlite') != 'postgresql':
        return 'CAST(' + expression + ' AS INTEGER)'
    # Preserve SQLite's integer-prefix ordering for legacy text identifiers,
    # including nonnumeric identifiers (0), NULL and signed 64-bit saturation.
    numeric = "COALESCE(substring((" + expression + ") from '^[[:space:]]*([+-]?[0-9]+)')::numeric,0)"
    return '(CASE WHEN (' + expression + ') IS NULL THEN NULL ELSE GREATEST(-9223372036854775808::numeric,LEAST(9223372036854775807::numeric,' + numeric + ')) END) NULLS FIRST'


def history_account(db, column):
    if column not in ('before_json', 'after_json'):
        raise ValueError('Unreviewed history column')
    if getattr(db, 'dialect', 'sqlite') == 'postgresql':
        return '(' + column + "::jsonb ->> 'account_id')"
    return 'json_extract(' + column + ",'$.account_id')"
