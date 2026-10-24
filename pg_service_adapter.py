"""Explicit qmark binding and row interface; no SQL dialect rewriting.

Unsupported SQLite statements must be ported in their service, never guessed here.
Transactions and authorization belong to the caller's reviewed write protocol.
"""
from collections.abc import Mapping
import re


def bind_qmarks(query, parameters):
    if not isinstance(parameters, (tuple, list)):
        raise TypeError('Positional tuple/list parameters required')
    output = []
    index = count = 0
    while index < len(query):
        character = query[index]
        start = index
        if character in "'\"":
            quote = character
            index += 1
            while index < len(query):
                if query[index] == quote:
                    index += 1
                    if index < len(query) and query[index] == quote:
                        index += 1
                        continue
                    break
                if query[index] == '\\':
                    raise ValueError('Use bound parameters instead of SQL backslash literals')
                index += 1
            else:
                raise ValueError('Unterminated quoted SQL')
        elif query.startswith('--', index):
            end = query.find('\n', index)
            index = len(query) if end < 0 else end + 1
        elif query.startswith('/*', index):
            depth = 1
            index += 2
            while index < len(query) and depth:
                if query.startswith('/*', index):
                    depth += 1
                    index += 2
                elif query.startswith('*/', index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise ValueError('Unterminated SQL comment')
        elif character == '$' and (match := re.match(r'\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$', query[index:])):
            tag = match.group()
            end = query.find(tag, index + len(tag))
            if end < 0:
                raise ValueError('Unterminated dollar quote')
            index = end + len(tag)
        elif character == '?':
            output.append('%s')
            count += 1
            index += 1
            continue
        else:
            index += 1
        output.append(query[start:index].replace('%', '%%'))
    if count != len(parameters):
        raise ValueError('SQL placeholder count mismatch')
    return ''.join(output), parameters


class ServiceRow(Mapping):
    def __init__(self, names, values):
        self.names, self.values = names, values
        self.positions = {name: index for index, name in enumerate(names)}

    def __getitem__(self, key):
        return self.values[key if isinstance(key, (int, slice)) else self.positions[key]]

    def __iter__(self):
        return iter(self.names)

    def __len__(self):
        return len(self.names)


def service_row_factory(cursor):
    names = tuple(column.name for column in cursor.description) if cursor.description else ()
    return lambda values: ServiceRow(names, values)


class ServiceConnection:
    dialect = 'postgresql'

    def __init__(self, connection, *, writer_epoch=None, restore_generation=None, schema=None, scope_enabled=False):
        self.connection = connection
        self.writer_epoch, self.restore_generation, self.schema = writer_epoch, restore_generation, schema
        self.write_admitted = False
        self.scope_enabled = scope_enabled

    def begin_write(self):
        from psycopg.pq import TransactionStatus
        from pg_write_protocol import acquire_control
        if self.writer_epoch is None or self.restore_generation is None or self.schema is None:
            raise ValueError('Explicit writer configuration required')
        if self.connection.info.transaction_status != TransactionStatus.IDLE:
            raise ValueError('Write transaction already active')
        self.connection.execute('BEGIN')
        try:
            acquire_control(self.connection, writer_epoch=self.writer_epoch,
                            restore_generation=self.restore_generation, schema=self.schema)
            self.write_admitted = True
        except BaseException:
            self.connection.rollback()
            raise

    def commit(self):
        self.connection.commit()
        self.write_admitted = False

    def rollback(self):
        self.connection.rollback()
        self.write_admitted = False

    def executemany(self, query, rows):
        rows = list(rows)
        if not rows:
            return
        query, _ = bind_qmarks(query, rows[0])
        cursor = self.connection.cursor(row_factory=service_row_factory)
        cursor.executemany(query, rows)
        return cursor

    def execute(self, query, parameters=None):
        cursor = self.connection.cursor(row_factory=service_row_factory)
        if parameters is None:
            return cursor.execute(query)
        query, parameters = bind_qmarks(query, parameters)
        return cursor.execute(query, parameters)
