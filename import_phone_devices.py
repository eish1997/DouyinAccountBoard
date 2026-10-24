"""Read only the account name / login phone / Douyin ID columns of a snapshot."""
import csv
import io
import json
import re
from collections import Counter,defaultdict
from pathlib import Path
import phone_devices


def clean(value):return re.sub(r'\s+','',value)


def apply(db, snapshot, phone_snapshot=None):
    result=json.loads(Path(snapshot).read_text(encoding='utf-8'))
    if result.get('error'):raise ValueError('腾讯文档快照读取失败')
    rows=list(csv.reader(io.StringIO(result['csv_data'])))
    if not rows or rows[0]!=['抖音名称','抖音在哪个手机上','抖音号']:
        raise ValueError('登录手机来源列结构变化，请核对')
    phones=[]
    if phone_snapshot:
        extra=json.loads(Path(phone_snapshot).read_text(encoding='utf-8'))
        phones=list(csv.reader(io.StringIO(extra['csv_data'])))
        if extra.get('error') or len(phones)!=len(rows) or phones[0]!=['注册抖音用的手机号']:
            raise ValueError('注册手机号快照结构与账号快照不一致')
    phone_devices.migrate(db)
    existing=[dict(r) for r in db.execute('SELECT * FROM accounts')]
    by_handle=defaultdict(list);by_name=defaultdict(list);by_identity=defaultdict(list)
    for a in existing:
        if '演示采集' in a['note']:continue
        if a['douyin_id']:by_handle[a['douyin_id']].append(a)
        by_name[clean(a['name'])].append(a)
        if a['registration_phone']:by_identity[(clean(a['name']),a['registration_phone'])].append(a)
    names=Counter(clean(row[0]) for row in rows[1:] if row and row[0].strip())
    identities=Counter((clean(row[0]),phones[i][0].strip()) for i,row in enumerate(rows) if i and phones and phones[i] and row[0].strip())
    plans=defaultdict(list);device_sources=defaultdict(set);warnings=[]
    for row_number,row in enumerate(rows[1:],2):
        name,raw,handle=(row+['']*3)[:3]
        name,raw,handle=name.strip(),raw.strip(),handle.strip()
        device_id=phone_devices.number(raw)
        if device_id:device_sources[device_id].add('抖音当前登录位置列')
        if not name and not handle:continue
        valid_handle=bool(re.fullmatch(r'[A-Za-z0-9_.-]{2,100}',handle))
        candidates=by_handle.get(handle,[]) if valid_handle else by_name.get(clean(name),[]) if name and names[clean(name)]==1 else []
        if not valid_handle and len(candidates)!=1 and phones and phones[row_number-1]:
            identity=(clean(name),phones[row_number-1][0].strip())
            if re.fullmatch(r'1[0-9]{10}',identity[1]) and identities[identity]==1:
                candidates=by_identity.get(identity,[])
        if len(candidates)!=1:
            warnings.append(dict(row=row_number,name=name,douyin_id=handle,login_phone=raw,reason='账号未唯一匹配，未创建账号或猜测关联'))
            continue
        plans[candidates[0]['id']].append((row_number,raw,device_id))
    # Card positions establish an inventory candidate, never an account login relation.
    for card in db.execute('SELECT location FROM phone_cards'):
        raw=card[0].strip()
        if re.fullmatch(r'\d{1,6}',raw,flags=re.ASCII) and phone_devices.number(raw):
            device_sources[phone_devices.number(raw)].add('手机卡位置记录（不代表当前登录账号）')
    added=0
    for device_id,sources in device_sources.items():
        added+=db.execute('INSERT OR IGNORE INTO phone_devices(id,name,note) VALUES(?,?,?)',(device_id,device_id+'号手机','编号来源：'+'；'.join(sorted(sources)))).rowcount
    assigned=0;pending=[];manual_kept=0
    for account_id,entries in plans.items():
        old=db.execute('SELECT phone_device_id,phone_device_source,phone_device_note FROM accounts WHERE id=?',(account_id,)).fetchone()
        if old['phone_device_source']=='manual':manual_kept+=1;continue
        distinct={entry[2] for entry in entries}
        rows_text=','.join(str(entry[0]) for entry in entries)
        raws=' / '.join(dict.fromkeys(entry[1] or '空白' for entry in entries))
        device_id=next(iter(distinct)) if len(distinct)==1 else None
        if device_id and (not old['phone_device_id'] or old['phone_device_id']==device_id):
            note='腾讯文档 登录手机列：'+raws+'；源行：'+rows_text
            if not old['phone_device_id']:assigned+=1
            db.execute("UPDATE accounts SET phone_device_id=?,phone_device_source='tencent',phone_device_note=? WHERE id=?",(device_id,note,account_id))
        else:
            reason='登录手机位置待核对：'+raws+'；源行：'+rows_text
            pending.append(dict(account_id=account_id,rows=[entry[0] for entry in entries],login_phone=raws,reason='位置为空、标注不明确或存在多个位置/已有位置冲突'))
            if not old['phone_device_id']:
                db.execute("UPDATE accounts SET phone_device_source='tencent',phone_device_note=? WHERE id=?",(reason,account_id))
    accounts=[dict(r) for r in db.execute('SELECT * FROM accounts')]
    overview=phone_devices.overview(db,accounts)
    return dict(source_rows=len(rows)-1,devices_added=added,devices=len(overview['devices']),accounts_assigned=assigned,
                assigned_total=sum(bool(a['phone_device_id']) for a in accounts),manual_kept=manual_kept,
                states=dict(Counter(d['state'] for d in overview['devices'])),
                occupancy=dict(Counter(d['occupancy'] for d in overview['devices'])),pending=pending,unmatched=warnings)
