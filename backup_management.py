"""Owner-only business restore: users, passwords and authorization never roll back."""
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from datetime import datetime
import backup
import operations


def location(db_path,ident):
    root=Path(db_path).resolve().parent.parent/'backups';path=(root/str(ident)).resolve()
    if path.parent not in [root,root/'automatic'] or path.suffix!='.db' or not path.is_file():raise ValueError('请选择备份列表中的文件')
    if not path.name.startswith(('before-','auto-','manual-','backup-')):raise ValueError('测试副本或未知文件不能恢复')
    if any(word in path.name.lower() for word in ['validation','preview','test']):raise ValueError('测试副本禁止恢复')
    return path


def listing(db_path):
    root=Path(db_path).resolve().parent.parent/'backups';result=[]
    for path in list(root.glob('*.db'))+list((root/'automatic').glob('*.db')):
        ident=path.relative_to(root).as_posix()
        try:location(db_path,ident)
        except ValueError:continue
        result.append(dict(id=ident,name=path.name,bytes=path.stat().st_size,at=int(path.stat().st_mtime)))
    return sorted(result,key=lambda row:row['at'],reverse=True)


def business_tables(db):
    return [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'auth_%' AND name NOT LIKE 'operations_%'")]


def fingerprint(db):return operations.token({table:[[hashlib.sha256(v).hexdigest() if isinstance(v,bytes) else v for v in row] for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')] for table in business_tables(db)})


def inspect(db,db_path,ident):
    path=location(db_path,ident)
    with closing(sqlite3.connect(path)) as source:
        if source.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('备份完整性检查失败')
        tables={row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'accounts','phone_cards','phone_devices','publishing_layout'}.issubset(tables):raise ValueError('备份缺少当前业务表，不能直接恢复')
        counts={name:dict(current=db.execute('SELECT count(*) FROM '+name).fetchone()[0],backup=source.execute('SELECT count(*) FROM '+name).fetchone()[0] if name in tables else 0) for name in business_tables(db)}
    return dict(backup_id=ident,file_hash=hashlib.sha256(path.read_bytes()).hexdigest(),state_hash=fingerprint(db),counts=counts)


def restore(db,db_path,payload):
    path=location(db_path,payload['backup_id'])
    if hashlib.sha256(path.read_bytes()).hexdigest()!=payload['file_hash']:raise operations.ConflictError('备份文件已改变，请重新预览')
    if fingerprint(db)!=payload['state_hash']:raise operations.ConflictError('预览后业务资料发生变化，请重新预览，避免覆盖新记录')
    fresh=Path(db_path).resolve().parent.parent/'backups'/('before-restore-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.db')
    backup.snapshot(db_path,fresh)
    with closing(sqlite3.connect(path)) as source:
        if source.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('备份完整性检查失败')
        available={row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables=business_tables(db);db.execute('PRAGMA defer_foreign_keys=ON')
        for table in tables:db.execute('DELETE FROM '+table)
        for table in tables:
            if table not in available or table=='collection_jobs':continue
            columns=[row[1] for row in source.execute('PRAGMA table_info('+table+')')]
            current={row[1] for row in db.execute('PRAGMA table_info('+table+')')}
            if not set(columns).issubset(current):raise ValueError('备份结构较新，请使用匹配的软件版本')
            rows=source.execute('SELECT '+','.join(columns)+' FROM '+table).fetchall()
            db.executemany('INSERT INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',rows)
        db.execute("UPDATE collection_settings SET auto_enabled=0,paused=1,pause_reason='恢复后请核对资料，再手动继续采集'")
        if list(db.execute('PRAGMA foreign_key_check')):raise ValueError('恢复资料关系检查失败，原数据已保留')
    return dict(backup_before_restore=fresh.name,note='业务资料已恢复，登录账号和权限保持当前设置，采集已暂停等待核对')
