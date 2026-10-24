"""Local accounts, revocable sessions and owner-only permission assignment."""
import hashlib
import hmac
import json
import re
import secrets
import time
from http.cookies import SimpleCookie

PERMISSIONS={
    'nurture_manage':'管理养号进度及截图',
    'history_view':'查看业务操作历史','data_import':'预览及确认资料导入','data_export':'导出资料与模板',
    'publishing_manage':'管理发布格子关联',
    'publish':'记录当天发布', 'account_status':'修改账号状态',
    'account_progress':'修改注册进度、稳定及改密记录',
    'account_create':'新增抖音账号 / 小号', 'account_manage':'修改账号基础资料',
    'card_tasks':'处理手机卡状态、接码和归还',
    'card_create':'新增手机卡', 'card_manage':'修改手机卡基础资料', 'card_import':'从已有账号补录手机卡',
    'device_create':'新增手机设备', 'device_manage':'修改手机设备及账号关联',
    'collection_manage':'操作作品采集及采集设置',
}
DEFAULTS={'admin':list(PERMISSIONS),'employee':['publish','account_status','account_progress','card_tasks'],'viewer':[]}
CARD_TASK_FIELDS={'status','code_state','code_note','return_state','return_due','returned_on'}
ROLES={'owner':'总管理员','admin':'管理员','employee':'员工','viewer':'只读人员'}
SESSION_SECONDS=12*3600
PERMISSION_GROUPS=[('养号管理',['nurture_manage']),('发布与账号',['publish','publishing_manage','account_status','account_progress','account_create','account_manage']),
                   ('手机卡',['card_tasks','card_create','card_manage','card_import']),
                   ('手机设备',['device_create','device_manage']),('作品采集',['collection_manage']),('数据管理',['history_view','data_import','data_export'])]
PERMISSION_DETAILS={
 'nurture_manage':'加入待检查、记录核对情况/负责人/备注、暂停养号及管理截图；开始、继续和结束养号还需修改账号状态权限。',
 'history_view':'查看业务资料修改人、时间及字段前后值；不展示登录密码。',
 'data_import':'上传Excel/CSV并预览差异，确认所选行；实际写入仍需要对应新增、修改及字段权限。',
 'data_export':'导出筛选资料及导入模板，不导出人员密码和登录会话。',
 'publishing_manage':'关联待匹配发布格子，调整或取消手动关联；不修改账号基础资料、原表文字和历史发布记录。',
 'publish':'勾选或取消当天是否发布，不代表自动操作抖音发布。',
 'account_status':'修改正常、封禁、养号7天等状态，支持批量修改。',
 'account_progress':'修改小号注册进度、账号稳定和改密记录。',
 'account_create':'录入新的抖音账号资料；不包含修改已有账号。初始状态/手机关联还需对应权限。',
 'account_manage':'修改已有账号昵称、抖音号、手机号、注册日期、大小号关系、备注和资料电脑；状态、进度及手机关联另行授权。',
 'card_tasks':'记录卡状态、待注销、接码事项、归还状态及日期；不改卡主和号码。',
 'card_create':'新增一张手机卡并填写初始资料；不包含修改已有卡。',
 'card_manage':'修改已有卡的号码、卡主、归属、位置、联系人和备注；卡状态与待办另行授权。',
 'card_import':'根据工作台已有账号手机号补录缺少的卡，不包含任意文件导入。',
 'device_create':'新增手机设备库存记录；不包含更改已有设备或移动账号。',
 'device_manage':'修改设备编号、状态、负责人和位置，关联、移入或移出账号登录手机。',
 'collection_manage':'提交作品数量采集任务、更改自动采集设置，以及在本机桌面程序打开采集浏览器。'}


class AccessError(ValueError):
    def __init__(self,message,status=403):super().__init__(message);self.status=status


def migrate(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS auth_users(id INTEGER PRIMARY KEY,username TEXT NOT NULL UNIQUE,
      name TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('owner','admin','employee','viewer')),
      permissions TEXT NOT NULL DEFAULT '[]',active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
      password_hash TEXT NOT NULL,created_at INTEGER NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS auth_single_owner ON auth_users(role) WHERE role='owner';
    CREATE TABLE IF NOT EXISTS auth_sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES auth_users(id),
      csrf TEXT NOT NULL,expires_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS auth_setup(token_hash TEXT PRIMARY KEY,expires_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS auth_limits(key TEXT PRIMARY KEY,failures INTEGER NOT NULL,expires_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS auth_audit(id INTEGER PRIMARY KEY,at INTEGER NOT NULL,actor_id INTEGER,
      actor_name TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL);
    ''')
    # Preserve existing grants once when separating creation from editing.
    db.execute('CREATE TABLE IF NOT EXISTS auth_migrations(name TEXT PRIMARY KEY)')
    if not db.execute("SELECT 1 FROM auth_migrations WHERE name='split-create-v1'").fetchone():
        for row in db.execute("SELECT id,permissions FROM auth_users WHERE role<>'owner'").fetchall():
            grants=set(json.loads(row[1]))
            for legacy,extra in [('account_manage',['account_create']),('card_manage',['card_create','card_import']),('device_manage',['device_create'])]:
                if legacy in grants:grants.update(extra)
            db.execute('UPDATE auth_users SET permissions=? WHERE id=?',(json.dumps(sorted(grants)),row[0]))
        db.execute("INSERT INTO auth_migrations VALUES('split-create-v1')")


def digest(value):return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password):
    if not isinstance(password,str) or not 10<=len(password)<=128:raise ValueError('密码请设置为10至128位')
    salt=secrets.token_hex(16)
    value=hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),600000).hex()
    return 'pbkdf2_sha256$600000$'+salt+'$'+value


def password_matches(password,encoded):
    if not isinstance(password,str) or len(password)>128:return False
    _,iterations,salt,expected=encoded.split('$')
    actual=hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),int(iterations)).hex()
    return hmac.compare_digest(actual,expected)


def cookies(header):
    result=SimpleCookie()
    try:result.load(header or '')
    except Exception:return {}
    return {key:item.value for key,item in result.items()}


def cookie(name,value,secure=False,age=SESSION_SECONDS):
    return f'{name}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}'+('; Secure' if secure else '')


def initialized(db):return bool(db.execute("SELECT 1 FROM auth_users WHERE role='owner'").fetchone())


def mint_setup(db):
    if initialized(db):return None
    token=secrets.token_urlsafe(32)
    db.execute('DELETE FROM auth_setup')
    db.execute('INSERT INTO auth_setup VALUES(?,?)',(digest(token),int(time.time())+1800))
    return token


def can_setup(db,header):
    token=cookies(header).get('board_setup','')
    return not initialized(db) and bool(token) and bool(db.execute('SELECT 1 FROM auth_setup WHERE token_hash=? AND expires_at>?',(digest(token),int(time.time()))).fetchone())


def public_user(row):
    return dict(id=row['id'],username=row['username'],name=row['name'],role=row['role'],active=bool(row['active']),
                permissions=list(PERMISSIONS) if row['role']=='owner' else json.loads(row['permissions']))


def session(db,header):
    token=cookies(header).get('board_session','')
    if not token:return None
    row=db.execute('SELECT u.*,s.csrf FROM auth_sessions s JOIN auth_users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1',(digest(token),int(time.time()))).fetchone()
    if not row:return None
    result={**public_user(row),'csrf':row['csrf']}
    if getattr(db,'scope_enabled',False):
        import office_scope
        result.update(authorization_version=row['authorization_version'],office_ids=sorted(office_scope.granted_offices(db,result)),scope_mode='all' if office_scope.global_scope(result) else 'assigned')
    return result


def new_session(db,user_id):
    token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32);now=int(time.time())
    db.execute('DELETE FROM auth_sessions WHERE expires_at<=?',(now,))
    db.execute('INSERT INTO auth_sessions VALUES(?,?,?,?)',(digest(token),user_id,csrf,now+SESSION_SECONDS))
    return token


def require(user,permission=None):
    if not user:raise AccessError('请先登录工作台',401)
    if permission=='owner':
        if user['role']!='owner':raise AccessError('只有总管理员可以管理人员和分配权限')
    elif permission and permission not in user['permissions']:raise AccessError('当前账号没有此操作权限')


def audit(db,user,action,target=''):
    if user:db.execute('INSERT INTO auth_audit(at,actor_id,actor_name,action,target) VALUES(?,?,?,?,?)',
                       (int(time.time()),user['id'],user['name'],str(action)[:100],str(target)[:200]))


def identity(body):
    username=str(body.get('username','')).strip()
    name=str(body.get('name','')).strip() or username
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,40}',username):raise ValueError('登录名请使用3至40位字母、数字、点、横线或下划线')
    if not 1<=len(name)<=60:raise ValueError('姓名不能超过60字')
    return username,name


def setup_owner(db,body,header):
    if not can_setup(db,header):raise AccessError('请在服务器本机桌面软件中创建总管理员；若页面过期，请重启软件')
    username,name=identity(body)
    encoded=password_hash(body.get('password'))
    user_id=db.execute("INSERT INTO auth_users(username,name,role,password_hash,created_at) VALUES(?,?,'owner',?,?) RETURNING id",(username,name,encoded,int(time.time()))).fetchone()[0]
    db.execute('DELETE FROM auth_setup')
    audit(db,dict(id=user_id,name=name),'创建总管理员',username)
    return new_session(db,user_id)


def login(db,body,ip):
    username=str(body.get('username','')).strip();now=int(time.time())
    db.execute('DELETE FROM auth_limits WHERE expires_at<=?',(now,))
    keys=[digest('login:'+ip+':'+username.casefold()),digest('ip:'+ip)]
    for key,limit in zip(keys,[5,30]):
        row=db.execute('SELECT * FROM auth_limits WHERE key=? AND expires_at>?',(key,now)).fetchone()
        if row and row['failures']>=limit:raise AccessError('登录尝试过多，请15分钟后再试',429)
    row=db.execute('SELECT * FROM auth_users WHERE username=?',(username,)).fetchone()
    encoded=row['password_hash'] if row else 'pbkdf2_sha256$600000$unavailable$'+'0'*64
    valid=password_matches(body.get('password'),encoded)
    if not row or not valid or not row['active']:
        for key in keys:
            db.execute('INSERT INTO auth_limits VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET failures=CASE WHEN auth_limits.expires_at<=? THEN 1 ELSE auth_limits.failures+1 END,expires_at=CASE WHEN auth_limits.expires_at<=? THEN excluded.expires_at ELSE auth_limits.expires_at END',(key,now+900,now,now))
        return None
    db.execute('DELETE FROM auth_limits WHERE key=?',(keys[0],))
    audit(db,public_user(row),'登录',username)
    return new_session(db,row['id'])


def save_user(db,actor,body):
    require(actor,'owner')
    role=body.get('role','employee');permissions=body.get('permissions',DEFAULTS.get(role,[]));active=body.get('active',True)
    if role not in DEFAULTS or not isinstance(permissions,list) or any(p not in PERMISSIONS for p in permissions):raise ValueError('角色或权限不正确；不能新增总管理员')
    if type(active) is not bool:raise ValueError('启用状态不正确')
    old=db.execute('SELECT * FROM auth_users WHERE id=?',(body.get('id'),)).fetchone() if body.get('id') else None
    if body.get('id') and not old:raise ValueError('人员不存在')
    if old and old['role']=='owner':raise AccessError('总管理员不能被修改角色、停用或由人员管理重置密码')
    username,name=identity({**body,'username':old['username']} if old else body)
    permissions=json.dumps(sorted(set(permissions)))
    encoded=password_hash(body['password']) if body.get('password') else None
    if old:
        db.execute('UPDATE auth_users SET name=?,role=?,permissions=?,active=? WHERE id=?',(name,role,permissions,int(active),old['id']))
        if encoded:db.execute('UPDATE auth_users SET password_hash=? WHERE id=?',(encoded,old['id']))
        # Changed grants, account status or password revoke every prior session immediately.
        db.execute('DELETE FROM auth_sessions WHERE user_id=?',(old['id'],));user_id=old['id']
    else:
        if not encoded:raise ValueError('请设置初始密码（10至128位）')
        if db.execute('SELECT 1 FROM auth_users WHERE username=?',(username,)).fetchone():raise ValueError('登录名已存在')
        user_id=db.execute('INSERT INTO auth_users(username,name,role,permissions,active,password_hash,created_at) VALUES(?,?,?,?,?,?,?) RETURNING id',(username,name,role,permissions,int(active),encoded,int(time.time()))).fetchone()[0]
    if getattr(db,'scope_enabled',False):
        import office_scope
        office_ids=body.get('office_ids',[row[0] for row in db.execute('SELECT office_id FROM user_office_grants WHERE user_id=?',(user_id,))] if old else [])
        office_scope.grant_user(db,actor,user_id,office_ids)
    audit(db,actor,'更新人员与权限' if old else '新增人员',username)
    return user_id


ACTION_PERMISSIONS={'/api/daily':'publish','/api/bulk-status':'account_status','/api/account/status':'account_status',
      '/api/publishing/link':'publishing_manage',
      '/api/publishing/assignment':'publishing_manage','/api/account/nurture':'account_status',
      '/api/workspace/import-preview':'data_import','/api/workspace/import-apply':'data_import','/api/workspace/export':'data_export',
      '/api/workspace/backup-now':'owner','/api/workspace/restore-preview':'owner','/api/workspace/restore-apply':'owner',
      '/api/lifecycle-status':'account_progress','/api/account/card-readiness':'account_progress',
      '/api/account':'account_manage','/api/account/create':'account_create',
      '/api/phone-device':'device_manage','/api/phone-device/create':'device_create','/api/account/phone-device':'device_manage',
      '/api/phone-cards/import':'card_import','/api/collection/enqueue':'collection_manage','/api/collection/settings':'collection_manage'}


def require_stored_action(user, path):
    """Recheck the current operation permission for a stored request. The original body is not kept."""
    require(user)
    if path in ('/api/auth/users','/api/auth/offices','/api/auth/entity-scope'):
        require(user,'owner')
        return
    if path.startswith('/api/workspace/nurture/'):
        require(user,'nurture_manage')
        return
    if path=='/api/phone-card':
        if any(item in user['permissions'] for item in ('card_create','card_manage','card_tasks')):return
        require(user,'card_manage')
        return
    if path=='/api/account/patch':
        # The stored body is not kept, so field-level authorization cannot be repeated.
        # Any permission that can authorize a patch field may read this user's stored result.
        import account_service
        if any(item in user['permissions'] for item in set(account_service.PATCH_PERMISSIONS.values())):return
        require(user,'account_manage')
        return
    if path=='/api/batch-edit':
        # The stored body is not kept, so field-level authorization cannot be repeated.
        # Any permission that can authorize a batch field may read this user's stored result.
        import batch_edit
        if any(item in user['permissions'] for item in batch_edit.BATCH_PERMISSIONS):return
        require(user,'account_status')
        return
    if path not in ACTION_PERMISSIONS:raise AccessError('接口未开放',404)
    require(user,ACTION_PERMISSIONS[path])


def authorize_route(db,user,path,body):
    if getattr(db,'scope_enabled',False):
        import office_scope
        office_scope.authorize_route(db,user,path,body)
    if path=='/api/account/patch':
        import account_service
        account_service.authorize_patch(db,user,body)
        return
    if path=='/api/batch-edit':
        import batch_edit
        batch_edit.authorize(user,body)
        return
    require(user)
    if path.startswith('/api/workspace/nurture/'):
        require(user,'nurture_manage')
        if path.endswith(('/start','/resume','/finish')):require(user,'account_status')
        return
    routes=ACTION_PERMISSIONS
    if path=='/api/phone-card':
        old=db.execute('SELECT * FROM phone_cards WHERE id=?',(body.get('id'),)).fetchone()
        if not old and not body.get('id'):
            require(user,'card_create')
            return
        if 'card_manage' in user['permissions']:
            if old and 'card_tasks' not in user['permissions']:
                for key in CARD_TASK_FIELDS:
                    if key in body and body[key]!=old[key]:raise AccessError('修改卡状态、接码和归还需要手机卡待办权限')
            return
        require(user,'card_tasks')
        if not old:raise AccessError('新增手机卡需要手机卡基础资料权限')
        allowed={'id','_expected_versions','request_id'}|CARD_TASK_FIELDS
        for key,value in body.items():
            if key not in allowed and (key not in old.keys() or value!=old[key]):raise AccessError('只能修改卡状态、接码及归还事项，不能修改基础资料')
        return
    if path not in routes:raise AccessError('接口未开放',404)
    require(user,routes[path])


def authorize_account_fields(user,body,old):
    for fields,permission in [(['status'],'account_status'),
                             (['auxiliary_state','card_stability','password_state'],'account_progress'),
                             (['phone_device_id'],'device_manage')]:
        if permission not in user['permissions']:
            for key in fields:
                if key in body and (body[key] or None)!=(old[key] or None):
                    raise AccessError('修改该字段还需要：'+PERMISSIONS[permission])
