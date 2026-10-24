"""Static PostgreSQL migration inventory; never opens any database or configuration."""
import ast
import json
import re
from pathlib import Path
from server_package import PYTHON_FILES

PATTERNS = {
    'sqlite_api': r'\bsqlite3\b',
    'pragma': r'\bPRAGMA\b',
    'sqlite_write_lock': r'BEGIN IMMEDIATE',
    'insert_or_ignore': r'INSERT OR IGNORE',
    'upsert': r'ON CONFLICT',
    'rowid_or_lastrowid': r'\b(?:rowid|lastrowid)\b',
    'max_id_generation': r'MAX\(CAST\(id',
    'integer_cast': r'CAST\(.+? AS INTEGER\)',
    'sqlite_backup': r'\.backup\(',
    'implicit_migrations': r'\.executescript\(',
    'sqlite_error_types': r'sqlite3\.(?:IntegrityError|OperationalError|Row)',
}


def inventory():
    result = {}
    for name in PYTHON_FILES:
        source = Path(__file__).with_name(name).read_text(encoding='utf-8')
        ast.parse(source)
        found = {}
        for kind, pattern in PATTERNS.items():
            lines = [number for number, text in enumerate(source.splitlines(), 1)
                     if re.search(pattern, text, flags=re.IGNORECASE)]
            if lines:
                found[kind] = lines
        if found:
            result[name] = found
    return {'kind': 'static_inventory_not_adapter_validation', 'files': result,
            'required_semantic_checks': ['transaction boundaries', 'foreign keys', 'NULL versus empty',
                'sorting and text equality', 'time semantics', 'sequence start', 'BLOB to bytea',
                'authorization and maintenance locks', 'audit ownership', 'schema privilege isolation']}


if __name__ == '__main__':
    print(json.dumps(inventory(), ensure_ascii=False, indent=2))
