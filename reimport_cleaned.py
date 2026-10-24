"""Reconcile cleaned snapshots by stable identity, preserving daily/auth/local evidence."""
import json,re,sqlite3
from pathlib import Path
from collections import Counter,defaultdict
from datetime import date
import phone_devices,phone_cards,account_lifecycle,import_phone_cards
import audit_publishing_matrix as audit
import import_publishing_layout
import nurture

FOLDER=Path('E:/Downloads/DouyinReimport-20261019')
def text(value):return re.sub(r'\s+','',value or '')
def parsed_date(value):
    value=value.strip()
    if not value:return ''
    nums=re.findall(r'\d+',value)
    if len(nums)!=3:raise ValueError('日期格式不明确')
    result=date(*map(int,nums)).isoformat()
    if result>date.today().isoformat():raise ValueError('注册日期晚于今天')
    return result

def apply(db,folder=FOLDER):
    report=dict(updated=0,added=0,skipped=[],warnings=[],cards_added=0,manual_kept=0)
    touched=set()
    for group in ['main','three']:
        rows=json.loads((folder/(group+'-accounts.json')).read_text(encoding='utf-8'))
        assert rows[0][2]=='注册抖音用的手机号' and rows[0][7]=='抖音号'
        counts=Counter(r[7].strip() for r in rows[1:] if len(r)>7 and r[7].strip())
        for number,raw in enumerate(rows[1:],2):
            r=[str(v).strip() for v in raw]+['']*22
            if not r[5] and not r[7]:continue
            handle=r[7] if re.fullmatch(r'[A-Za-z0-9_.-]{2,100}',r[7]) else ''
            candidates=db.execute("SELECT * FROM accounts WHERE douyin_id=? AND note NOT LIKE '%演示采集%'",(handle,)).fetchall() if handle else []
            candidates=[a for a in candidates if (a['office_group']=='three' or str(a['phone_device_id'] or '').startswith('office3:'))==(group=='three')]
            if not handle or counts[r[7]]>1 or len(candidates)>1:
                report['skipped'].append(dict(group=group,row=number,reason='抖音号缺失、无效或不唯一'));continue
            old=dict(candidates[0]) if candidates else None
            if old and old['id'] in touched:
                report['skipped'].append(dict(group=group,row=number,reason='身份重复'));continue
            try:registered=parsed_date(r[19 if group=='main' else 18])
            except ValueError:
                registered='';report['warnings'].append(dict(group=group,row=number,reason='注册日期待核对，保留系统日期'))
            status_index=16 if group=='main' else 15
            raw_state=r[status_index]
            state={'正常':'正常','账号永久封禁':'封禁','永久封禁':'封禁','封禁':'封禁','账号短期封禁':'短期封禁','短期封禁':'短期封禁','养号7天':'养号7天','未使用':'停用','停用':'停用','发不了':'发不了','待确认':'待确认'}.get(raw_state)
            if raw_state=='未使用' and registered:state='养号7天'
            if raw_state and not state:report['warnings'].append(dict(group=group,row=number,reason='账号状态未识别，保留系统状态'))
            ident=old['id'] if old else str(db.execute('SELECT max(CAST(id AS INTEGER))+1 FROM accounts').fetchone()[0])
            if not old:
                db.execute("INSERT OR IGNORE INTO computers VALUES('unassigned','未分配电脑','','','','')")
                db.execute("INSERT INTO accounts(id,computer_id,slot,name,status,note,douyin_id,homepage,last_collected,office_group) VALUES(?,'unassigned',NULL,?,'待确认',?,?,'','','')",(ident,r[5] or '昵称待补充','腾讯文档清理后重导入：'+group+'，第'+str(number)+'行',handle))
                report['added']+=1
            else:report['updated']+=1
            touched.add(ident)
            if r[5]:db.execute('UPDATE accounts SET name=? WHERE id=?',(r[5],ident))
            if state:
                db.execute("UPDATE accounts SET nurture_started=CASE WHEN ?='养号7天' AND status<>'养号7天' THEN '' ELSE nurture_started END,status=? WHERE id=?",(state,state,ident))
            if registered:db.execute('UPDATE accounts SET registered_on=? WHERE id=?',(registered,ident))
            if phone_cards.valid_phone(r[2]):db.execute('UPDATE accounts SET registration_phone=? WHERE id=?',(r[2],ident))
            elif r[2]:report['warnings'].append(dict(group=group,row=number,reason='注册手机号不明确，保留原值'))
            role=r[9] if group=='main' else r[8]
            kind='小号' if '小号' in role and '已注册' in role else '大号' if '大号' in role else ''
            if kind:db.execute('UPDATE accounts SET account_type=? WHERE id=?',(kind,ident))
            if group=='main':
                aux='已注册' if r[8]=='已注册' else '未注册' if any(v in r[8]+role for v in ['未注册','未满30']) else '暂无法注册' if any(v in r[8]+role for v in ['无法注册','无法创建','环境异常']) else ''
                if aux:db.execute('UPDATE accounts SET auxiliary_state=? WHERE id=?',(aux,ident))
            number_phone=phone_devices.number(r[6])
            if number_phone:
                device=('office3:' if group=='three' else '')+number_phone
                if not db.execute('SELECT 1 FROM phone_devices WHERE id=?',(device,)).fetchone():phone_devices.save(db,dict(id=device,state='auto'),True)
                phone_devices.assign(db,ident,device,'cleaned-tencent-20261019')
            elif r[6]:report['warnings'].append(dict(group=group,row=number,reason='登录手机编号不明确，保留原关联'))
            db.execute('UPDATE accounts SET office_group=? WHERE id=?',('three' if group=='three' else '',ident))
            raw_computer=r[12 if group=='main' else 11]
            cid=audit.computer_number(raw_computer) if raw_computer else None
            if cid:
                cid=('office3:' if group=='three' else '')+cid
                db.execute('INSERT OR IGNORE INTO computers VALUES(?,?,?,?,?,?)',(cid,('办公室三 · ' if group=='three' else '')+cid.split(':')[-1]+'号电脑','','','',''))
                current=db.execute('SELECT computer_id,slot FROM accounts WHERE id=?',(ident,)).fetchone()
                if current['computer_id']!=cid:
                    occupied={v[0] for v in db.execute('SELECT slot FROM accounts WHERE computer_id=? AND slot IS NOT NULL',(cid,))}
                    slot=next((i for i in range(1,101) if i not in occupied),None)
                    if slot:db.execute('UPDATE accounts SET computer_id=?,slot=? WHERE id=?',(cid,slot,ident))
                    else:report['warnings'].append(dict(group=group,row=number,reason='资料电脑无空位，保留原资料登记电脑'))
            elif raw_computer:report['warnings'].append(dict(group=group,row=number,reason='资料电脑编号不明确，保留原资料登记电脑'))
        # Only unambiguous source fields fill existing blank card fields; task records stay intact.
        import csv,io
        out=io.StringIO();csv.writer(out).writerows(rows)
        temp=folder/(group+'-card-source.json');temp.write_text(json.dumps(dict(csv_data=out.getvalue()),ensure_ascii=False),encoding='utf-8')
        info=import_phone_cards.apply(db,temp);report['cards_added']+=info['added']
        card_records,card_report=import_phone_cards.prepare(temp)
        for card in card_records:
            for field in ['owner','location','code_contact','source_note']:
                if card[field]:db.execute('UPDATE phone_cards SET '+field+'=? WHERE phone=?',(card[field],card['phone']))
        if card_report['owner_conflicts']:report['warnings'].append(dict(group=group,reason='部分手机卡卡主标注仍冲突，保留核对原文',count=card_report['owner_conflicts']))
    # Preserve valid pairs; detach changed identities explicitly and report them.
    for row in db.execute('SELECT a.*,p.account_type AS parent_type,p.registration_phone AS parent_phone FROM accounts a JOIN accounts p ON p.id=a.parent_account_id').fetchall():
        if row['account_type']!='小号' or row['parent_type']!='大号' or (row['registration_phone'] and row['parent_phone'] and row['registration_phone']!=row['parent_phone']):
            db.execute('UPDATE accounts SET parent_account_id=NULL WHERE id=?',(row['id'],));report['warnings'].append(dict(id=row['id'],reason='清理后配对资料不一致，解除旧配对待核对'))
    groups=defaultdict(list)
    for row in db.execute("SELECT * FROM accounts WHERE registration_phone<>''"):groups[row['registration_phone']].append(row)
    for accounts in groups.values():
        parents=[a for a in accounts if a['account_type']=='大号'];children=[a for a in accounts if a['account_type']=='小号']
        if len(parents)==len(children)==1 and not children[0]['parent_account_id']:
            if not db.execute('SELECT 1 FROM accounts WHERE parent_account_id=?',(parents[0]['id'],)).fetchone():
                db.execute('UPDATE accounts SET parent_account_id=? WHERE id=?',(parents[0]['id'],children[0]['id']))
    db.execute("UPDATE accounts SET auxiliary_state='已注册' WHERE id IN (SELECT parent_account_id FROM accounts WHERE parent_account_id IS NOT NULL)")
    audit.FOLDER=folder;audit.analyze()
    old_manual=[dict(r) for r in db.execute('SELECT p.*,l.actor_id,l.updated_at,l.previous_reason FROM publishing_layout p JOIN publishing_layout_links l USING(batch,computer_id,slot) WHERE batch=(SELECT batch FROM publishing_layout_state WHERE id=1)')]
    layout=import_publishing_layout.apply(db,folder)
    duplicates=db.execute('SELECT account_id FROM publishing_layout WHERE batch=? AND account_id IS NOT NULL GROUP BY account_id HAVING count(*)>1',(layout['batch'],)).fetchall()
    for duplicate in duplicates:
        db.execute("UPDATE publishing_layout SET account_id=NULL,reason='同一抖音号出现在多个发布格，请确认保留哪个位置' WHERE batch=? AND account_id=?",(layout['batch'],duplicate['account_id']))
        report['warnings'].append(dict(id=duplicate['account_id'],reason='同一账号重复发布位置，已保留原格为待匹配'))
    for old in old_manual:
        new=db.execute('SELECT * FROM publishing_layout WHERE batch=? AND computer_id=? AND slot=?',(layout['batch'],old['computer_id'],old['slot'])).fetchone()
        if new and new['label']==old['label'] and (not new['account_id'] or new['account_id']==old['account_id']) and not db.execute('SELECT 1 FROM publishing_layout WHERE batch=? AND account_id=? AND NOT(computer_id=? AND slot=?)',(layout['batch'],old['account_id'],old['computer_id'],old['slot'])).fetchone():
            db.execute("UPDATE publishing_layout SET account_id=?,reason='' WHERE batch=? AND computer_id=? AND slot=?",(old['account_id'],layout['batch'],old['computer_id'],old['slot']))
            db.execute('INSERT OR IGNORE INTO publishing_layout_links VALUES(?,?,?,?,?,?)',(layout['batch'],old['computer_id'],old['slot'],old['previous_reason'],old['actor_id'],old['updated_at']));report['manual_kept']+=1
        else:report['warnings'].append(dict(computer=old['computer_id'],slot=old['slot'],reason='原手动关联的源格内容有变化，请复核新安排'))
    nurture.sync(db,date.today().isoformat())
    layout['linked']=db.execute('SELECT count(*) FROM publishing_layout WHERE batch=? AND account_id IS NOT NULL',(layout['batch'],)).fetchone()[0]
    layout['unresolved']=db.execute("SELECT count(*) FROM publishing_layout WHERE batch=? AND trim(label)<>'' AND account_id IS NULL",(layout['batch'],)).fetchone()[0]
    report['layout']=layout
    assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    assert not list(db.execute('PRAGMA foreign_key_check'))
    return report
