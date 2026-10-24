"""Import an authorized Tencent snapshot without rewriting account records."""
import csv
import io
import json
import re
from collections import defaultdict
from pathlib import Path
import phone_cards


def owner_name(value):
    return re.sub(r'(抖音小号|抖音副号|已销户|已注销|接码)', '', value).strip(' \n，,、-/')


def prepare(snapshot):
    result=json.loads(Path(snapshot).read_text(encoding='utf-8'))
    if result.get('error'):raise ValueError('腾讯文档快照读取失败')
    rows=list(csv.reader(io.StringIO(result['csv_data'])))
    if not rows or len(rows[0])<10 or rows[0][2]!='注册抖音用的手机号' or '谁的卡' not in rows[0][0]:
        raise ValueError('源表列结构发生变化，请重新核对映射')
    groups=defaultdict(list);invalid=[]
    for number,row in enumerate(rows[1:],2):
        row=(row+['']*21)[:21]
        phone=row[2].strip()
        if phone_cards.valid_phone(phone):groups[phone].append((number,row))
        elif phone:invalid.append(number)
    records=[];conflicts=0
    for phone,items in groups.items():
        owners=sorted({row[0].strip() for _,row in items if row[0].strip()})
        names=sorted({owner_name(value) for value in owners if owner_name(value)})
        locations=sorted({row[1].strip() for _,row in items if row[1].strip()})
        contacts=sorted({owner_name(row[i].strip()) for _,row in items for i in [0,1] if '接码' in row[i] and owner_name(row[i].strip())})
        details=['腾讯文档 最新账号情况表1；源行：'+','.join(str(n) for n,_ in items)]
        if owners:details.append('原卡主标注：'+' / '.join(owners))
        if locations:details.append('原手机卡位置：'+' / '.join(locations))
        if len(names)>1:details.append('卡主标注不一致，请核对');conflicts+=1
        for col,label in [(8,'原小号说明'),(9,'原账号类型/卡说明')]:
            values=sorted({row[col].strip() for _,row in items if row[col].strip()})
            if values:details.append(label+'：'+' / '.join(values))
        records.append(dict(phone=phone,owner=names[0] if len(names)==1 and len(names[0])<=100 else '',
                            location=' / '.join(locations)[:200],
                            code_contact=contacts[0] if len(contacts)==1 and len(contacts[0])<=200 else '',
                            source_note='\n'.join(details)))
    return records,dict(source_rows=len(rows)-1,unique_phones=len(records),invalid_phone_rows=invalid,owner_conflicts=conflicts)


def apply(db, snapshot):
    records,report=prepare(snapshot)
    phone_cards.migrate(db)
    added=0;filled=0
    for record in records:
        added+=db.execute('INSERT OR IGNORE INTO phone_cards(phone) VALUES(?)',(record['phone'],)).rowcount
        for field in ['owner','location','code_contact','source_note']:
            if record[field]:
                filled+=db.execute('UPDATE phone_cards SET '+field+"=? WHERE phone=? AND "+field+"=''",(record[field],record['phone'])).rowcount
    report.update(added=added,fields_filled=filled,total_cards=db.execute('SELECT COUNT(*) FROM phone_cards').fetchone()[0])
    return report
