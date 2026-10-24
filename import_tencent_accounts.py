"""Import the verified Tencent Docs account snapshot, preserving local history."""
import csv
import io
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = Path('E:/Downloads/TencentDocsAccountImport')


def rows(name):
    data = json.loads((SOURCE / (name + '.json')).read_text(encoding='utf-8'))
    if data.get('error'):
        raise ValueError(data['error'])
    return list(csv.reader(io.StringIO(data['csv_data'])))


def clean_name(value):
    return re.sub(r'\s+', '', value.strip())


def status(value):
    return {'正常': '正常', '账号永久封禁': '封禁', '账号短期封禁': '短期封禁',
            '养号7天': '养号7天', '未使用': '停用'}.get(value.strip(), '待确认')


def import_snapshot(db_path, backup=True):
    accounts = rows('latest-accounts')[1:]
    matrix = rows('computer-matrix')[1:]
    db = sqlite3.connect(db_path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_path = ROOT / 'backups' / ('before-tencent-import-' + stamp + '.db')
    if backup:
        backup_path.parent.mkdir(exist_ok=True)
        with sqlite3.connect(backup_path) as copy:
            db.backup(copy)
    existing = [dict(r) for r in db.execute('SELECT * FROM accounts')]
    by_name = defaultdict(list)
    by_handle = defaultdict(list)
    by_source = {}
    for a in existing:
        origin = re.search(r'^腾讯文档：最新账号情况表1，第(\d+)行', a['note'])
        if origin:
            by_source[int(origin[1])] = a
        by_name[clean_name(a['name'])].append(a)
        if a['douyin_id'] and '演示采集' not in a['note']:
            by_handle[a['douyin_id']].append(a)
    valid = [r for r in accounts if r[5].strip() or r[7].strip()]
    name_counts = Counter(clean_name(r[5]) for r in valid)
    next_id = max((int(a['id']) for a in existing if a['id'].isdigit()), default=0) + 1
    plans = []
    matched = set()
    handles = set()
    warnings = []
    for row_number, row in enumerate(accounts, 2):
        name, raw_handle, computer = row[5].strip(), row[7].strip(), row[12].strip()
        if not name and not raw_handle:
            continue
        handle = raw_handle if re.fullmatch(r'[A-Za-z0-9_.-]{2,100}', raw_handle) else ''
        if raw_handle and not handle:
            warnings.append({'row': row_number, 'issue': '抖音号列不是有效账号标识，保留在备注'})
        if handle and handle in handles:
            raise ValueError('Duplicate real Douyin handle in source')
        if handle:
            handles.add(handle)
        if computer and not computer.isdigit():
            warnings.append({'row': row_number, 'issue': '电脑编号不明确，列入未分配'})
            computer = ''
        candidates = [by_source[row_number]] if row_number in by_source else (by_handle.get(handle, []) if handle else [])
        if not candidates and name_counts[clean_name(name)] == 1:
            candidates = by_name.get(clean_name(name), [])
        old = candidates[0] if len(candidates) == 1 and candidates[0]['id'] not in matched else None
        if old:
            account_id = old['id']; matched.add(account_id)
        else:
            account_id = str(next_id); next_id += 1
        note = '\n'.join(filter(None, [
            '腾讯文档：最新账号情况表1，第' + str(row_number) + '行',
            '手机内部编号：' + row[6].strip() if row[6].strip() else '',
            '原账号状态：' + row[16].strip(),
            '使用情况：' + row[18].strip() if row[18].strip() else '',
            '负责人：' + row[11].strip() if row[11].strip() else '',
            '类型：' + row[13].strip() if row[13].strip() else '',
            '违规/处理备注：' + row[17].strip() if row[17].strip() else '',
            '原抖音号列：' + raw_handle if raw_handle and not handle else '',
            '原本地备注：' + old['note'] if old and old['note'] and not old['note'].startswith('腾讯文档：') else '',
        ]))
        plans.append(dict(id=account_id, name=name or '名称待补充', douyin_id=handle,
                          computer_id=computer or 'unassigned', status=status(row[16]),
                          note=note[:2000], slot=None, old=old))
    devices = {}
    order = {}
    for row in matrix:
        m = re.fullmatch(r'(\d+)号(?:电脑|设备)', row[0].strip())
        if not m:
            continue
        cid = m[1]
        devices[cid] = dict(id=cid, name=row[0].strip(), shadow_account='', ip=row[3].strip(),
                            operator=row[5].strip() or row[4].strip(), category=row[6].strip())
        names = []
        for value in row[7:16]:
            name = re.sub(r'^\.?\s*\d+(?:多开|小号)?\s*[-—－]\s*', '', value.strip())
            name = re.split(r'[（(]', name)[0]
            names.append(clean_name(name))
        order[cid] = names
    for p in plans:
        cid = p['computer_id']
        devices.setdefault(cid, dict(id=cid, name='未分配电脑' if cid == 'unassigned' else cid + '号电脑',
                                     shadow_account='', ip='', operator='', category=''))
    occupied = defaultdict(set)
    for a in existing:
        if a['id'] not in matched and a['slot'] is not None:
            occupied[a['computer_id']].add(a['slot'])
    plans.sort(key=lambda a: (a['computer_id'], order.get(a['computer_id'], []).index(clean_name(a['name']))
                             if clean_name(a['name']) in order.get(a['computer_id'], []) else 1000, int(a['id'])))
    for p in plans:
        cid = p['computer_id']
        if cid == 'unassigned':
            continue
        slot = next(i for i in range(1, 101) if i not in occupied[cid])
        p['slot'] = slot; occupied[cid].add(slot)
    daily_before = [tuple(r) for r in db.execute('SELECT * FROM daily ORDER BY account_id,day')]
    with db:
        for d in devices.values():
            db.execute('INSERT INTO computers VALUES(:id,:name,:shadow_account,:ip,:operator,:category) ON CONFLICT(id) DO UPDATE SET name=excluded.name,ip=CASE WHEN excluded.ip<>\'\' THEN excluded.ip ELSE computers.ip END,operator=excluded.operator,category=excluded.category', d)
        for aid in matched:
            db.execute('UPDATE accounts SET slot=NULL WHERE id=?', (aid,))
        for p in plans:
            old = p['old']
            if old:
                if old['douyin_id'] != p['douyin_id']:
                    db.execute('DELETE FROM collection_jobs WHERE account_id=?', (p['id'],))
                    db.execute("UPDATE accounts SET homepage='',work_count=NULL,last_collected='',collection_state='',collection_error='',last_attempted='' WHERE id=?", (p['id'],))
                db.execute('UPDATE accounts SET name=:name,status=:status,note=:note,computer_id=:computer_id,slot=:slot,douyin_id=:douyin_id WHERE id=:id', p)
            else:
                db.execute("INSERT INTO accounts(id,computer_id,slot,name,status,note,douyin_id,homepage,work_count,last_collected) VALUES(:id,:computer_id,:slot,:name,:status,:note,:douyin_id,'',NULL,'')", p)
        for old in existing:
            if old['id'] not in matched:
                note = old['note']
                marker = '原图片导入账号：未与最新表唯一匹配，保留待核对'
                if marker not in note:
                    db.execute('UPDATE accounts SET note=? WHERE id=?', ((note + '\n' + marker).strip(), old['id']))
        # Importing identity data does not start a new collection run.
        db.execute('UPDATE collection_settings SET auto_enabled=0 WHERE id=1')
        assert daily_before == [tuple(r) for r in db.execute('SELECT * FROM daily ORDER BY account_id,day')]
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    report = dict(source_accounts=len(plans), updated=len(matched), added=len(plans)-len(matched),
                  unassigned=sum(p['computer_id']=='unassigned' for p in plans),
                  real_handles=sum(bool(p['douyin_id']) for p in plans),
                  preserved_unmatched=len(existing)-len(matched),
                  total_accounts=db.execute('SELECT count(*) FROM accounts').fetchone()[0],
                  real_computers=db.execute("SELECT count(*) FROM computers WHERE id<>'unassigned'").fetchone()[0],
                  daily_preserved=True, automatic_collection=False, warnings=warnings,
                  backup=str(backup_path) if backup else None)
    db.close()
    return report


if __name__ == '__main__':
    import sys
    target = Path(sys.argv[1]) if len(sys.argv)>1 else ROOT / 'data/board.db'
    report = import_snapshot(target, backup=len(sys.argv)==1)
    (SOURCE / 'import-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
