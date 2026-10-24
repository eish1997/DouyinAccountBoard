"""Import the separately numbered, authorized third-office Tencent snapshot."""
import argparse
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
import phone_devices

ROOT=Path(__file__).resolve().parent
SOURCE=Path('E:/Downloads/TencentDocsSecondAccountImport')
MARKER='tencent-DRWxWc1huUWFsVkZp-BB08J2-office-three-v1'
PREFIX='office3:'


def prepare(folder):
    rows=json.loads((Path(folder)/'accounts.json').read_text(encoding='utf-8'))
    matrix=json.loads((Path(folder)/'matrix.json').read_text(encoding='utf-8'))
    if rows[0][2]!='注册抖音用的手机号' or rows[0][6]!='抖音在哪个手机上' or rows[0][7]!='抖音号' or rows[0][11]!='对应电脑':
        raise ValueError('源表列结构不符合已核对映射')
    records=[]
    for row_number,raw in enumerate(rows[1:],2):
        row=[str(v).strip() for v in raw]+['']*22
        if not row[5] and not row[7]:continue
        if not re.fullmatch(r'[A-Za-z0-9_.-]{2,100}',row[7]):
            raise ValueError('第'+str(row_number)+'行抖音号不明确，请核对')
        device=phone_devices.number(row[6])
        if not device:raise ValueError('第'+str(row_number)+'行登录手机编号不明确')
        if row[11] and not re.fullmatch(r'[0-9]{1,6}',row[11]):raise ValueError('发布电脑编号不明确')
        registered=''
        if row[18]:
            match=re.fullmatch(r'(\d{4})年(\d{1,2})月(\d{1,2})日',row[18])
            if not match:raise ValueError('注册日期不明确')
            registered=date(*map(int,match.groups())).isoformat()
            if registered>date.today().isoformat():raise ValueError('注册日期晚于今天')
        phone=row[2] if re.fullmatch(r'1[0-9]{10}',row[2]) else ''
        state={'正常':'正常','未使用':'停用','账号永久封禁':'封禁','账号短期封禁':'短期封禁','养号7天':'养号7天'}.get(row[15],'待确认')
        note='\n'.join(filter(None,[
            '腾讯文档：抖音账号表 / 账号情况表，第'+str(row_number)+'行；办公室三独立批次',
            '原账号状态：'+row[15],
            '使用情况：'+row[17] if row[17] else '',
            '负责人：'+row[10] if row[10] else '',
            '类型：'+row[12] if row[12] else '',
            '违规/处理备注：'+row[16] if row[16] else '',
            '昵称待补充' if not row[5] else '',
            '注册手机号待核对：'+row[2] if row[2] and not phone else '',
        ]))
        records.append(dict(row=row_number,name=row[5] or '昵称待补充（手机'+device+'）',
                            douyin_id=row[7],device=PREFIX+device,phone=phone,
                            computer=PREFIX+str(int(row[11])) if row[11] else 'unassigned',
                            registered=registered,status=state,note=note[:2000],
                            lifecycle_note=('原小号说明：'+row[8]+'；账号类型/注册结果待核对') if row[8] else '源表未明确大小号类型，待核对'))
    handles=[a['douyin_id'] for a in records]
    if len(handles)!=len(set(handles)):raise ValueError('源表抖音号重复，停止导入')
    computers={}
    for row in matrix[1:]:
        match=re.fullmatch(r'(\d+)号(?:电脑|设备)',row[0].strip())
        if not match:continue
        cid=PREFIX+str(int(match[1]))
        computers[cid]=(cid,'办公室三 · '+row[0].strip(),row[2].strip(),row[3].strip(),row[5].strip() or row[4].strip(),row[6].strip())
    missing={a['computer'] for a in records if a['computer']!='unassigned'}-computers.keys()
    if missing:raise ValueError('工作进度表缺少对应电脑资料')
    return records,computers


def apply(db,folder=SOURCE):
    records,computers=prepare(folder)
    phone_devices.migrate(db)
    if db.execute('SELECT 1 FROM imports WHERE name=?',(MARKER,)).fetchone():
        return dict(already_imported=True,accounts_added=0)
    existing={row[0] for row in db.execute("SELECT douyin_id FROM accounts WHERE douyin_id<>''")}
    pending=[a for a in records if a['douyin_id'] not in existing]
    added_computers=added_devices=0
    if any(a['computer']=='unassigned' for a in pending):
        db.execute("INSERT OR IGNORE INTO computers VALUES('unassigned','未分配电脑','','','','')")
    for cid in sorted({a['computer'] for a in pending}-{'unassigned'}):
        added_computers+=db.execute('INSERT OR IGNORE INTO computers VALUES(?,?,?,?,?,?)',computers[cid]).rowcount
    for device in sorted({a['device'] for a in pending}):
        added_devices+=db.execute('INSERT OR IGNORE INTO phone_devices(id,name,note) VALUES(?,?,?)',
            (device,'办公室三 · '+device.split(':')[1]+'号手机','腾讯文档 DRWxWc1huUWFsVkZp；独立编号，与其他办公室重号设备不同')).rowcount
    next_id=db.execute('SELECT COALESCE(MAX(CAST(id AS INTEGER)),0)+1 FROM accounts').fetchone()[0]
    slots={cid:db.execute('SELECT COALESCE(MAX(slot),0) FROM accounts WHERE computer_id=?',(cid,)).fetchone()[0] for cid in computers}
    for a in pending:
        slot=None
        if a['computer']!='unassigned':slots[a['computer']]+=1;slot=slots[a['computer']]
        db.execute('''INSERT INTO accounts(id,computer_id,slot,name,status,note,douyin_id,homepage,last_collected,
            registered_on,registration_phone,lifecycle_note,phone_device_id,phone_device_source,phone_device_note,office_group)
            VALUES(?,?,?,?,?,?,?,'','',?,?,?,?,?,?, 'three')''',
            (str(next_id),a['computer'],slot,a['name'],a['status'],a['note'],a['douyin_id'],a['registered'],a['phone'],
             a['lifecycle_note'],a['device'],'tencent-office3','办公室三独立编号；来源：账号情况表第'+str(a['row'])+'行'))
        next_id+=1
    db.execute('INSERT INTO imports(name) VALUES(?)',(MARKER,))
    return dict(source_rows=len(records),accounts_added=len(pending),exact_handles_kept=len(records)-len(pending),
                devices_added=added_devices,computers_added=added_computers,unassigned_computers=sum(a['computer']=='unassigned' for a in pending),
                missing_names=[a['row'] for a in records if '昵称待补充' in a['note']],
                statuses=dict(Counter(a['status'] for a in pending)),source='DRWxWc1huUWFsVkZp/BB08J2')


def run(db_path,write=False):
    with closing(sqlite3.connect(db_path,timeout=15)) as original:
        with closing(sqlite3.connect(':memory:')) as copy:
            original.backup(copy)
            target=original if write else copy
            target.execute('PRAGMA foreign_keys=ON')
            original_columns=[r[1] for r in target.execute('PRAGMA table_info(accounts)')]
            before_accounts=list(target.execute('SELECT '+','.join(original_columns)+' FROM accounts ORDER BY id'))
            before_other={table:list(target.execute('SELECT * FROM '+table+' ORDER BY 1')) for table in ['computers','phone_devices','phone_cards','daily']}
            backup_path=None
            if write:
                backup_path=ROOT/'backups'/('before-office-three-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.db')
                with closing(sqlite3.connect(backup_path)) as backup_db:original.backup(backup_db)
            with target:
                target.execute('BEGIN IMMEDIATE')
                report=apply(target)
                for old in before_accounts:
                    current=target.execute('SELECT '+','.join(original_columns)+' FROM accounts WHERE id=?',(old[0],)).fetchone()
                    if current!=old:raise ValueError('旧账号资料变化，已中止')
                for table,old_rows in before_other.items():
                    keys={r[0] for r in old_rows}
                    current=[r for r in target.execute('SELECT * FROM '+table+' ORDER BY 1') if r[0] in keys]
                    if current!=old_rows:raise ValueError('旧'+table+'资料变化，已中止')
                if list(target.execute('PRAGMA foreign_key_check')):raise ValueError('外键检查失败')
                integrity=target.execute('PRAGMA integrity_check').fetchone()[0]
                if integrity!='ok':raise ValueError('完整性检查失败')
                report.update(integrity=integrity,old_accounts_preserved=len(before_accounts),backup=str(backup_path) if backup_path else None)
            return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--db',default=str(ROOT/'data/board.db'));parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();report=run(args.db,args.apply)
    (SOURCE/('import-report.json' if args.apply else 'validation-report.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
