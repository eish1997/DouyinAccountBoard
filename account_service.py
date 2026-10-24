import collection
import account_lifecycle
import phone_cards
import phone_devices
import auth
import uuid
STATES=['正常','养号7天','短期封禁','封禁','发不了','停用','待确认']

PATCH_PERMISSIONS={**{key:'account_manage' for key in ('name','note','computer_id','slot','douyin_id','homepage','account_type','parent_account_id','registered_on','lifecycle_note','registration_phone','office_id')},
    'status':'account_status','auxiliary_state':'account_progress','card_stability':'account_progress','password_state':'account_progress','phone_device_id':'device_manage'}


def authorize_patch(db,actor,body):
    auth.require(actor)
    changes=body.get('changes')
    if not isinstance(changes,dict) or not changes:raise ValueError('请提交需要修改的字段')
    if set(changes)-PATCH_PERMISSIONS.keys():raise ValueError('包含不允许修改的字段')
    for field,value in changes.items():
        auth.require(actor,PATCH_PERMISSIONS[field])
        if field=='slot':
            if type(value) is not int:raise ValueError('账号位置必须为整数')
        elif value is None and field in ('parent_account_id','phone_device_id','office_id'):pass
        elif not isinstance(value,str):raise ValueError('字段格式不正确：'+field)
    if 'office_id' in changes and not getattr(db,'scope_enabled',False):raise ValueError('当前模式未启用办公室范围')
    return changes


def patch(db,body,actor,day):
    """Caller owns one admitted transaction, version check and audit boundary."""
    auth.authorize_route(db,actor,'/api/account/patch',body)
    changes=authorize_patch(db,actor,body);ident=str(body.get('id',''))
    old=db.execute('SELECT * FROM accounts WHERE id=?',(ident,)).fetchone()
    if not old:raise ValueError('账号不存在')
    values={}
    if 'name' in changes:
        name=changes['name'].strip()
        if not name or len(name)>100:raise ValueError('账号名称不能为空或超过100字')
        values['name']=name
    if 'note' in changes:
        if len(changes['note'])>2000:raise ValueError('备注不能超过2000字')
        values['note']=changes['note']
    if 'status' in changes:
        if changes['status'] not in STATES:raise ValueError('账号状态不正确')
        values['status']=changes['status']
        if changes['status']=='养号7天' and old['status']!='养号7天':values['nurture_started']=''
    if {'computer_id','slot'}&changes.keys():
        computer=changes.get('computer_id',old['computer_id']);slot=changes.get('slot',old['slot'] or 1)
        if not 1<=slot<=100 or not db.execute('SELECT 1 FROM computers WHERE id=?',(computer,)).fetchone():raise ValueError('电脑或账号位置不正确')
        values.update(computer_id=computer,slot=None if computer=='unassigned' else slot)
    if 'homepage' in changes:values['homepage']=collection.normalize_homepage(changes['homepage'])
    if 'douyin_id' in changes:
        identity=changes['douyin_id'].strip()
        if len(identity)>100 or any(c.isspace() for c in identity):raise ValueError('真实抖音号不能包含空格或超过100字')
        if identity and identity!=old['douyin_id'] and db.execute('SELECT 1 FROM accounts WHERE douyin_id=? AND id<>?',(identity,ident)).fetchone():raise ValueError('这个抖音号已经在工作台中，请编辑已有账号')
        values['douyin_id']=identity
        if identity!=old['douyin_id'] and values.get('homepage',old['homepage'])==old['homepage']:values['homepage']=''
    lifecycle=('account_type','parent_account_id','registered_on','auxiliary_state','lifecycle_note','registration_phone')
    if set(lifecycle)&changes.keys():
        resolved=account_lifecycle.validate(db,ident,changes,old,day,actor=actor)
        values.update({key:value for key,value in zip(lifecycle,resolved) if value!=old[key]})
    # A late readiness validation can still fail after the first SQL statement;
    # the HTTP transaction rolls all fields and side effects back together.
    if any(key in values and values[key]!=old[key] for key in ('douyin_id','homepage')):
        values.update(work_count=None,last_collected='',last_attempted='',collection_state='',collection_error='')
    if values:db.execute('UPDATE accounts SET '+','.join(key+'=?' for key in values)+' WHERE id=?',(*values.values(),ident))
    if {'card_stability','password_state'}&changes.keys():phone_cards.update_readiness(db,ident,changes)
    if 'phone_device_id' in changes and (changes['phone_device_id'] or None)!=(old['phone_device_id'] or None):phone_devices.assign(db,ident,changes['phone_device_id'])
    if 'office_id' in changes:
        import office_scope
        office_scope.save_assignment(db,actor,'accounts',ident,changes)
    return ident

def save(db,body,actor,day,create=False):
    # Serialize identity validation and writes across request threads.

    if getattr(db,'scope_enabled',False):auth.authorize_route(db,actor,'/api/account/create' if create else '/api/account',body)
    if create:
        db.execute("INSERT INTO computers VALUES('unassigned','未分配电脑','','','','') ON CONFLICT(id) DO NOTHING")
        account_id = 'account-' + uuid.uuid4().hex
        if body.get('office_group','') not in ('','three'):raise ValueError('办公室不正确')
        db.execute("INSERT INTO accounts(id,computer_id,slot,name,status,note,douyin_id,homepage,last_collected) VALUES(?,'unassigned',NULL,'新账号','待确认','','','','')", (account_id,))
        db.execute('UPDATE accounts SET office_group=? WHERE id=?',(body.get('office_group',''),account_id))
    else:
        account_id = str(body['id'])
    if not db.execute('SELECT 1 FROM accounts WHERE id=?',(account_id,)).fetchone():
        raise ValueError('账号不存在')
    if body['status'] not in STATES:
        raise ValueError('账号状态不正确')
    name = str(body['name']).strip()
    if not name or len(name)>100:
        raise ValueError('账号名称不能为空或超过100字')
    computer = str(body['computer_id']); slot = int(body['slot'])
    if not 1 <= slot <= 100 or not db.execute('SELECT 1 FROM computers WHERE id=?',(computer,)).fetchone():
        raise ValueError('电脑或账号位置不正确')
    if computer == 'unassigned':
        slot = None
    homepage = collection.normalize_homepage(body.get('homepage',''))
    douyin_id=str(body.get('douyin_id','')).strip()
    if len(douyin_id)>100 or any(c.isspace() for c in douyin_id):
        raise ValueError('真实抖音号不能包含空格或超过100字')
    old=db.execute('SELECT * FROM accounts WHERE id=?',(account_id,)).fetchone()
    if douyin_id and douyin_id != old['douyin_id'] and db.execute('SELECT 1 FROM accounts WHERE douyin_id=? AND id<>?',(douyin_id,account_id)).fetchone():
        raise ValueError('这个抖音号已经在工作台中，请编辑已有账号')
    auth.authorize_account_fields(actor,body,old)
    lifecycle_values=account_lifecycle.validate(db,account_id,body,old,day,actor=actor)
    if old['douyin_id']!=douyin_id and old['homepage']==homepage:
        homepage=''
    if old['homepage'] != homepage or old['douyin_id'] != douyin_id:
        # Disabled collection tasks remain reviewable even when account identity changes.
        # Counts still reset so the old identity's result cannot describe the new identity.
        db.execute("UPDATE accounts SET work_count=NULL,last_collected='',last_attempted='',collection_state='',collection_error='' WHERE id=?",(account_id,))
    if body['status']=='养号7天' and old['status']!='养号7天':db.execute("UPDATE accounts SET nurture_started='' WHERE id=?",(account_id,))
    db.execute('UPDATE accounts SET name=?,status=?,note=?,computer_id=?,slot=?,douyin_id=?,homepage=? WHERE id=?',(name,body['status'],str(body.get('note',''))[:2000],computer,slot,douyin_id,homepage,account_id))
    db.execute('UPDATE accounts SET account_type=?,parent_account_id=?,registered_on=?,auxiliary_state=?,lifecycle_note=?,registration_phone=? WHERE id=?',(*lifecycle_values,account_id))
    phone_cards.update_readiness(db,account_id,body)
    if 'phone_device_id' in body and (body['phone_device_id'] or None)!=(old['phone_device_id'] or None):phone_devices.assign(db,account_id,body['phone_device_id'])
    if getattr(db,'scope_enabled',False):
        import office_scope
        office_scope.save_assignment(db,actor,'accounts',account_id,body,create)
    return account_id
