"""Local phone-card inventory; account relationships follow registration phones."""
from datetime import date

STATES = ['待确认', '正常', '欠费', '停机', '停用', '待注销', '已注销']
CARRIERS = ['待确认', '中国移动', '中国联通', '中国电信', '中国广电', '其他']
OWNERSHIPS = ['待确认', '自有', '借用', '代管']
CODE_STATES = ['无待办', '待接码', '已完成', '暂无法接码']
RETURN_STATES = ['待确认', '不需归还', '待归还', '已归还']
STABILITY_STATES = ['待确认', '未稳定', '已稳定']
PASSWORD_STATES = ['待确认', '未修改', '已修改']
EXTRA_TEXT = {'keeper':100, 'code_contact':200, 'code_note':1000}
EXTRA_ENUM = {'ownership':OWNERSHIPS, 'code_state':CODE_STATES, 'return_state':RETURN_STATES}


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS phone_cards(
        id INTEGER PRIMARY KEY, phone TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL DEFAULT '', carrier TEXT NOT NULL DEFAULT '待确认',
        status TEXT NOT NULL DEFAULT '待确认', owner TEXT NOT NULL DEFAULT '',
        location TEXT NOT NULL DEFAULT '', expires_on TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '')''')
    columns={r[1] for r in db.execute('PRAGMA table_info(phone_cards)')}
    for field, default in {'keeper':'','code_contact':'','code_note':'','ownership':'待确认','code_state':'无待办','return_state':'待确认','return_due':'','returned_on':'','source_note':''}.items():
        if field not in columns:
            db.execute("ALTER TABLE phone_cards ADD COLUMN " + field + " TEXT NOT NULL DEFAULT '" + default + "'")
    columns={r[1] for r in db.execute('PRAGMA table_info(accounts)')}
    for field in ['card_stability','password_state']:
        if field not in columns:
            db.execute("ALTER TABLE accounts ADD COLUMN " + field + " TEXT NOT NULL DEFAULT '待确认'")


def valid_phone(phone):
    return len(phone) == 11 and phone.isascii() and phone.isdigit() and phone.startswith('1')


def overview(db, allowed_ids=None, accounts=None):
    cards = [dict(row) for row in db.execute('SELECT * FROM phone_cards ORDER BY id DESC')]
    if allowed_ids is not None:cards=[row for row in cards if row['id'] in allowed_ids]
    known = {card['phone'] for card in cards}
    phones = {row['registration_phone'] for row in accounts if row['registration_phone']} if accounts is not None else {row[0] for row in db.execute("SELECT DISTINCT registration_phone FROM accounts WHERE registration_phone<>''")}
    return dict(cards=cards, statuses=STATES, carriers=CARRIERS, ownerships=OWNERSHIPS,code_states=CODE_STATES,return_states=RETURN_STATES,
                importable=sum(valid_phone(phone) and phone not in known for phone in phones))


def save(db, body, actor=None):
    scoped=getattr(db,'scope_enabled',False)
    if scoped:
        import office_scope
        office_scope.authorize_route(db,actor,'/api/phone-card',body)
    phone = str(body.get('phone', '')).strip()
    if not valid_phone(phone):
        raise ValueError('请填写11位手机号码')
    card_id = body.get('id')
    old=None
    if card_id is not None:
        if type(card_id) is not int or not db.execute('SELECT 1 FROM phone_cards WHERE id=?', (card_id,)).fetchone():
            raise ValueError('手机卡记录不存在，请刷新后重试')
        old=db.execute('SELECT * FROM phone_cards WHERE id=?',(card_id,)).fetchone()
    duplicate = db.execute('SELECT 1 FROM phone_cards WHERE phone=?' + (' AND id<>?' if card_id is not None else ''),
                           (phone, card_id) if card_id is not None else (phone,)).fetchone()
    if duplicate:
        raise ValueError('这个手机号已经录入，请编辑已有手机卡')
    values = {}
    for key, limit in [('label', 100), ('owner', 100), ('location', 200), ('note', 2000)]:
        value = str(body.get(key, '')).strip()
        if len(value) > limit:
            raise ValueError('手机卡字段内容过长，请缩短后保存')
        values[key] = value
    carrier = body.get('carrier', '待确认')
    status = body.get('status', '待确认')
    if carrier not in CARRIERS or status not in STATES:
        raise ValueError('运营商或手机卡状态不正确')
    expires = str(body.get('expires_on', '')).strip()
    if expires:
        try:
            date.fromisoformat(expires)
        except ValueError:
            raise ValueError('到期日期不正确') from None
    extras={}
    for field,limit in EXTRA_TEXT.items():
        value=str(body.get(field,old[field] if old else '')).strip()
        if len(value)>limit:raise ValueError('接码或负责人信息过长，请缩短后保存')
        extras[field]=value
    for field,options in EXTRA_ENUM.items():
        value=body.get(field,old[field] if old else options[0])
        if value not in options:raise ValueError('归属、接码或归还状态不正确')
        extras[field]=value
    for field in ['return_due','returned_on']:
        value=str(body.get(field,old[field] if old else '')).strip()
        if value:date.fromisoformat(value)
        extras[field]=value
    if extras['return_state']=='已归还' and not extras['returned_on']:
        raise ValueError('标记已归还时请填写实际归还日期')
    params = (phone, values['label'], carrier, status, values['owner'], values['location'], expires, values['note'])
    if card_id is None:
        card_id=db.execute('INSERT INTO phone_cards(phone,label,carrier,status,owner,location,expires_on,note) VALUES(?,?,?,?,?,?,?,?) RETURNING id', params).fetchone()[0]
    else:
        db.execute('UPDATE phone_cards SET phone=?,label=?,carrier=?,status=?,owner=?,location=?,expires_on=?,note=? WHERE id=?', (*params, card_id))
    db.execute('UPDATE phone_cards SET '+','.join(field+'=?' for field in extras)+' WHERE id=?',(*extras.values(),card_id))
    if scoped:office_scope.save_assignment(db,actor,'cards',card_id,body,body.get('id') is None)
    return card_id


def update_readiness(db, account_id, body):
    old=db.execute('SELECT card_stability,password_state FROM accounts WHERE id=?',(str(account_id),)).fetchone()
    if not old:raise ValueError('账号不存在')
    stable=body.get('card_stability',old['card_stability'])
    password=body.get('password_state',old['password_state'])
    if stable not in STABILITY_STATES or password not in PASSWORD_STATES:
        raise ValueError('账号稳定或改密状态不正确')
    db.execute('UPDATE accounts SET card_stability=?,password_state=? WHERE id=?',(stable,password,str(account_id)))


def import_accounts(db,actor=None,body=None):
    phones = [row[0] for row in db.execute("SELECT DISTINCT registration_phone FROM accounts WHERE registration_phone<>'' ORDER BY registration_phone")]
    scoped=getattr(db,'scope_enabled',False)
    if scoped:
        import office_scope
        wanted=office_scope.requested_offices(db,actor,'cards',body or {})
        allowed=office_scope.visible_ids(db,actor,'accounts')
        phones=sorted({row['registration_phone'] for row in db.execute('SELECT id,registration_phone FROM accounts') if row['id'] in allowed and row['registration_phone']})
    added = 0
    for phone in phones:
        if valid_phone(phone):
            row=db.execute('INSERT INTO phone_cards(phone) VALUES(?) ON CONFLICT(phone) DO NOTHING RETURNING id', (phone,)).fetchone()
            if row:
                added+=1
                if scoped:office_scope.assign_entity(db,actor,'cards',row[0],wanted,newly_created=True)
    return added
