"""Reconcile Tencent matrix coordinates with the account table, without writes."""
import json
import re
from collections import Counter,defaultdict
from pathlib import Path

FOLDER=Path('E:/Downloads/DouyinDeviceAudit-20261017')
SOURCES={
 'main':dict(name='办公室一、二原文档',file='DRVFPb29NZ2JieVJS',matrix='tnqw1u',accounts='3qxa7x',computer_col=12),
 'three':dict(name='办公室三新文档',file='DRWxWc1huUWFsVkZp',matrix='nh0fs2',accounts='BB08J2',computer_col=11),
}


def col_name(number):
    result=''
    while number>=0:result=chr(65+number%26)+result;number=number//26-1
    return result


def normalized(value):return re.sub(r'[\s\u200b\ufeff]+','',value).strip()


def phone_number(value):
    match=re.fullmatch(r'(\d+)(?:号手机|多开(?:抖音)?|小号)?',normalized(value))
    return str(int(match[1])) if match and int(match[1])>0 else normalized(value)


def computer_number(value):
    match=re.fullmatch(r'(\d+)(?:号(?:电脑|设备))?',normalized(value))
    return str(int(match[1])) if match and int(match[1])>0 else None


def split_label(value):
    # A device identifier precedes a hyphen. Comments begin at the first bracket.
    raw=value.strip()
    match=re.match(r'^\s*[.。]?\s*(.+?)\s*[-—－–]\s*(.+)$',raw)
    if not match:return '',normalized(re.split(r'[（(]',raw)[0])
    prefix,name=match.groups()
    return phone_number(prefix),normalized(re.split(r'[（(]',name)[0])


def analyze():
    errors=[];pending=[];positions=[];unassigned=[];summaries=[];matrices=[]
    for group,source in SOURCES.items():
        raw=json.loads((FOLDER/(group+'-accounts.json')).read_text(encoding='utf-8'))
        matrix=json.loads((FOLDER/(group+'-matrix.json')).read_text(encoding='utf-8'))
        ac=[];by_name=defaultdict(list);by_phone=defaultdict(list)
        for row_number,row in enumerate(raw[1:],2):
            row=row+['']*22
            if not row[5].strip() and not row[7].strip():continue
            account=dict(row=row_number,name=row[5].strip(),handle=row[7].strip(),phone=row[6].strip(),number=phone_number(row[6]),computer=row[source['computer_col']].strip(),status=row[16 if group=='main' else 15].strip())
            ac.append(account)
            if account['name']:by_name[normalized(account['name'])].append(account)
            if account['number']:by_phone[account['number']].append(account)
        active_cols=[i for i,title in enumerate(matrix[0]) if '发布的设备' in title]
        stop_cols=[i for i,title in enumerate(matrix[0]) if '停发' in title]
        cells=[];matrix_rows=[];ignored=[]
        for row_number,row in enumerate(matrix[1:],2):
            cid=computer_number(row[0])
            if not cid:
                if any(row[i].strip() for i in active_cols if i<len(row)):ignored.append(row_number)
                continue
            matrix_rows.append([row[0].strip()]+[(row[i].strip() if i<len(row) else '') for i in active_cols])
            for ci in active_cols+stop_cols:
                text=row[ci].strip() if ci<len(row) else ''
                if not text:continue
                kind='发布' if ci in active_cols else '停发'
                number,name=split_label(text)
                candidates=by_name.get(name,[]) if name else []
                exact=[a for a in candidates if a['number']==number] if number else []
                account=None;certain=False;reason='';choices=[]
                if len(exact)==1:account=exact[0];certain=True;reason='手机编号与昵称均匹配'
                elif len(exact)>1:choices=exact;reason='同编号同昵称对应多条账号'
                elif len(candidates)==1:account=candidates[0];reason='昵称唯一，但手机编号不同或未明确'
                elif len(candidates)>1:choices=candidates;reason='昵称重复，编号不能唯一确定'
                elif number and len(by_phone.get(number,[]))==1:
                    account=by_phone[number][0];reason='编号唯一，但昵称不同或缺失'
                elif number and by_phone.get(number):choices=by_phone[number];reason='编号对应多个账号，昵称未匹配'
                else:reason='未匹配账号表记录' if text not in ['.','。','-','—'] else '占位符，不是账号'
                cells.append(dict(group=group,source=source['name'],row=row_number,col=ci,cell=col_name(ci)+str(row_number),computer=cid,header=matrix[0][ci],text=text,kind=kind,number=number,name=name,account=account,certain=certain,reason=reason,choices=choices))
        assignments=defaultdict(list);stopped=defaultdict(list)
        for cell in cells:
            if cell['account'] and cell['certain']:
                (assignments if cell['kind']=='发布' else stopped)[cell['account']['row']].append(cell)
        group_errors=group_missing=0
        for cell in cells:
            account=cell['account'];result='';suggestion=''
            if not cell['certain']:
                result='待核对';suggestion=cell['reason']
            elif len(assignments.get(account['row'],[]))>1:
                result='重复发布安排';suggestion='同一账号出现在多个发布格子，先确认保留位置'
            elif assignments.get(account['row']) and stopped.get(account['row']):
                result='发布停发冲突';suggestion='同一账号同时出现在发布和停发列'
            elif cell['kind']=='停发':
                result='仅作停发记录';suggestion='停发列不作为当前发布电脑依据'
            elif computer_number(account['computer'])==cell['computer']:
                result='一致'
            elif not account['computer']:
                result='对应电脑漏填';suggestion='在账号表填写 '+cell['computer']+' 号电脑';group_missing+=1
            else:
                result='对应电脑错误';suggestion='将账号表对应电脑改为 '+cell['computer']+' 号电脑';group_errors+=1
            cell['result']=result
            record=dict(source=source['name'],kind=cell['kind'],computer=cell['computer'],header=cell['header'],matrix_cell=cell['cell'],matrix_text=cell['text'],account_row=account['row'] if account else '',account_name=account['name'] if account else '',handle=account['handle'] if account else '',account_phone=account['phone'] if account else '',old_computer=account['computer'] if account else '',account_cell=col_name(source['computer_col'])+str(account['row']) if account else '',result=result,reason=suggestion or cell['reason'],candidates='；'.join(str(a['row'])+'行 '+(a['name'] or '无昵称') for a in cell['choices']),action='待处理')
            positions.append(record)
            if result in ['对应电脑错误','对应电脑漏填']:errors.append(record)
            elif result in ['待核对','重复发布安排','发布停发冲突']:pending.append(record)
        for account in ac:
            if not assignments.get(account['row']):
                candidate_cells=[c for c in cells if c['kind']=='发布' and ((c['account'] and c['account']['row']==account['row']) or account in c['choices'])]
                unassigned.append(dict(source=source['name'],row=account['row'],name=account['name'],handle=account['handle'],phone=account['phone'],computer=account['computer'],status=account['status'],stopped='、'.join(c['cell'] for c in stopped.get(account['row'],[])),reason='存在待核对发布格子' if candidate_cells else '仅在停发列' if stopped.get(account['row']) else '未在有效发布格子中匹配，不能据此认定对应电脑错误'))
        valid_cells=[c for c in cells if c['kind']=='发布' and c['text'] not in ['.','。','-','—']]
        summaries.append(dict(source=source['name'],accounts=len(ac),active=len(valid_cells),matched=sum(c['certain'] for c in valid_cells),errors=group_errors,missing=group_missing,review=sum(c['result'] in ['待核对','重复发布安排','发布停发冲突'] for c in valid_cells),unassigned=sum(r['source']==source['name'] for r in unassigned),ignored_rows=ignored))
        matrices.append(dict(group=group,source=source,headers=['电脑 / 主机']+[matrix[0][i] for i in active_cols],rows=matrix_rows))
    output=dict(summary=summaries,errors=errors,pending=pending,positions=positions,unassigned=unassigned,matrices=matrices)
    (FOLDER/'audit.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    return output


if __name__=='__main__':
    result=analyze()
    print(json.dumps(result['summary'],ensure_ascii=False))
    print('明确错误及漏填',len(result['errors']),'待核对格子',len(result['pending']))
