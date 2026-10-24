"""Apply narrowly scoped lifecycle UI integration to the existing board."""
from pathlib import Path

p=Path('static/index.html')
s=p.read_text(encoding='utf-8')
s=s.replace('<section class="workspace">','<section id="lifecycleSummary" class="lifecycle-summary" hidden></section><section class="workspace">',1)
s=s.replace('<button id="clear"','<select id="accountType"><option value="">全部账号类型</option><option>大号</option><option>小号</option><option>待确认</option></select><select id="lifecycleFilter"><option value="">全部注册进度</option></select><button id="newAccount" class="button">新增账号 / 小号</button><button id="clear"',1)
s=s.replace('<th>账号状态</th>','<th>账号类型 / 注册进度</th><th>账号状态</th>',1)
s=s.replace('<label>真实抖音号', '<div class="form-row"><label>账号类型<select id="editType"><option>待确认</option><option>大号</option><option>小号</option></select></label><label>注册日期<input id="editRegistered" type="date"></label></div><label id="parentField">关联大号<select id="editParent"></select></label><label id="auxField">辅助账号注册状态<select id="editAux"><option>待确认</option><option>未注册</option><option>已注册</option><option>暂无法注册</option></select></label><label>注册进度备注<textarea id="editLifecycleNote" rows="2" maxlength="1000"></textarea></label><label>真实抖音号',1)
s=s.replace('<script src="/app.js">','<script src="/lifecycle.js"></script><script src="/app.js">')
p.write_text(s,encoding='utf-8')
p=Path('static/app.js');s=p.read_text(encoding='utf-8')
s=s.replace("&&(!$('published').value", "&&(!$('accountType').value||a.account_type===$('accountType').value)&&(!$('lifecycleFilter').value||a.lifecycle===$('lifecycleFilter').value)&&(!$('published').value",1)
s=s.replace("const accounts=filtered().filter", "lifecycleSummary();const accounts=filtered().filter",1)
s=s.replace('<td>${badge(a)}</td>', '<td class="lifecycle-cell">${lifecycleDetails(a)}</td><td>${badge(a)}</td>',1)
s=s.replace('colspan="8"','colspan="9"')
s=s.replace("const a=data.accounts.find(a=>a.id===id);$('editId')", "const a=data.accounts.find(a=>a.id===id)||{id:'',name:'',computer_id:'unassigned',slot:1,status:'待确认',douyin_id:'',homepage:'',note:''};lifecycleEdit(a);$('editId')",1)
s=s.replace('`编辑账号 #${a.id}`', "a.id?`编辑账号 #${a.id}`:'新增账号 / 小号'",1)
s=s.replace("await api('/api/account',{id:", "await api($('editId').value?'/api/account':'/api/account/create',{...lifecyclePayload(),id:",1)
s=s.replace("['query','computer','status','published'].forEach", "['query','computer','status','published','accountType','lifecycleFilter'].forEach",1)
p.write_text(s,encoding='utf-8')
p=Path('static/styles.css');s=p.read_text(encoding='utf-8')
s+='\n.lifecycle-summary{padding:18px 22px;background:white;border:1px solid var(--line);border-radius:12px;margin-bottom:18px}.lifecycle-summary p{font-size:12px;color:#6b7d6e;margin:0 0 15px}.lifecycle-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:14px}.lifecycle-grid small{font-size:11px;color:#7e9586}.lifecycle-grid strong{display:block;font-size:25px;margin-top:8px}.lifecycle-cell{min-width:220px;line-height:1.6}.account-table{min-width:1600px}dialog{max-height:90vh;overflow:auto}@media(max-width:1100px){.lifecycle-grid{grid-template-columns:repeat(3,1fr)}}\n'
p.write_text(s,encoding='utf-8')
