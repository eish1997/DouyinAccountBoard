"""Account relationships and the user's 30-day registration tracking rule."""
from datetime import date, timedelta

TYPES = ['待确认', '大号', '小号']
AUX_STATES = ['待确认', '未注册', '已注册', '暂无法注册']


def migrate(db):
    columns = {r[1] for r in db.execute('PRAGMA table_info(accounts)')}
    for name, definition in {
        'account_type': "TEXT NOT NULL DEFAULT '待确认'",
        'parent_account_id': "TEXT REFERENCES accounts(id)",
        'registered_on': "TEXT NOT NULL DEFAULT ''",
        'auxiliary_state': "TEXT NOT NULL DEFAULT '待确认'",
        'lifecycle_note': "TEXT NOT NULL DEFAULT ''",
        'registration_phone': "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in columns:
            db.execute('ALTER TABLE accounts ADD COLUMN ' + name + ' ' + definition)


def enrich(accounts, today):
    by_id = {a['id']: a for a in accounts}
    children = {}
    for a in accounts:
        if a['parent_account_id']:
            children.setdefault(a['parent_account_id'], []).append(a)
    for a in accounts:
        own_children = children.get(a['id'], [])
        a['child_accounts'] = [{'id': c['id'], 'name': c['name']} for c in own_children]
        parent = by_id.get(a['parent_account_id'])
        a['parent_name'] = parent['name'] if parent else ''
        a['effective_auxiliary_state'] = '已注册' if own_children else a['auxiliary_state']
        a['mature_on'] = ''
        a['days_remaining'] = None
        if a['registered_on']:
            mature = date.fromisoformat(a['registered_on']) + timedelta(days=30)
            a['mature_on'] = mature.isoformat()
            a['days_remaining'] = max(0, (mature - date.fromisoformat(today)).days)
        if a['account_type'] == '小号':
            a['lifecycle'] = '小号'
        elif a['account_type'] != '大号':
            a['lifecycle'] = '账号类型待确认'
        elif a['effective_auxiliary_state'] == '已注册':
            a['lifecycle'] = '已注册辅助账号'
        elif a['effective_auxiliary_state'] == '暂无法注册':
            a['lifecycle'] = '暂无法注册'
        elif not a['registered_on']:
            a['lifecycle'] = '注册日期待补充'
        elif a['days_remaining'] > 0:
            a['lifecycle'] = '未满30天'
        elif a['effective_auxiliary_state'] == '未注册':
            a['lifecycle'] = '满30天未注册'
        else:
            a['lifecycle'] = '满30天注册情况待确认'


def validate(db, account_id, body, old, today, actor=None):
    kind = body.get('account_type', old['account_type'])
    aux = body.get('auxiliary_state', old['auxiliary_state'])
    registered = str(body.get('registered_on', old['registered_on'])).strip()
    parent = str(body.get('parent_account_id', old['parent_account_id']) or '').strip() or None
    note = str(body.get('lifecycle_note', old['lifecycle_note']))[:1000]
    phone = str(body.get('registration_phone', old['registration_phone'])).strip()
    if phone and (len(phone)!=11 or not phone.isdigit() or not phone.startswith('1')):
        raise ValueError('注册手机号应为11位手机号，未知可留空')
    if kind not in TYPES or aux not in AUX_STATES:
        raise ValueError('账号类型或辅助账号注册状态不正确')
    if registered:
        if date.fromisoformat(registered) > date.fromisoformat(today):
            raise ValueError('注册日期不能晚于今天')
    if kind=='小号' and phone and not parent:
        candidates=db.execute("SELECT id FROM accounts WHERE account_type='大号' AND registration_phone=? AND id<>?",(phone,account_id)).fetchall()
        if len(candidates)==1:
            parent=candidates[0]['id']
        elif len(candidates)>1:
            raise ValueError('同一手机号对应多个大号，请手动确认关联大号')
    if parent:
        if getattr(db,'scope_enabled',False) and parent != old['parent_account_id']:
            # Matching a phone can discover a parent absent from the submitted
            # payload; authorize the resolved relationship before inspecting it.
            import office_scope
            office_scope.require_entity(db,actor,'accounts',parent,write=True)
        target = db.execute('SELECT account_type,registration_phone FROM accounts WHERE id=?', (parent,)).fetchone()
        if kind != '小号' or parent == account_id or not target or target['account_type'] != '大号':
            raise ValueError('小号只能关联另一个已标记为大号的账号')
        if phone and target['registration_phone'] and phone!=target['registration_phone']:
            raise ValueError('大小号的注册手机号不一致，请核对配对关系')
        if db.execute('SELECT 1 FROM accounts WHERE parent_account_id=? AND id<>?', (parent, account_id)).fetchone():
            raise ValueError('这个大号已经关联小号，请先核对原有关系')
    if kind != '小号':
        parent = None
    if db.execute('SELECT 1 FROM accounts WHERE parent_account_id=?', (account_id,)).fetchone():
        if kind != '大号' or aux in ['未注册', '暂无法注册']:
            raise ValueError('已有小号关联，不能将大号改为其他类型或标记未注册')
        if phone != old['registration_phone'] and db.execute(
            "SELECT 1 FROM accounts WHERE parent_account_id=? AND registration_phone<>'' AND registration_phone<>?",
            (account_id, phone),
        ).fetchone():
            raise ValueError('修改后会与已关联小号的注册手机号不一致，请先核对或解除配对关系')
    return kind, parent, registered, aux, note, phone
