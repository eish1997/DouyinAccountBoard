"""Previewed account/card/device imports; empty cells never erase existing values."""
import base64
import csv
import io
import json
import re
from sql_dialect import integrity_errors
import zipfile
import xml.etree.ElementTree as ET
import auth
import account_service
import account_lifecycle
import phone_cards
import phone_devices
import operations

FIELDS={
 'accounts':{'内部编号':'id','昵称':'name','抖音号':'douyin_id','注册手机号':'registration_phone','账号状态':'status','账号类型':'account_type','关联大号编号':'parent_account_id','注册日期':'registered_on','小号注册状态':'auxiliary_state','稳定状态':'card_stability','改密状态':'password_state','养号开始日期':'nurture_started','备注':'note'},
 'cards':{'内部编号':'id','手机卡号码':'phone','卡名称':'label','卡主':'owner','运营商':'carrier','卡状态':'status','卡位置':'location','卡归属':'ownership','负责人':'keeper','接码联系人':'code_contact','接码状态':'code_state','接码事项':'code_note','归还状态':'return_state','预计归还日期':'return_due','实际归还日期':'returned_on','到期日期':'expires_on','备注':'note'},
 'devices':{'手机序号':'id','手机名称':'name','安排状态':'state','负责人':'owner','设备位置':'location','备注':'note'}}
TABLES={'accounts':'accounts','cards':'phone_cards','devices':'phone_devices'}


def read_file(filename,content):
    try:return _read_file(filename,content)
    except (zipfile.BadZipFile,ET.ParseError,IndexError,KeyError,AttributeError,UnicodeError) as error:raise ValueError('表格损坏或格式不支持，请重新另存为.xlsx或UTF-8 CSV') from error


def _read_file(filename,content):
    try:raw=base64.b64decode(content,validate=True)
    except Exception:raise ValueError('文件内容无效')
    if len(raw)>8*1024*1024:raise ValueError('文件不能超过8MB')
    if filename.lower().endswith('.csv'):
        try:text=raw.decode('utf-8-sig')
        except UnicodeDecodeError:text=raw.decode('gb18030')
        rows=list(csv.reader(io.StringIO(text)))
    elif filename.lower().endswith('.xlsx'):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if sum(info.file_size for info in archive.infolist())>40*1024*1024:raise ValueError('表格解压后过大')
            ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            strings=[]
            if 'xl/sharedStrings.xml' in archive.namelist():
                strings=[''.join(item.itertext()) for item in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('m:si',ns)]
            sheets=sorted(name for name in archive.namelist() if re.fullmatch(r'xl/worksheets/sheet\d+\.xml',name))
            if len(sheets)!=1:raise ValueError('请仅保留一张需要导入的工作表，再上传')
            root=ET.fromstring(archive.read(sheets[0]));rows=[]
            for row in root.findall('.//m:sheetData/m:row',ns):
                cells={}
                for cell in row.findall('m:c',ns):
                    if cell.find('m:f',ns) is not None:raise ValueError('导入不接受公式，请先粘贴为值')
                    index=0
                    for letter in re.sub(r'\d','',cell.get('r','A')):index=index*26+ord(letter)-64
                    if index>100:raise ValueError('表格列数过多')
                    value=cell.find('m:v',ns);value=value.text or '' if value is not None else ''
                    if cell.get('t')=='s':value=strings[int(value)]
                    if cell.get('t')=='inlineStr':value=''.join(cell.find('m:is',ns).itertext())
                    cells[index-1]=value
                rows.append([cells.get(i,'') for i in range(max(cells,default=-1)+1)])
    else:raise ValueError('请选择.xlsx或.csv文件，旧版.xls请另存为.xlsx')
    if not rows or len(rows)>5001:raise ValueError('表格需有标题行，最多5000条资料')
    return rows


def table_rows(db,kind,ids=None,template=False,day='',actor=None):
    if kind not in FIELDS:raise ValueError('导出类型不正确')
    context=None
    if getattr(db,'scope_enabled',False):
        import office_scope
        context=office_scope.read_context(db,actor)
        allowed={str(ident) for ident in context['ids'][kind]}
        if ids is not None and not ids<=allowed:raise auth.AccessError('导出包含无权访问的资料',403)
        ids=allowed if ids is None else ids
    headers=list(FIELDS[kind]);values=[]
    if kind=='accounts' and not template:headers+=['登录手机序号','资料登记电脑','实际发布位置','公开作品数量','上次成功采集','所选日期发布']
    if not template:
        for row in db.execute('SELECT * FROM '+TABLES[kind]):
            if ids is None or str(row['id']) in ids:
                row=dict(row)
                if context is not None and not context['global_access'] and kind=='accounts':
                    if row['parent_account_id'] not in context['ids']['accounts']:row['parent_account_id']=None
                    if row['phone_device_id'] not in context['ids']['devices']:row['phone_device_id']=None
                    if row['computer_id'] not in context['ids']['computers']:row['computer_id']=None
                value=[str(row[key] or '') for key in FIELDS[kind].values()]
                if kind=='accounts':
                    computer=db.execute('SELECT name FROM computers WHERE id=?',(row['computer_id'],)).fetchone()
                    positions=db.execute('SELECT c.name,p.slot,p.source_cell,p.computer_id FROM publishing_layout p JOIN computers c ON c.id=p.computer_id WHERE p.account_id=? AND p.batch=(SELECT batch FROM publishing_layout_state WHERE id=1)',(row['id'],)).fetchall()
                    if context is not None and not context['global_access']:positions=[p for p in positions if p[3] in context['ids']['computers']]
                    daily=db.execute('SELECT published FROM daily WHERE account_id=? AND day=?',(row['id'],day)).fetchone()
                    value+=[row['phone_device_id'] or '',computer[0] if computer else '', '；'.join(str(p[0])+'/设备'+str(p[1])+'/'+str(p[2]) for p in positions),str(row['work_count']) if row['work_count'] is not None else '',row['last_collected'] or '',(day+' · ' if day else '')+('已发' if daily and daily[0] else '未发')]
                values.append(value)
    return [headers,*values]


def save(db,kind,payload,actor,day,create):
    if kind=='accounts':
        auth.authorize_route(db,actor,'/api/account/create' if create else '/api/account',payload)
        if not create:auth.authorize_account_fields(actor,payload,db.execute('SELECT * FROM accounts WHERE id=?',(payload['id'],)).fetchone())
        started=payload.pop('nurture_started','')
        if started:
            from datetime import date
            date.fromisoformat(started)
            if started>day:raise ValueError('养号开始日期不能在未来')
            old_started=db.execute('SELECT nurture_started FROM accounts WHERE id=?',(str(payload.get('id','')),)).fetchone()
            if not old_started or old_started[0]!=started:auth.require(actor,'account_status')
        ident=account_service.save(db,payload,actor,day,create)
        if started:db.execute('UPDATE accounts SET nurture_started=? WHERE id=?',(started,ident))
    elif kind=='cards':
        auth.authorize_route(db,actor,'/api/phone-card',payload);phone_cards.save(db,payload,actor=actor)
    else:
        auth.authorize_route(db,actor,'/api/phone-device/create' if create else '/api/phone-device',payload);phone_devices.save(db,payload,create=create,actor=actor)


def plan_import(db,actor,kind,rows,day,new_scope=None):
    if kind not in FIELDS:raise ValueError('导入类型不正确')
    headers=[str(value).strip() for value in rows[0]]
    if len(set(headers))!=len(headers):raise ValueError('表头存在重复列')
    mapping=FIELDS[kind];known=[(i,mapping[name]) for i,name in enumerate(headers) if name in mapping]
    if not known:raise ValueError('没有识别到列，请下载本系统模板')
    plans=[];seen={};current=operations.snapshot(db)
    for number,row in enumerate(rows[1:],2):
        if not any(str(value).strip() for value in row):continue
        values={key:str(row[i]).strip() for i,key in known if i<len(row) and str(row[i]).strip()}
        item=dict(row=number,action='error',error='',changes=[],selected=False)
        try:
            table=TABLES[kind];old=None
            if kind=='accounts':
                if values.get('id'):
                    old=db.execute('SELECT * FROM accounts WHERE id=?',(values['id'],)).fetchone()
                    if not old:raise ValueError('内部编号不存在；新增账号请留空内部编号')
                elif values.get('douyin_id'):
                    candidates=db.execute('SELECT * FROM accounts WHERE douyin_id=?',(values['douyin_id'],)).fetchall()
                    if len(candidates)>1:raise ValueError('该抖音号对应多条记录，需明确内部编号')
                    old=candidates[0] if candidates else None
                else:raise ValueError('需填写内部编号或真实抖音号；不按昵称猜测身份')
                identity='id:'+str(old['id']) if old else 'douyin:'+values['douyin_id']
                payload={**dict(old)} if old else dict(name='',computer_id='unassigned',slot=1,status='待确认',account_type='待确认')
                payload.update(values);payload['slot']=payload.get('slot') or 1
                payload.pop('phone_device_id',None)
                if 'nurture_started' not in values:payload.pop('nurture_started',None)
                if old:payload['id']=old['id']
            elif kind=='cards':
                if not phone_cards.valid_phone(values.get('phone','')):raise ValueError('需填写完整11位手机卡号码')
                old=db.execute('SELECT * FROM phone_cards WHERE phone=?',(values['phone'],)).fetchone()
                if values.get('id') and (not old or str(old['id'])!=values['id']):raise ValueError('内部编号与手机卡号码不一致')
                identity='phone:'+values['phone'];payload={**dict(old),**values} if old else values.copy()
                payload['id']=old['id'] if old else None
            else:
                ident=values.get('id','')
                if not re.fullmatch(r'(?:office3:)?[1-9]\d{0,5}',ident):raise ValueError('手机序号格式错误；三办使用office3:序号')
                old=db.execute('SELECT * FROM phone_devices WHERE id=?',(ident,)).fetchone()
                identity='device:'+ident;payload={**dict(old),**values} if old else values.copy()
                if old and old['state_source']=='auto' and ('state' not in values or values['state']==old['state']):payload['state']='auto'
            if getattr(db,'scope_enabled',False):
                import office_scope
                if old:office_scope.require_entity(db,actor,kind,old['id'],write=True)
                else:
                    offices=office_scope.requested_offices(db,actor,kind,new_scope or {})
                    payload.update({'office_id':offices[0] if offices else None} if kind=='accounts' else {'office_ids':offices})
            if identity in seen:
                plans[seen[identity]]['action']='error';plans[seen[identity]]['error']='同一文件重复记录，请合并后重试'
                raise ValueError('同一文件重复记录，请合并后重试')
            seen[identity]=len(plans)
            item.update(identity=identity,entity=operations.row_key(table,dict(old)) if old else '',expected=operations.token(current[operations.row_key(table,dict(old))]) if old else None,payload=payload,create=not bool(old),
                        action='update' if old else 'create',changes=[dict(field=key,before=old[key] if old and key in old.keys() else '',after=value) for key,value in values.items() if key!='id' and (not old or str(old[key] or '')!=value)])
            if old and not item['changes']:item['action']='same'
            # Reuse the production validators in a rollback-only savepoint.
            db.execute('SAVEPOINT import_validation')
            try:save(db,kind,payload.copy(),actor,day,not bool(old))
            finally:db.execute('ROLLBACK TO import_validation');db.execute('RELEASE import_validation')
            item['selected']=not bool(old)
        except auth.AccessError:
            # Do not return identity, old values or a stored payload after a
            # relation/field authorization failure discovered during validation.
            item=dict(row=number,action='error',error='无权导入此资料或关联对象',changes=[],selected=False)
        except integrity_errors(db):item['action']='error';item['error']='数据约束不满足，请核对必填项、唯一值及关联资料'
        except (ValueError,KeyError,TypeError) as error:item['action']='error';item['error']=str(error)
        plans.append(item)
    preview_id=operations.preview(db,actor,'import',dict(kind=kind,plans=plans))
    return dict(preview_id=preview_id,rows=[{key:value for key,value in item.items() if key not in ['payload','expected']} for item in plans],ignored_columns=[name for name in headers if name not in mapping],note='空白保留原值；不导入发布安排、不删除资料，已有记录默认不覆盖，请逐行选择确认。')


def apply_import(db,actor,body,day):
    preview=operations.take_preview(db,actor,body.get('preview_id'),'import');plans=preview['plans']
    selected=body.get('rows')
    if not isinstance(selected,list) or not selected or any(type(value) is not int for value in selected):raise ValueError('请勾选要导入的行')
    chosen=[item for item in plans if item['row'] in set(selected)]
    if len(chosen)!=len(set(selected)):raise ValueError('选择的行不正确')
    state=operations.snapshot(db)
    for item in chosen:
        if item['action'] not in ['create','update']:raise ValueError('不能导入错误行或无变化行')
        if item['entity'] and operations.token(state.get(item['entity']))!=item['expected']:raise operations.ConflictError('第'+str(item['row'])+'行已被修改，请重新预览')
        save(db,preview['kind'],item['payload'].copy(),actor,day,item['create'])
    db.execute('DELETE FROM operations_previews WHERE id=?',(body['preview_id'],))
    return dict(imported=len(chosen))
