"""Local administrator recovery utility; never exposed as a web endpoint."""
import ctypes
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
import auth


def reset_password(db_path,password):
    encoded=auth.password_hash(password)
    with closing(sqlite3.connect(db_path,timeout=15)) as db:
        db.row_factory=sqlite3.Row
        owners=db.execute("SELECT * FROM auth_users WHERE role='owner'").fetchall()
        if len(owners)!=1:raise ValueError('总管理员记录异常，已停止重置')
        owner=owners[0]
        backup_path=Path(db_path).parent.parent/'backups'/('before-owner-reset-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.db')
        backup_path.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(backup_path)) as copy:db.backup(copy)
        with db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM auth_users WHERE role='owner' AND id=?",(owner['id'],)).fetchone()
            if not row:raise ValueError('总管理员记录已改变，已停止重置')
            db.execute('UPDATE auth_users SET password_hash=? WHERE id=?',(encoded,row['id']))
            db.execute('DELETE FROM auth_sessions WHERE user_id=?',(row['id'],))
            auth.audit(db,dict(row),'本机管理员重置总账号密码',row['username'])
        return dict(username=owner['username'],backup=str(backup_path))


def main():
    if not ctypes.windll.shell32.IsUserAnAdmin():raise ValueError('请在服务器本机以管理员身份运行')
    db_path=ROOT/'data/board.db'
    if not db_path.is_file():raise ValueError('正式数据库不存在')
    if sys.argv[1:]==['--info']:
        with closing(sqlite3.connect(db_path)) as db:
            owners=db.execute("SELECT username FROM auth_users WHERE role='owner'").fetchall()
            if len(owners)!=1:raise ValueError('总管理员记录异常')
            result=dict(username=owners[0][0])
    elif sys.argv[1:]==['--apply']:
        result=reset_password(db_path,json.load(sys.stdin)['password'])
    else:raise ValueError('无效参数')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps(dict(error=str(error)),ensure_ascii=False));sys.exit(1)
