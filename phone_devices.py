"""Physical phones, independent of SIM ownership and publishing computers."""
import re
from sql_dialect import integer_order

STATES = ['待确认', '在用', '待清理', '可用闲置', '故障']
INACTIVE = {'封禁', '停用'}


def number(value):
    match=re.fullmatch(r'(\d{1,6})(?:号手机|多开(?:抖音)?)?',str(value).strip(),flags=re.ASCII)
    return str(int(match[1])) if match and int(match[1])>0 else None


def device_identity(value):
    raw=str(value).strip()
    prefix='office3:' if raw.startswith('office3:') else ''
    digits=raw[len(prefix):]
    normalized=number(digits)
    if not normalized or not digits.isascii() or not digits.isdigit():
        raise ValueError('手机编号请填1至6位正整数')
    return prefix+normalized


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS phone_devices(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, state TEXT NOT NULL DEFAULT '待确认',
        state_source TEXT NOT NULL DEFAULT 'auto', owner TEXT NOT NULL DEFAULT '',
        location TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '')""")
    columns={r[1] for r in db.execute('PRAGMA table_info(accounts)')}
    for name, definition in {
        'phone_device_id':'TEXT REFERENCES phone_devices(id)',
        'phone_device_source':"TEXT NOT NULL DEFAULT ''",
        'phone_device_note':"TEXT NOT NULL DEFAULT ''",
        'office_group':"TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in columns:db.execute('ALTER TABLE accounts ADD COLUMN '+name+' '+definition)


def overview(db, accounts, allowed_ids=None):
    order=integer_order(db,"CASE WHEN id LIKE 'office3:%' THEN substr(id,9) ELSE id END")
    devices=[dict(r) for r in db.execute("SELECT * FROM phone_devices ORDER BY CASE WHEN id LIKE 'office3:%' THEN 1 ELSE 0 END,"+order+",id")]
    if allowed_ids is not None:devices=[row for row in devices if row['id'] in allowed_ids]
    by_id={d['id']:d for d in devices}
    for a in accounts:
        device=by_id.get(a['phone_device_id'])
        a['phone_device_name']=device['name'] if device else ''
    for d in devices:
        own=[a for a in accounts if a['phone_device_id']==d['id']]
        inactive=sum(a['status'] in INACTIVE for a in own)
        d['account_ids']=[a['id'] for a in own]
        d['account_count']=len(own)
        d['inactive_count']=inactive
        d['occupancy']='无当前账号' if not own else '仅封禁或停用' if inactive==len(own) else '有在用或待确认账号'
        d['suggested_state']='待确认' if not own else '待清理' if inactive==len(own) else '在用'
        if d['state_source']=='auto':d['state']=d['suggested_state']
        d['attention']='没有当前登录记录，需确认是否已清理' if not own and d['state']!='可用闲置' else '关联账号均封禁或停用，请核对清理' if own and inactive==len(own) else ''
    return dict(devices=devices,statuses=STATES,unassigned=sum(not a['phone_device_id'] for a in accounts),
                unresolved=sum('待核对' in a['phone_device_note'] and a['phone_device_source']!='manual' for a in accounts))


def save(db, body, create=False, actor=None):
    scoped=getattr(db,'scope_enabled',False)
    if scoped:
        import office_scope
        office_scope.authorize_route(db,actor,'/api/phone-device/create' if create else '/api/phone-device',body)
    raw=str(body.get('id','')).strip()
    device_id=device_identity(raw)
    old=db.execute('SELECT * FROM phone_devices WHERE id=?',(device_id,)).fetchone()
    if create and old:raise ValueError('这个手机编号已经存在，请编辑已有设备')
    if not create and not old:raise ValueError('手机设备不存在')
    state=body.get('state','待确认')
    state_source='auto' if state=='auto' else 'manual'
    if state=='auto':state='待确认'
    if state not in STATES:raise ValueError('手机设备状态不正确')
    if state=='可用闲置' and db.execute('SELECT 1 FROM accounts WHERE phone_device_id=?',(device_id,)).fetchone():
        raise ValueError('仍有当前账号关联，请先移出账号并确认清理完成，再标记可用闲置')
    values=[]
    for field,limit in [('name',100),('owner',100),('location',200),('note',2000)]:
        value=str(body.get(field,'')).strip()
        if len(value)>limit:raise ValueError('手机资料内容过长')
        values.append(value)
    name,owner,location,note=values
    name=name or ('办公室三 · '+device_id.split(':')[-1]+'号手机' if device_id.startswith('office3:') else device_id+'号手机')
    if create:
        db.execute('INSERT INTO phone_devices(id,name,state,state_source,owner,location,note) VALUES(?,?,?,?,?,?,?)',(device_id,name,state,state_source,owner,location,note))
    else:
        db.execute('UPDATE phone_devices SET name=?,state=?,state_source=?,owner=?,location=?,note=? WHERE id=?',(name,state,state_source,owner,location,note,device_id))
    if scoped:office_scope.save_assignment(db,actor,'devices',device_id,body,create)
    return device_id


def assign(db, account_id, value, source='manual'):
    account=db.execute('SELECT phone_device_id,status FROM accounts WHERE id=?',(str(account_id),)).fetchone()
    if not account:raise ValueError('账号不存在')
    device_id=str(value or '').strip() or None
    if device_id:
        target=db.execute('SELECT * FROM phone_devices WHERE id=?',(device_id,)).fetchone()
        if not target:raise ValueError('请选择已有手机设备，或先新增手机')
        if target['state']=='故障' and account['phone_device_id']!=device_id:
            raise ValueError('该手机标记为故障，请先核对设备状态')
    if account['phone_device_id']==device_id:return
    db.execute('UPDATE accounts SET phone_device_id=?,phone_device_source=? WHERE id=?',(device_id,source,str(account_id)))
    if device_id:
        db.execute('UPDATE accounts SET office_group=? WHERE id=?',('three' if device_id.startswith('office3:') else '',str(account_id)))
        # A newly assigned phone must no longer appear as available idle.
        db.execute("UPDATE phone_devices SET state_source='auto' WHERE id=? AND state<>'故障'",(device_id,))
    if account['phone_device_id']:
        db.execute("UPDATE phone_devices SET state='待确认',state_source='auto' WHERE id=? AND state='在用' AND NOT EXISTS(SELECT 1 FROM accounts WHERE phone_device_id=?)",(account['phone_device_id'],account['phone_device_id']))
