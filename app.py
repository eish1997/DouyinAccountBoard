import argparse
import json
import sqlite3
import os
import threading
from contextlib import contextmanager
from datetime import datetime, date, timezone, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import collection
import account_lifecycle
import backup
import phone_cards
import phone_devices
import auth
import publishing_layout
import operations
import account_service
import workspace_service
import nurture
import office_scope
import idempotency
from release_policy import COLLECTION_MESSAGE
from sql_dialect import begin_write, begin_read, integer_order, integrity_errors, transient_errors, AdmissionRejected

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ['DOUYIN_BOARD_DATA_DIR']) / 'board.db' if os.environ.get('DOUYIN_BOARD_DATA_DIR') else None
STATES = ['正常', '养号7天', '短期封禁', '封禁', '发不了', '停用', '待确认']
WORKER_TOKEN = os.environ.get('DOUYIN_BOARD_WORKER_TOKEN', '')
_DATABASE_PROVIDER = None


def configure_database(provider):
    global _DATABASE_PROVIDER
    if _DATABASE_PROVIDER is not None:
        raise ValueError('Database provider cannot be replaced in a running process')
    _DATABASE_PROVIDER = provider

def today():
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()

@contextmanager
def connect():
    if _DATABASE_PROVIDER is not None:
        with _DATABASE_PROVIDER.connect() as db:
            yield db
        return
    if DB is None:
        raise ValueError('必须显式配置数据目录，禁止回退到默认数据库')
    if os.environ.get('DOUYIN_BOARD_TEST_ROOT'):
        from test_isolation import checked_database
        checked_database(DB)
    test_mode = bool(os.environ.get('DOUYIN_BOARD_TEST_ROOT'))
    db = sqlite3.connect(DB if test_mode else DB.resolve().as_uri() + '?mode=rw',
                         uri=not test_mode, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        with db:
            yield db
    finally:
        db.close()

def initialize():
    if _DATABASE_PROVIDER is not None:
        _DATABASE_PROVIDER.validate_startup()
        return
    if DB is None:
        raise ValueError('必须显式配置数据目录，禁止回退到默认数据库')
    if os.environ.get('DOUYIN_BOARD_TEST_ROOT'):
        from test_isolation import checked_database
        checked_database(DB)
    elif not DB.is_file():
        raise ValueError('数据库不存在；请使用受控初始化流程，禁止自动创建或导入种子')
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        if not os.environ.get('DOUYIN_BOARD_TEST_ROOT'):
            try:
                initialized = db.execute("SELECT 1 FROM imports WHERE name='initial-image'").fetchone()
            except sqlite3.OperationalError:
                initialized = None
            if not initialized:
                raise ValueError('数据库未受控初始化；禁止自动创建结构或读取客户种子')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS computers(id TEXT PRIMARY KEY,name TEXT,shadow_account TEXT,ip TEXT,operator TEXT,category TEXT);
        CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,computer_id TEXT REFERENCES computers(id),slot INTEGER,name TEXT,status TEXT,note TEXT,douyin_id TEXT,homepage TEXT,work_count INTEGER,last_collected TEXT,UNIQUE(computer_id,slot));
        CREATE TABLE IF NOT EXISTS daily(account_id TEXT REFERENCES accounts(id),day TEXT,published INTEGER NOT NULL CHECK(published IN (0,1)),PRIMARY KEY(account_id,day));
        CREATE TABLE IF NOT EXISTS imports(name TEXT PRIMARY KEY);
        ''')
        if not db.execute("SELECT 1 FROM imports WHERE name='initial-image'").fetchone():
            if os.environ.get('DOUYIN_BOARD_TEST_ROOT'):
                from test_isolation import synthetic_seed
                seed = synthetic_seed()
            else:
                raise ValueError('数据库未受控初始化；禁止自动读取客户种子')
            for c in seed['computers']:
                db.execute('INSERT INTO computers VALUES(:id,:name,:shadow_account,:ip,:operator,:category)', c)
            for a in seed['accounts']:
                db.execute('INSERT INTO accounts(id,computer_id,slot,name,status,note,douyin_id,homepage,work_count,last_collected) VALUES(:id,:computer_id,:slot,:name,:status,:note,:douyin_id,:homepage,:work_count,:last_collected)', a)
            db.execute("INSERT INTO imports VALUES('initial-image')")
        collection.migrate(db)
        account_lifecycle.migrate(db)
        phone_cards.migrate(db)
        phone_devices.migrate(db)
        auth.migrate(db)
        publishing_layout.migrate(db)
        operations.migrate(db)
        nurture.migrate(db)
        nurture.sync(db,today())
        # Collection is disabled; retained task states are historical, not executable leases.

# A stalled connection or request body stops after this many idle seconds.
# Bytes that keep arriving are not cut off by the total transfer time.
REQUEST_IO_TIMEOUT = 8
# Above the 75-user peak in the load profile. This cap is not capacity acceptance.
REQUEST_CONCURRENCY_LIMIT = 128
REQUEST_QUEUE_LIMIT = 256
_REQUEST_SLOTS = threading.BoundedSemaphore(REQUEST_CONCURRENCY_LIMIT)
ThreadingHTTPServer.request_queue_size = REQUEST_QUEUE_LIMIT

class Handler(SimpleHTTPRequestHandler):
    timeout = REQUEST_IO_TIMEOUT

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'static'), **kwargs)

    def handle(self):
        acquired = _REQUEST_SLOTS.acquire(blocking=False)
        try:
            if not acquired:
                self._reject_concurrency()
                return
            super().handle()
        finally:
            if acquired:
                _REQUEST_SLOTS.release()

    def _reject_concurrency(self):
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if not self.raw_requestline or len(self.raw_requestline) > 65536 or not self.parse_request():
                self.close_connection = True
                return
            length = int(self.headers.get('Content-Length', '0') or '0')
            if 0 < length <= 100000:
                self.rfile.read(length)
            self.close_connection = True
            self.reply({'error': '同时处理的请求过多，请稍后重试', 'code': 'CONCURRENCY_LIMIT'}, 503)
        except (TimeoutError, ValueError, OSError):
            self.close_connection = True

    def reply(self, value, status=200, headers=None):
        if isinstance(value,dict) and hasattr(self,'_version_delta'):value={**value,'_record_versions':self._version_delta}
        payload = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(payload)))
        for key,value in headers or []:self.send_header(key,value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        try:
            return self.get_response()
        except transient_errors() as error:
            return self.database_failure(error)
        except auth.AccessError as error:
            return self.reply({'error':str(error)},error.status)

    def database_failure(self,error):
        state=getattr(error,'sqlstate',None)
        code='DB_BUSY' if state=='55P03' or 'locked' in str(error).lower() else 'DB_TIMEOUT' if state=='57014' else 'DB_UNAVAILABLE'
        payload={'error':'数据服务暂时不可用，请稍后刷新核对操作结果','code':code}
        try:payload['request_id']=idempotency.request_uuid(getattr(self,'_failure_request_id',None))
        except ValueError:pass
        return self.reply(payload,503)

    def _export_failure(self,db,body,code,message):
        db.rollback()
        payload={'error':message,'code':code}
        try:payload['request_id']=idempotency.request_uuid(body.get('request_id'))
        except ValueError:pass
        return self.reply(payload,503)

    def get_response(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith('/api/collection/') and parsed.path != '/api/collection/status':
            return self.reply({'error': COLLECTION_MESSAGE, 'code': 'COLLECTION_DISABLED'}, 503)
        if parsed.path=='/api/nurture/image':
            with connect() as db:
                begin_read(db)
                actor=auth.session(db,self.headers.get('Cookie'))
                if not actor:return self.reply({'error':'请先登录'},401)
                image_id=parse_qs(parsed.query).get('id',[''])[0]
                row=db.execute('SELECT account_id,mime FROM nurture_images WHERE id=?',(image_id,)).fetchone()
                if not row:return self.reply({'error':'截图不存在'},404)
                if getattr(db,'scope_enabled',False):office_scope.require_entity(db,actor,'accounts',row['account_id'])
                content=db.execute('SELECT content FROM nurture_images WHERE id=?',(image_id,)).fetchone()[0]
                self.send_response(200);self.send_header('Content-Type',row['mime']);self.send_header('Content-Length',str(len(content)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(content);return
        if parsed.path.startswith('/api/workspace/'):
            try:
                with connect() as db:
                    begin_read(db)
                    actor=auth.session(db,self.headers.get('Cookie'));auth.require(actor)
                    return self.reply(workspace_service.get(db,actor,parsed.path,parse_qs(parsed.query),DB))
            except auth.AccessError as error:return self.reply({'error':str(error)},error.status)
            except (ValueError,KeyError) as error:return self.reply({'error':str(error)},400)
        if parsed.path=='/api/ready':
            with connect() as db:
                begin_read(db)
                row=db.execute('SELECT mode FROM runtime_control WHERE id=1').fetchone()
                if not row or row['mode']!='open':return self.reply({'ready':False,'code':'NOT_READY'},503)
                return self.reply({'ready':True})
        if parsed.path=='/api/health':
            return self.reply({'service':'douyin-account-board','shared':os.environ.get('DOUYIN_BOARD_SHARED_WEB')=='1','authentication_required':True})
        if parsed.path=='/api/idempotency/result':
            try:
                with connect() as db:
                    begin_read(db)
                    actor=auth.session(db,self.headers.get('Cookie'));auth.require(actor)
                    request_id=parse_qs(parsed.query).get('request_id',[''])[0]
                    outcome=idempotency.lookup(db,actor,request_id)
                    if outcome[0]!='missing':auth.require_stored_action(actor,outcome[2])
                    if outcome[0]=='missing':return self.reply({'error':'没有这条请求记录','code':'REQUEST_NOT_FOUND'},404)
                    if outcome[0]=='generation':raise idempotency.IdempotencyError('RESTORE_GENERATION_EXPIRED','请求属于已关闭的恢复代次，不能再次执行')
                    if outcome[0]=='pending':return self.reply({'error':'结果待确认','code':'REQUEST_PENDING'},409)
                    if outcome[0]=='expired':raise idempotency.IdempotencyError('RESULT_EXPIRED','该请求的结果已超过保留期，不能再次执行')
                    status,body=outcome[3],outcome[4]
                    return self.reply({**body,'code':'REQUEST_SUCCEEDED' if status==200 else 'REQUEST_FAILED'},status)
            except auth.AccessError as error:return self.reply({'error':str(error)},error.status)
            except idempotency.IdempotencyError as error:return self.reply({'error':str(error),'code':error.code},error.status)
            except ValueError as error:return self.reply({'error':str(error)},400)
        if parsed.path=='/api/collection/status':
            with connect() as db:
                begin_read(db)
                actor=auth.session(db,self.headers.get('Cookie'))
                if not actor:return self.reply({'error':'请先登录工作台'},401)
                accounts=None
                if getattr(db,'scope_enabled',False):
                    allowed=office_scope.visible_ids(db,actor,'accounts')
                    accounts=[dict(row) for row in db.execute('SELECT * FROM accounts') if row['id'] in allowed]
                return self.reply({'collection':collection.overview(db,accounts),'can_manage':False})
        if parsed.path=='/api/auth/me':
            with connect() as db:
                user=auth.session(db,self.headers.get('Cookie'))
                state=dict(user=user,initialized=auth.initialized(db),can_setup=auth.can_setup(db,self.headers.get('Cookie')),
                           permissions=auth.PERMISSIONS,roles=auth.ROLES,defaults=auth.DEFAULTS,
                           permission_groups=auth.PERMISSION_GROUPS,permission_details=auth.PERMISSION_DETAILS)
            return self.reply(state)
        if parsed.path=='/api/auth/entity-scopes':
            with connect() as db:
                begin_read(db)
                actor=auth.session(db,self.headers.get('Cookie'));auth.require(actor,'owner')
                if not getattr(db,'scope_enabled',False):return self.reply({'error':'当前模式未启用办公室范围'},400)
                query=parse_qs(parsed.query)
                return self.reply(office_scope.inventory(db,actor,query.get('kind',['computers'])[0],query.get('after',[''])[0]))
        if parsed.path in ['/api/auth/users','/api/auth/audit']:
            try:
                with connect() as db:
                    begin_read(db)
                    actor=auth.session(db,self.headers.get('Cookie'));auth.require(actor,'owner')
                    result={'users':[auth.public_user(r) for r in db.execute('SELECT * FROM auth_users ORDER BY id')]} if parsed.path.endswith('/users') else {'entries':[dict(r) for r in db.execute('SELECT * FROM auth_audit ORDER BY id DESC LIMIT 200')]}
                    if 'users' in result and getattr(db,'scope_enabled',False):
                        result['offices']=office_scope.list_offices(db,actor)
                        result['scope_enabled']=True
                        for person in result['users']:
                            person['office_ids']=[row[0] for row in db.execute('SELECT office_id FROM user_office_grants WHERE user_id=? ORDER BY office_id',(person['id'],))]
                            person['scope_mode']='all' if office_scope.global_scope(person) else 'assigned'
                return self.reply(result)
            except auth.AccessError as error:return self.reply({'error':str(error)},error.status)
        if parsed.path == '/api/collection/next':
            if not self.is_worker():
                return self.reply({'error':'仅桌面采集服务可调用'},403)
            if self.headers.get('X-Collection-Engine')!='f2':
                return self.reply({'job':None})
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                job=collection.next_job(db,today())
            return self.reply({'job':job})
        if parsed.path != '/api/board':
            if parsed.path.startswith('/api/'):return self.reply({'error':'接口不存在'},404)
            return super().do_GET()
        with connect() as db:
            actor=auth.session(db,self.headers.get('Cookie'))
        if not actor:return self.reply({'error':'请先登录工作台'},401)
        try:
            day = parse_qs(parsed.query).get('date', [today()])[0]
            date.fromisoformat(day)
            with connect() as db:
                begin_read(db)
                actor=auth.session(db,self.headers.get('Cookie'))
                if not actor:return self.reply({'error':'请先登录工作台'},401)
                computer_order=integer_order(db,"CASE WHEN id LIKE 'office3:%' THEN substr(id,9) ELSE id END")
                computers = [dict(r) for r in db.execute("SELECT * FROM computers ORDER BY CASE WHEN id LIKE 'office3:%' THEN 1 ELSE 0 END,"+computer_order+",id")]
                accounts = [dict(r) for r in db.execute('SELECT a.*,COALESCE(d.published,0) AS published FROM accounts a LEFT JOIN daily d ON d.account_id=a.id AND d.day=? ORDER BY '+integer_order(db,'a.computer_id')+',a.slot', (day,))]
                context=office_scope.read_context(db,actor) if getattr(db,'scope_enabled',False) else None
                if context:computers,accounts=office_scope.filter_board_entities(context,computers,accounts)
                collection_info=collection.overview(db,accounts if context else None)
                account_lifecycle.enrich(accounts, today())
                card_info=phone_cards.overview(db,context['ids']['cards'] if context else None,accounts if context else None)
                device_info=phone_devices.overview(db,accounts,context['ids']['devices'] if context else None)
                if context:
                    for kind,rows in [('cards',card_info['cards']),('devices',device_info['devices'])]:
                        for row in rows:row['office_ids']=context['maps'][kind].get(row['id'],[])
                layout_info=publishing_layout.overview(db,context['ids']['accounts'] if context else None,context['ids']['computers'] if context else None)
                record_versions=operations.versions(db,context)
                nurture_info=nurture.overview(db,today(),context['ids']['accounts'] if context else None)
                offices=office_scope.list_offices(db,actor) if context else []
            self.reply(dict(computers=computers,accounts=accounts,date=day,today=today(),statuses=STATES,collection=collection_info,backup=backup.status(),phone_cards=card_info,phone_devices=device_info,user=actor,publishing_layout=layout_info,nurture=nurture_info,record_versions=record_versions,offices=offices))
        except ValueError:
            self.reply({'error':'日期格式不正确'}, 400)

    def is_worker(self):
        return bool(WORKER_TOKEN) and auth.hmac.compare_digest(self.headers.get('X-Worker-Token',''),WORKER_TOKEN)

    def auth_post(self,body):
        secure=os.environ.get('DOUYIN_BOARD_PUBLIC_ORIGIN','').startswith('https://')
        with connect() as db:
            begin_write(db)
            def finish(value,status=200,headers=None):
                db.commit()
                return self.reply(value,status,headers)
            if self.path=='/api/auth/desktop-setup':
                if not self.is_worker():raise auth.AccessError('仅服务器本机桌面软件可以初始化')
                token=auth.mint_setup(db)
                return finish({'setup_token':token})
            if self.path=='/api/auth/setup':
                token=auth.setup_owner(db,body,self.headers.get('Cookie'))
                return finish({'ok':True},headers=[('Set-Cookie',auth.cookie('board_session',token,secure)),('Set-Cookie',auth.cookie('board_setup','',secure,0))])
            if self.path=='/api/auth/login':
                token=auth.login(db,body,self.client_address[0])
                if not token:return finish({'error':'登录名或密码不正确，或账号已停用'},401)
                return finish({'ok':True},headers=[('Set-Cookie',auth.cookie('board_session',token,secure))])
            actor=auth.session(db,self.headers.get('Cookie'));auth.require(actor)
            if not auth.hmac.compare_digest(self.headers.get('X-CSRF-Token',''),actor['csrf']):raise auth.AccessError('页面已过期，请刷新后重试')
            if self.path=='/api/auth/logout':
                token=auth.cookies(self.headers.get('Cookie')).get('board_session','')
                db.execute('DELETE FROM auth_sessions WHERE token_hash=?',(auth.digest(token),))
                auth.audit(db,actor,'退出登录')
                return finish({'ok':True},headers=[('Set-Cookie',auth.cookie('board_session','',secure,0))])
            if self.path=='/api/auth/users':
                auth.require(actor,'owner')
                if getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'保存人员必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if body.get('request_id') and getattr(db,'restore_generation',None):
                    return self.idempotent_create(db,actor,self.path,body,lambda database,current:{'ok':True,'id':auth.save_user(database,current,body)},recheck=lambda database,current:auth.require(current,'owner'),record_operations=False)
                user_id=auth.save_user(db,actor,body)
                return finish({'ok':True,'id':user_id})
            if self.path=='/api/auth/offices' and getattr(db,'scope_enabled',False):
                auth.require(actor,'owner')
                if getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'新增办公室必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if body.get('request_id') and getattr(db,'restore_generation',None):
                    return self.idempotent_create(db,actor,self.path,body,lambda database,current:{'ok':True,'id':office_scope.create_office(database,current,body.get('name'))},recheck=lambda database,current:auth.require(current,'owner'),record_operations=False)
                ident=office_scope.create_office(db,actor,body.get('name'))
                return finish({'ok':True,'id':ident})
            if self.path=='/api/auth/entity-scope' and getattr(db,'scope_enabled',False):
                auth.require(actor,'owner')
                if getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'分配归属必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if body.get('request_id') and getattr(db,'restore_generation',None):
                    return self.idempotent_create(db,actor,self.path,body,lambda database,current:office_scope.reassign(database,current,body),recheck=lambda database,current:auth.require(current,'owner'),record_operations=False)
                return finish(office_scope.reassign(db,actor,body))
            if self.path=='/api/auth/password':
                row=db.execute('SELECT * FROM auth_users WHERE id=?',(actor['id'],)).fetchone()
                if not auth.password_matches(body.get('old_password'),row['password_hash']):raise ValueError('原密码不正确')
                db.execute('UPDATE auth_users SET password_hash=? WHERE id=?',(auth.password_hash(body.get('password')),actor['id']))
                db.execute('DELETE FROM auth_sessions WHERE user_id=?',(actor['id'],))
                token=auth.new_session(db,actor['id']);auth.audit(db,actor,'修改本人密码')
                return finish({'ok':True},headers=[('Set-Cookie',auth.cookie('board_session',token,secure))])
            raise auth.AccessError('接口不存在',404)

    def do_POST(self):
        try:
            return self.post_response()
        except transient_errors() as error:
            return self.database_failure(error)

    def _save_phone_card(self, db, actor, body):
        if body.get('id') and 'card_manage' not in actor['permissions']:
            old_card=db.execute('SELECT * FROM phone_cards WHERE id=?',(body['id'],)).fetchone()
            body={**dict(old_card),**{key:body[key] for key in auth.CARD_TASK_FIELDS if key in body}}
        return {'ok':True,'id':phone_cards.save(db,body,actor=actor)}

    def _save_account_status(self, db, body):
        if body.get('status') not in STATES:raise ValueError('账号状态不正确')
        ident=str(body.get('id',''))
        if not db.execute('SELECT 1 FROM accounts WHERE id=?',(ident,)).fetchone():raise ValueError('账号不存在')
        db.execute("UPDATE accounts SET nurture_started=CASE WHEN ?='养号7天' AND status<>'养号7天' THEN '' ELSE nurture_started END,status=? WHERE id=?",(body['status'],body['status'],ident))
        return {'ok':True,'id':ident}

    def _idempotent_failure(self, db, actor, body, status, message):
        db.execute('ROLLBACK TO SAVEPOINT idem_business')
        if hasattr(self, '_version_delta'):
            del self._version_delta
        idempotency.complete(db, actor, body, 'failed', status, {'error': message})
        db.commit()

    def _claim_idempotent(self, db, actor, path, body):
        outcome = idempotency.claim(db, actor, path, body)
        if outcome[0] == 'expired':
            db.commit()
            raise idempotency.IdempotencyError('RESULT_EXPIRED', '该请求的结果已超过保留期，不能再次执行')
        return outcome

    def idempotent_create(self, db, actor, path, body, perform, integrity_message='保存未能完成，请核对后重试', recheck=None, record_operations=True):
        outcome = self._claim_idempotent(db, actor, path, body)
        if outcome[0] == 'replay':
            db.rollback()
            return self.reply(outcome[2], outcome[1])
        if outcome[0] == 'reserved':
            db.commit()
            begin_write(db)
            actor = auth.session(db, self.headers.get('Cookie'))
            auth.require(actor)
            if not auth.hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), actor['csrf']):
                raise auth.AccessError('页面已过期，请刷新后重试')
            if recheck:recheck(db, actor)
            else:auth.authorize_route(db, actor, path, body)
            outcome = self._claim_idempotent(db, actor, path, body)
            if outcome[0] == 'replay':
                db.rollback()
                return self.reply(outcome[2], outcome[1])
            if outcome[0] != 'continue':
                raise idempotency.IdempotencyError('REQUEST_ID_REUSED', '请求标识已结束，不能重复写入')
        db.execute('SAVEPOINT idem_business')
        try:
            change_state = operations.before(db, path, body) if record_operations else None
            result = perform(db, actor)
            if record_operations:
                operations.finish(self, db, actor, path, change_state)
                auth.audit(db, actor, path, str(result.get('id', '')))
            if isinstance(result, dict) and hasattr(self, '_version_delta'):
                result = {**result, '_record_versions': self._version_delta}
            idempotency.complete(db, actor, body, 'succeeded', 200, result)
            db.execute('RELEASE SAVEPOINT idem_business')
            db.commit()
            return self.reply(result)
        except AdmissionRejected:
            raise
        except idempotency.IdempotencyError:
            raise
        except integrity_errors():
            self._idempotent_failure(db, actor, body, 400, integrity_message)
            return self.reply({'error': integrity_message}, 400)
        except (operations.ConflictError, publishing_layout.ConflictError):
            raise
        except auth.AccessError:
            raise
        except (ValueError, KeyError, TypeError) as error:
            self._idempotent_failure(db, actor, body, 400, str(error))
            return self.reply({'error': str(error)}, 400)

    def post_response(self):
        if urlparse(self.path).path.startswith('/api/collection/'):
            return self.reply({'error': COLLECTION_MESSAGE, 'code': 'COLLECTION_DISABLED'}, 503)
        # Same-origin writes only; local services must not accept cross-site form submissions.
        origin = self.headers.get('Origin')
        configured_origin=os.environ.get('DOUYIN_BOARD_PUBLIC_ORIGIN','').rstrip('/')
        expected_origin=configured_origin or 'http://' + self.headers.get('Host','')
        if origin and origin != expected_origin:
            return self.reply({'error':'请求来源不正确'}, 403)
        if not self.headers.get('Content-Type', '').startswith('application/json'):
            return self.reply({'error':'需要 JSON 请求'}, 400)
        try:
            size = int(self.headers.get('Content-Length',0))
            if not 0 < size <= (12*1024*1024 if self.path in ['/api/workspace/import-preview','/api/workspace/nurture/upload'] else 100000):
                raise ValueError('请求过大或为空')
            try:
                raw = self.rfile.read(size)
            except TimeoutError:
                self.close_connection = True
                return self.reply({'error':'请求读取超时','code':'READ_TIMEOUT'},408)
            body = json.loads(raw)
            if not isinstance(body,dict):raise ValueError('请求内容应为对象')
            self._failure_request_id=body.get('request_id')
            if self.path.startswith('/api/auth/'):
                return self.auth_post(body)
            with connect() as db:
                # Wait for write admission before reading sessions or permissions.
                begin_write(db)
                actor=None
                if self.path=='/api/collection/heartbeat':
                    if not self.is_worker():return self.reply({'error':'仅采集服务可调用'},403)
                    return self.reply(collection.heartbeat(db,body.get('status')))
                if self.path!='/api/collection/result':
                    actor=auth.session(db,self.headers.get('Cookie'));auth.require(actor)
                    if not auth.hmac.compare_digest(self.headers.get('X-CSRF-Token',''),actor['csrf']):raise auth.AccessError('页面已过期，请刷新后重试')
                    auth.authorize_route(db,actor,self.path,body)
                if self.path=='/api/account/create' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'新增账号必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/phone-card' and not body.get('id') and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'新增手机卡必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/phone-card' and body.get('id') and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'修改手机卡必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/phone-device/create' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'新增手机设备必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/phone-device' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'修改手机设备必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/publishing/assignment' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'调整实际发布位置必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/publishing/link' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'关联发布格子必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/account/phone-device' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'调整登录手机必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/account/card-readiness' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'修改稳定或改密记录必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/lifecycle-status' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'修改小号注册进度必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/bulk-status' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'批量修改账号状态必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/batch-edit' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'批量修改业务资料必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/workspace/import-apply' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    preview=operations.take_preview(db,actor,body.get('preview_id'),'import')
                    selected=body.get('rows')
                    if isinstance(selected,list) and selected and all(type(value) is int for value in selected):
                        chosen=[item for item in preview['plans'] if item['row'] in set(selected)]
                        if len(chosen)==len(set(selected)):
                            for item in chosen:
                                if item['action'] not in ['create','update']:continue
                                kind=preview['kind']
                                if kind=='accounts':route='/api/account/create' if item['create'] else '/api/account'
                                elif kind=='cards':route='/api/phone-card'
                                elif kind=='devices':route='/api/phone-device/create' if item['create'] else '/api/phone-device'
                                else:continue
                                auth.authorize_route(db,actor,route,item['payload'])
                    db.rollback()
                    return self.reply({'error':'导入确认的资料必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/phone-cards/import' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'补录手机卡必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/account' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'保存账号必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/account/patch' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'修改账号资料必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/account/status' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'修改账号状态必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/daily' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'记录当天发布必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/account/nurture' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'修改养号开始日期必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/workspace/nurture/upload' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'保存养号截图必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/workspace/nurture/join' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'加入养号必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/workspace/nurture/start' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'确认开始养号必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/workspace/nurture/verification' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'记录核对情况必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/workspace/nurture/pause' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'暂停养号必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/workspace/nurture/resume' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'继续养号必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/workspace/nurture/finish' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'结束养号并转正常必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/workspace/nurture/delete-image' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'移除养号截图必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/workspace/nurture/purge' and getattr(db,'restore_generation',None) and not body.get('request_id'):
                    db.rollback()
                    return self.reply({'error':'清理养号截图必须带请求标识','code':'REQUEST_ID_REQUIRED'},400)
                if self.path=='/api/workspace/export':
                    try:result=workspace_service.post(db,actor,self.path,body,DB,today())
                    except workspace_service.ExportTimeout:
                        return self._export_failure(db,body,'EXPORT_TIMEOUT','导出超时，请稍后重试或改用 CSV')
                    except workspace_service.ExportFileError:
                        return self._export_failure(db,body,'FILE_FAILED','导出文件未能保存，请稍后重试或改用 CSV')
                    auth.audit(db,actor,'导出业务资料',str(body.get('kind')))
                    db.commit()
                    return self.reply(result)
                if body.get('request_id') and getattr(db,'restore_generation',None):
                    if self.path=='/api/account/create':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:{'ok':True,'id':account_service.save(database,body,current,today(),create=True)},'这个电脑位置已经有账号，请选择空位')
                    if self.path=='/api/account':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:{'ok':True,'id':account_service.save(database,body,current,today(),create=False)},'这个电脑位置已经有账号，请选择空位')
                    if self.path=='/api/account/nurture':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:workspace_service.post(database,current,self.path,body,DB,today()))
                    if self.path=='/api/account/patch':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:{'ok':True,'id':account_service.patch(database,body,current,today())},'这个电脑位置已经有账号，请选择空位')
                    if self.path=='/api/account/status':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:self._save_account_status(database,body))
                    if self.path=='/api/phone-device/create':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:{'ok':True,'id':phone_devices.save(database,body,create=True,actor=current)})
                    if self.path=='/api/phone-device':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:{'ok':True,'id':phone_devices.save(database,body,create=False,actor=current)})
                    if self.path=='/api/phone-card':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:self._save_phone_card(database,current,body))
                    if self.path=='/api/workspace/import-apply':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:workspace_service.post(database,current,self.path,body,DB,today()))
                    if self.path=='/api/workspace/nurture/upload':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:workspace_service.post(database,current,self.path,body,DB,today()))
                    if self.path=='/api/workspace/nurture/join':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:workspace_service.post(database,current,self.path,body,DB,today()))
                    if self.path=='/api/workspace/nurture/start':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:workspace_service.post(database,current,self.path,body,DB,today()))
                    if self.path=='/api/workspace/nurture/verification':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:workspace_service.post(database,current,self.path,body,DB,today()))
                    if self.path=='/api/workspace/nurture/pause':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:workspace_service.post(database,current,self.path,body,DB,today()))
                    if self.path=='/api/workspace/nurture/resume':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:workspace_service.post(database,current,self.path,body,DB,today()))
                    if self.path=='/api/workspace/nurture/finish':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:workspace_service.post(database,current,self.path,body,DB,today()))
                    if self.path=='/api/workspace/nurture/delete-image':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:workspace_service.post(database,current,self.path,body,DB,today()))
                    if self.path=='/api/workspace/nurture/purge':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:workspace_service.post(database,current,self.path,body,DB,today()))
                    if self.path=='/api/publishing/assignment':
                        return self.idempotent_create(db,actor,self.path,body,lambda database,current:workspace_service.post(database,current,self.path,body,DB,today()))
                    if self.path=='/api/publishing/link':
                        def link_once(database,current):
                            result=publishing_layout.link(database,body,current['id'])
                            auth.audit(database,current,'调整发布格子关联',json.dumps(dict(computer=body.get('computer_id'),slot=body.get('slot'),**result),ensure_ascii=False))
                            return {'ok':True,**result}
                        return self.idempotent_create(db,actor,self.path,body,link_once)
                    if self.path=='/api/account/phone-device':
                        def assign_phone(database,current):
                            phone_devices.assign(database,body['id'],body.get('phone_device_id'))
                            return {'ok':True}
                        return self.idempotent_create(db,actor,self.path,body,assign_phone)
                    if self.path=='/api/account/card-readiness':
                        def save_readiness(database,current):
                            phone_cards.update_readiness(database,body['id'],body)
                            return {'ok':True}
                        return self.idempotent_create(db,actor,self.path,body,save_readiness)
                    if self.path=='/api/lifecycle-status':
                        def save_lifecycle(database,current):
                            account_id=str(body['id'])
                            old=database.execute('SELECT * FROM accounts WHERE id=?',(account_id,)).fetchone()
                            if not old or old['account_type']!='大号':
                                raise ValueError('请先在详情中确认账号类型为大号')
                            values=account_lifecycle.validate(database,account_id,{'auxiliary_state':body['auxiliary_state']},old,today(),actor=current)
                            database.execute('UPDATE accounts SET auxiliary_state=? WHERE id=?',(values[3],account_id))
                            return {'ok':True}
                        return self.idempotent_create(db,actor,self.path,body,save_lifecycle)
                    if self.path=='/api/daily':
                        def save_daily(database,current):
                            day=body['date']
                            date.fromisoformat(day)
                            if type(body['published']) is not bool:raise ValueError('发布标记应为布尔值')
                            if not database.execute('SELECT 1 FROM accounts WHERE id=?',(str(body['id']),)).fetchone():raise ValueError('账号不存在')
                            database.execute('INSERT INTO daily VALUES(?,?,?) ON CONFLICT(account_id,day) DO UPDATE SET published=excluded.published',(str(body['id']),day,int(body['published'])))
                            return {'ok':True}
                        return self.idempotent_create(db,actor,self.path,body,save_daily)
                    if self.path=='/api/bulk-status':
                        def save_bulk(database,current):
                            if body['status'] not in STATES or not isinstance(body['ids'],list) or not body['ids']:raise ValueError('请选择账号及状态')
                            database.executemany("UPDATE accounts SET nurture_started=CASE WHEN ?='养号7天' AND status<>'养号7天' THEN '' ELSE nurture_started END,status=? WHERE id=?",[(body['status'],body['status'],str(i)) for i in body['ids']])
                            return {'ok':True}
                        return self.idempotent_create(db,actor,self.path,body,save_bulk)
                    if self.path=='/api/batch-edit':
                        def save_batch(database,current):
                            import batch_edit
                            return batch_edit.apply(database,current,body,today())
                        return self.idempotent_create(db,actor,self.path,body,save_batch)
                    if self.path=='/api/phone-cards/import':
                        def import_cards(database,current):
                            return {'ok':True,'added':phone_cards.import_accounts(database,actor=current,body=body)}
                        return self.idempotent_create(db,actor,self.path,body,import_cards)
                change_state=operations.before(db,self.path,body)
                if self.path=='/api/batch-edit':
                    import batch_edit
                    result=batch_edit.apply(db,actor,body,today())
                    operations.finish(self,db,actor,self.path,change_state)
                    auth.audit(db,actor,'批量修改',str(body.get('kind'))+':'+str(result['count']))
                    db.commit()
                    return self.reply(result)
                if self.path.startswith('/api/workspace/') or self.path in ['/api/publishing/assignment','/api/account/nurture']:
                    result=workspace_service.post(db,actor,self.path,body,DB,today())
                    operations.finish(self,db,actor,self.path,change_state)
                    auth.audit(db,actor,self.path,str(body.get('id',body.get('backup_id',''))))
                    db.commit()
                    return self.reply(result)
                elif self.path=='/api/publishing/link':
                    result=publishing_layout.link(db,body,actor['id'])
                    auth.audit(db,actor,'调整发布格子关联',json.dumps(dict(computer=body.get('computer_id'),slot=body.get('slot'),**result),ensure_ascii=False))
                    operations.finish(self,db,actor,self.path,change_state)
                    db.commit()
                    return self.reply({'ok':True,**result})
                elif self.path=='/api/account/patch':
                    account_service.patch(db,body,actor,today())
                elif self.path=='/api/account/status':
                    self._save_account_status(db,body)
                elif self.path in ['/api/phone-device','/api/phone-device/create']:
                    phone_devices.save(db,body,create=self.path.endswith('/create'),actor=actor)
                elif self.path == '/api/account/phone-device':
                    phone_devices.assign(db,body['id'],body.get('phone_device_id'))
                elif self.path == '/api/account/card-readiness':
                    phone_cards.update_readiness(db,body['id'],body)
                elif self.path == '/api/phone-card':
                    card_id=self._save_phone_card(db,actor,body)['id']
                    auth.audit(db,actor,self.path,body.get('id',body.get('account_id','')))
                    operations.finish(self,db,actor,self.path,change_state)
                    db.commit()
                    return self.reply({'ok':True,'id':card_id})
                elif self.path == '/api/phone-cards/import':
                    added=phone_cards.import_accounts(db,actor=actor,body=body)
                    auth.audit(db,actor,self.path,body.get('id',body.get('account_id','')))
                    operations.finish(self,db,actor,self.path,change_state)
                    db.commit()
                    return self.reply({'ok':True,'added':added})
                elif self.path == '/api/collection/result':
                    if not self.is_worker():
                        return self.reply({'error':'仅桌面采集服务可调用'},403)
                    result=collection.complete(db,body)
                    auth.audit(db,actor,self.path,body.get('id',body.get('account_id','')))
                    operations.finish(self,db,actor,self.path,change_state)
                    db.commit()
                    return self.reply(result)
                elif self.path == '/api/collection/enqueue':
                    ids=body.get('ids')
                    if not isinstance(ids,list) or not 0<len(ids)<=2000:
                        raise ValueError('请选择1至2000个账号')
                    result=collection.enqueue(db,ids)
                    auth.audit(db,actor,self.path,body.get('id',body.get('account_id','')))
                    operations.finish(self,db,actor,self.path,change_state)
                    db.commit()
                    return self.reply(result)
                elif self.path == '/api/collection/settings':
                    if body.get('pause') is True:
                        db.execute("UPDATE collection_settings SET paused=1,pause_reason='已手动暂停，当前采集完成后停止' WHERE id=1")
                    if body.get('cancel_waiting') is True:
                        db.execute("UPDATE accounts SET collection_state='已取消',collection_error='' WHERE id IN (SELECT account_id FROM collection_jobs WHERE state='queued')")
                        db.execute("UPDATE collection_jobs SET state='cancelled' WHERE state='queued'")
                    if body.get('open_browser') is True:
                        if not collection.overview(db)['worker_online']:raise ValueError('采集器离线，请在服务器电脑登录 Windows 并启动采集器')
                        db.execute("UPDATE collection_worker SET command='open' WHERE id=1")
                    if 'auto_enabled' in body:
                        if type(body['auto_enabled']) is not bool:
                            raise ValueError('自动采集开关不正确')
                        db.execute('UPDATE collection_settings SET auto_enabled=? WHERE id=1',(int(body['auto_enabled']),))
                    if body.get('resume') is True:
                        db.execute("UPDATE collection_worker SET command='resume' WHERE id=1")
                        db.execute("UPDATE collection_settings SET paused=0,pause_reason='' WHERE id=1")
                        retry=[r[0] for r in db.execute("SELECT id FROM accounts WHERE collection_state='需要验证'")]
                        if retry:
                            collection.enqueue(db,retry)
                elif self.path == '/api/lifecycle-status':
                    account_id=str(body['id'])
                    old=db.execute('SELECT * FROM accounts WHERE id=?',(account_id,)).fetchone()
                    if not old or old['account_type']!='大号':
                        raise ValueError('请先在详情中确认账号类型为大号')
                    values=account_lifecycle.validate(db,account_id,{'auxiliary_state':body['auxiliary_state']},old,today(),actor=actor)
                    db.execute('UPDATE accounts SET auxiliary_state=? WHERE id=?',(values[3],account_id))
                elif self.path == '/api/daily':
                    day = body['date']; date.fromisoformat(day)
                    if type(body['published']) is not bool:
                        raise ValueError('发布标记应为布尔值')
                    if not db.execute('SELECT 1 FROM accounts WHERE id=?',(str(body['id']),)).fetchone():
                        raise ValueError('账号不存在')
                    db.execute('INSERT INTO daily VALUES(?,?,?) ON CONFLICT(account_id,day) DO UPDATE SET published=excluded.published',(str(body['id']),day,int(body['published'])))
                elif self.path in ['/api/account', '/api/account/create']:
                    account_service.save(db,body,actor,today(),create=self.path.endswith('/create'))
                elif self.path == '/api/bulk-status':
                    if body['status'] not in STATES or not isinstance(body['ids'],list) or not body['ids']:
                        raise ValueError('请选择账号及状态')
                    db.executemany("UPDATE accounts SET nurture_started=CASE WHEN ?='养号7天' AND status<>'养号7天' THEN '' ELSE nurture_started END,status=? WHERE id=?",[(body['status'],body['status'],str(i)) for i in body['ids']])
                else:
                    return self.reply({'error':'接口不存在'},404)
                operations.finish(self,db,actor,self.path,change_state)
                auth.audit(db,actor,self.path,body.get('id',','.join(map(str,body.get('ids',[])))[:200]))
            self.reply({'ok':True})
        except AdmissionRejected as e:
            status=409 if e.code in ('WRITER_VERSION_RETIRED','RESTORE_GENERATION_EXPIRED') else 503
            self.reply({'error':'当前运行状态不允许写入，请刷新后重试','code':e.code},status)
        except idempotency.IdempotencyError as error:
            self.reply({'error':str(error),'code':error.code},error.status)
        except (operations.ConflictError,publishing_layout.ConflictError) as e:
            self.reply({'error':str(e)},409)
        except auth.AccessError as e:
            self.reply({'error':str(e)},e.status)
        except integrity_errors():
            self.reply({'error':'这个电脑位置已经有账号，请选择空位'},400)
        except (ValueError,KeyError,TypeError) as e:
            self.reply({'error':str(e)},400)

if __name__ == '__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8776); args=parser.parse_args()
    initialize()
    backup.start(DB)
    print(f'账号看板已启动 http://127.0.0.1:{args.port}',flush=True)
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
