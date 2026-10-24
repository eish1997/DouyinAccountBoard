"""Populate confirmed account types and phone-based links from the imported snapshot."""
import csv
import io
import json
import re
import sqlite3
from collections import defaultdict, Counter
from datetime import date
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
import account_lifecycle


def populate(db):
    account_lifecycle.migrate(db)
    marker = 'tencent-lifecycle-20261014'
    if db.execute('SELECT 1 FROM imports WHERE name=?',(marker,)).fetchone():
        return {'already_imported': True}
    source = json.loads(Path('E:/Downloads/TencentDocsAccountImport/latest-accounts.json').read_text(encoding='utf-8'))
    rows = list(csv.reader(io.StringIO(source['csv_data'])))
    by_row = {}
    for a in db.execute('SELECT id,note FROM accounts'):
        match = re.search(r'^腾讯文档：最新账号情况表1，第(\d+)行',a['note'])
        if match:
            by_row[int(match[1])] = a['id']
    groups = defaultdict(list)
    imported = {}
    for row_number, r in enumerate(rows[1:],2):
        if row_number not in by_row:
            continue
        aid = by_row[row_number]
        role, creation, original = r[9].strip(), r[8].strip(), r[19].strip()
        kind = '小号' if role=='已注册（小号）' else ('大号' if role=='已注册（大号）' else '待确认')
        aux = '待确认'
        if kind=='大号' or kind=='待确认':
            if creation=='已注册' or role=='已注册（大号）':
                aux='已注册'
                if kind=='待确认': kind='大号'
            elif any(x in creation+role for x in ['无法创建','暂时无法创建','无法注册','环境异常','电话封停','卡收不了验证码','大号封禁','未满30','未注册']):
                kind='大号'
                aux='未注册' if any(x in creation+role for x in ['未满30','未注册']) else '暂无法注册'
        registered=''
        if original:
            try:
                registered=date.fromisoformat(original.replace('/','-')).isoformat()
            except ValueError:
                registered=date(*(int(v) for v in original.split('/'))).isoformat()
        notes = '原小号登记：' + (creation or '空') + ' / ' + (role or '空')
        phone = re.sub(r'\s+','',r[2])
        phone = phone if re.fullmatch(r'1\d{10}',phone) else ''
        db.execute('UPDATE accounts SET account_type=?,registered_on=?,auxiliary_state=?,lifecycle_note=?,registration_phone=? WHERE id=?',
                   (kind,registered,aux,notes,phone,aid))
        imported[aid]=kind
        if phone:
            groups[phone].append(aid)
    linked=0
    for group in groups.values():
        children=[aid for aid in group if imported[aid]=='小号']
        parents=[aid for aid in group if imported[aid]=='大号']
        if len(group)==2 and len(children)==1 and len(parents)==1:
            db.execute('UPDATE accounts SET parent_account_id=? WHERE id=?',(parents[0],children[0]))
            db.execute("UPDATE accounts SET auxiliary_state='已注册' WHERE id=?",(parents[0],))
            linked+=1
    db.execute("UPDATE accounts SET lifecycle_note=lifecycle_note || '；大小号关系待确认' WHERE account_type='小号' AND parent_account_id IS NULL")
    db.execute('INSERT INTO imports VALUES(?)',(marker,))
    return dict(types=dict(Counter(imported.values())),paired=linked,
                dates=db.execute("SELECT count(*) FROM accounts WHERE registered_on<>''").fetchone()[0])


if __name__=='__main__':
    import app
    app.initialize()
    with app.connect() as db:
        print(json.dumps(populate(db),ensure_ascii=False))
