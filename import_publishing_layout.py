"""Install a verified work-progress snapshot without changing accounts or publication history."""
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
import audit_publishing_matrix as audit
import publishing_layout


def apply(db,folder=audit.FOLDER):
    folder=Path(folder);publishing_layout.migrate(db)
    result=json.loads((folder/'audit.json').read_text(encoding='utf-8'))
    batch='matrix-'+hashlib.sha256((folder/'audit.json').read_bytes()).hexdigest()[:20]
    accounts=[dict(r) for r in db.execute('SELECT * FROM accounts')]
    by_handle=defaultdict(list)
    for account in accounts:
        if account['douyin_id'] and '演示采集' not in account['note']:by_handle[account['douyin_id']].append(account)
    positions={(r['source'],r['matrix_cell']):r for r in result['positions'] if r['kind']=='发布'}
    inserted=linked=unresolved=0
    for group,source in audit.SOURCES.items():
        matrix=json.loads((folder/(group+'-matrix.json')).read_text(encoding='utf-8'))
        cols=[i for i,title in enumerate(matrix[0]) if '发布的设备' in title]
        for row_number,row in enumerate(matrix[1:],2):
            cid=audit.computer_number(row[0])
            if not cid:continue
            computer=('office3:' if group=='three' else '')+cid
            name=('办公室三 · ' if group=='three' else '')+row[0].strip()
            db.execute('INSERT OR IGNORE INTO computers(id,name,shadow_account,ip,operator,category) VALUES(?,?,?,?,?,?)',
                       (computer,name,'',row[3].strip(),row[5].strip() or row[4].strip(),row[6].strip()))
            for slot,ci in enumerate(cols,1):
                label=row[ci].strip() if ci<len(row) else ''
                cell=audit.col_name(ci)+str(row_number)
                record=positions.get((source['name'],cell));account=None;reason=''
                if record:
                    if record['result'] in ['一致','对应电脑错误','对应电脑漏填']:
                        candidates=by_handle.get(record['handle'],[])
                        if group=='three':candidates=[a for a in candidates if a['office_group']=='three' or str(a['phone_device_id']).startswith('office3:')]
                        else:candidates=[a for a in candidates if a['office_group']!='three' and not str(a['phone_device_id']).startswith('office3:')]
                        if len(candidates)==1:account=candidates[0]
                        else:reason='格子身份已确认，但工作台尚无唯一对应账号'
                    else:reason=record['reason']
                number=audit.split_label(label)[0]
                office='three' if group=='three' else 'two' if number.isdigit() and 301<=int(number)<=700 else 'one' if number.isdigit() and int(number)>0 else 'pending'
                if label:
                    if account:linked+=1
                    else:unresolved+=1
                inserted+=db.execute('INSERT OR IGNORE INTO publishing_layout VALUES(?,?,?,?,?,?,?,?,?)',
                      (batch,computer,slot,account['id'] if account else None,label,office,cell,'https://docs.qq.com/sheet/'+source['file']+'?tab='+source['matrix'],reason)).rowcount
    db.execute('INSERT INTO publishing_layout_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET batch=excluded.batch',(batch,))
    return dict(batch=batch,cells_added=inserted,linked=linked,unresolved=unresolved,account_records_changed=0)
