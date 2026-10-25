const byId=id=>document.getElementById(id);
const safe=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let metadata=null,people=[],offices=[],scopeEnabled=false,scopeRows=[],scopeNext='',scopeAfter='';
const officeFieldset=document.createElement('fieldset');
officeFieldset.id='personOfficeFieldset';officeFieldset.hidden=true;
officeFieldset.innerHTML='<legend>可访问的办公室</legend><p id="personScopeHint"></p><div id="personOffices"></div>';
byId('personGrants').before(officeFieldset);
function renderPersonOffices(chosen){
 officeFieldset.hidden=!scopeEnabled;
 const globalAccess=byId('personRole').value==='admin';
 byId('personScopeHint').textContent=globalAccess?'管理员可以访问全部办公室；操作权限仍由下方勾选控制。':'只可访问已勾选办公室的资料；不勾选表示没有资料范围。';
 byId('personOffices').innerHTML=offices.map(office=>`<label class="grant-option"><input type="checkbox" value="${safe(office.id)}" ${chosen.includes(office.id)?'checked':''} ${globalAccess?'disabled':''}>${safe(office.name)}${office.active?'':'（已停用）'}</label>`).join('');
}
function selectedOffices(){return [...document.querySelectorAll('#personOffices input:checked')].map(input=>input.value);}
const pendingRequestIds=new Map();
function newRequestId(){if(typeof crypto.randomUUID==='function')return crypto.randomUUID();const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);bytes[6]=bytes[6]&15|64;bytes[8]=bytes[8]&63|128;let hex='';for(let i=0;i<16;i++)hex+=bytes[i].toString(16).padStart(2,'0');return hex.slice(0,8)+'-'+hex.slice(8,12)+'-'+hex.slice(12,16)+'-'+hex.slice(16,20)+'-'+hex.slice(20);}
function claimRequestId(key){if(!pendingRequestIds.has(key))pendingRequestIds.set(key,newRequestId());return pendingRequestIds.get(key);}
function settleRequestId(key,error){if(error&&error.status===undefined)return;pendingRequestIds.delete(key);}
async function request(path,body){
 const response=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-CSRF-Token':metadata?.user?.csrf||''}:{},body:body?JSON.stringify(body):undefined});
 const result=await response.json();if(response.status===401){location.replace('/login.html');const failure=Error('请先登录');failure.status=401;failure.code=result.code;throw failure;}if(!response.ok){const failure=Error(result.error||'操作失败');failure.status=response.status;failure.code=result.code;throw failure;}return result;
}
async function confirmRequest(path,body,requestKey){
 try{const result=await request(path,body);settleRequestId(requestKey);return result;}
 catch(error){
  if(error.status!==undefined){settleRequestId(requestKey,error);throw error;}
  try{const looked=await request('/api/idempotency/result?request_id='+encodeURIComponent(body.request_id));settleRequestId(requestKey);return looked;}
  catch(queryError){
   if(queryError.status===undefined||queryError.code==='REQUEST_PENDING'||queryError.status===404){const pending=Error('结果待确认');throw pending;}
   settleRequestId(requestKey,queryError);throw queryError;
  }
 }
}
function permissionMarkup(chosen,editable){
 return metadata.permission_groups.map(([title,keys])=>`<fieldset class="grant-group"><legend>${safe(title)}</legend><div class="grant-grid">${keys.map(key=>`<label class="grant-option">${editable?`<input type="checkbox" value="${safe(key)}" ${chosen.includes(key)?'checked':''}>`:''}<span><strong>${safe(metadata.permissions[key])}</strong><small>${safe(metadata.permission_details[key])}</small></span></label>`).join('')}</div></fieldset>`).join('');
}
function selectedGrants(){return [...document.querySelectorAll('#personGrants input:checked')].map(input=>input.value);}
function updateGrantCount(){byId('grantCount').textContent=`${selectedGrants().length} / ${Object.keys(metadata.permissions).length} 项`;}
function setGrants(grants){byId('personGrants').innerHTML=permissionMarkup(grants,true);updateGrantCount();}
function renderPeople(){
 const query=byId('userSearch').value.trim().toLowerCase(),filter=byId('userFilter').value;
 const shown=people.filter(person=>(!query||[person.username,person.name].some(value=>value.toLowerCase().includes(query)))&&(!filter||((filter==='active')===person.active)));
 byId('peopleBody').innerHTML=shown.map(person=>`<tr><td><strong>${safe(person.name)}</strong><small>登录名：${safe(person.username)}</small></td><td>${safe(metadata.roles[person.role])}<small>${person.active?'启用中':'已停用'}</small></td><td>${person.role==='owner'?'<strong>全部业务操作 + 人员授权</strong><small>你的唯一总管理员账号</small>':person.permissions.length?person.permissions.map(key=>`<span class="grant-tag">${safe(metadata.permissions[key])}</span>`).join(''):'仅查看资料，无修改权限'}</td><td>${person.role==='owner'?'<span class="permission-note">本人改密请返回工作台</span>':`<button class="button" data-person="${person.id}">编辑权限 / 重设密码</button>`}</td></tr>`).join('')||'<tr><td colspan="4">没有符合条件的人员</td></tr>';
 byId('peopleCount').textContent=`显示 ${shown.length} / ${people.length} 人`;
 byId('userStats').innerHTML=[['登录人员',people.length],['已启用',people.filter(p=>p.active).length],['已停用',people.filter(p=>!p.active).length],['可分配操作',Object.keys(metadata.permissions).length]].map(([label,count])=>`<div class="stat"><div class="stat-label">${label}</div><strong>${count}</strong></div>`).join('');
}
async function refreshPeople(){
 metadata=await request('/api/auth/me');if(!metadata.user){location.replace('/login.html');return;}if(metadata.user.role!=='owner'){byId('ownerContent').hidden=true;throw Error('只有总管理员可以进入人员授权页面。请返回工作台使用你的日常账号。');}
 const result=await request('/api/auth/users');people=result.users;offices=result.offices||[];scopeEnabled=result.scope_enabled===true;
 byId('officeManagement').hidden=!scopeEnabled;byId('officeList').textContent=offices.map(office=>office.name).join('、')||'尚未添加办公室';
 byId('scopeAssignment').hidden=!scopeEnabled;
 byId('scopeHelp').textContent=scopeEnabled?'员工和只读人员仅能访问授权办公室；管理员跨办公室访问。资料范围与操作权限同时生效，普通管理员不能给他人授权。':'下方勾选决定允许修改的内容。普通管理员不能给他人授权。';
 byId('ownerContent').hidden=false;byId('permissionGuide').innerHTML=permissionMarkup([],false);byId('pageError').textContent='';renderPeople();
}
function editPerson(id){
 const person=people.find(p=>p.id===id);if(person?.role==='owner')return;
 byId('personForm').reset();byId('personId').value=person?.id||'';byId('personUsername').value=person?.username||'';byId('personUsername').readOnly=!!person;byId('personName').value=person?.name||'';byId('personRole').value=person?.role||'employee';byId('personActive').value=person?.active===false?'no':'yes';byId('personPassword').required=!person;byId('personConfirm').required=!person;byId('personPasswordLabel').textContent=person?'重设密码（可留空）':'初始密码';byId('passwordHint').textContent=person?'不修改密码时两项留空；需要重设时填写两次新密码，至少10位。':'请设置至少10位的初始密码，员工可登录后自行修改。';byId('personTitle').textContent=person?'编辑子账号与权限':'添加子账号';byId('personError').textContent='';setGrants(person?.permissions||metadata.defaults.employee);renderPersonOffices(person?.office_ids||[]);byId('personDialog').showModal();
}
byId('userSearch').oninput=renderPeople;byId('userFilter').onchange=renderPeople;
byId('addPerson').onclick=()=>editPerson(null);
byId('refreshPeople').onclick=()=>refreshPeople().catch(error=>byId('pageError').textContent=error.message);
byId('peopleBody').onclick=event=>{const button=event.target.closest('[data-person]');if(button)editPerson(Number(button.dataset.person));};
byId('closePerson').onclick=()=>byId('personDialog').close();
byId('personDialog').addEventListener('close',()=>{byId('personPassword').value='';byId('personConfirm').value='';});
byId('personRole').onchange=()=>{setGrants(metadata.defaults[byId('personRole').value]);renderPersonOffices(selectedOffices());};
byId('grantAll').onclick=()=>setGrants(Object.keys(metadata.permissions));byId('grantNone').onclick=()=>setGrants([]);byId('personGrants').onchange=updateGrantCount;
byId('personForm').onsubmit=async event=>{
 event.preventDefault();byId('personError').textContent='';if(byId('personPassword').value!==byId('personConfirm').value){byId('personError').textContent='两次密码不一致';return;}
 const button=event.submitter;button.disabled=true;
 try{await confirmRequest('/api/auth/users',{id:byId('personId').value?Number(byId('personId').value):null,username:byId('personUsername').value,name:byId('personName').value,role:byId('personRole').value,active:byId('personActive').value==='yes',password:byId('personPassword').value,permissions:selectedGrants(),...(scopeEnabled?{office_ids:selectedOffices()}:{}),request_id:claimRequestId('personnel-save')},'personnel-save');byId('personDialog').close();await refreshPeople();byId('auditSection').hidden=true;}catch(error){byId('personError').textContent=error.message;}finally{button.disabled=false;}
};
byId('auditToggle').onclick=async()=>{try{const entries=(await request('/api/auth/audit')).entries;byId('peopleAudit').innerHTML=entries.map(entry=>`<p>${safe(new Date(entry.at*1000).toLocaleString('zh-CN'))} · ${safe(entry.actor_name)} · ${safe(entry.action)} ${safe(entry.target)}</p>`).join('')||'<p>暂无记录</p>';byId('auditSection').hidden=!byId('auditSection').hidden;}catch(error){byId('pageError').textContent=error.message;}};
byId('officeForm').onsubmit=async event=>{
 event.preventDefault();const button=event.submitter;button.disabled=true;byId('officeError').textContent='';
 const officeName=byId('officeName').value;const requestKey='office-create:'+officeName.trim();try{await confirmRequest('/api/auth/offices',{name:officeName,request_id:claimRequestId(requestKey)},requestKey);byId('officeName').value='';await refreshPeople();}
 catch(error){byId('officeError').textContent=error.message;}finally{button.disabled=false;}
};
function editEntityScope(){
 const row=scopeRows.find(row=>String(row.id)===byId('scopeEntity').value);if(!row)return;
 byId('scopeOffices').innerHTML=`<option value="__pending__" ${row.office_ids.length?'':'selected'}>待核对，暂不分配</option>`+offices.map(office=>`<option value="${safe(office.id)}" ${row.office_ids.includes(office.id)?'selected':''}>${safe(office.name)}</option>`).join('');
}
async function loadEntityScopes(after=''){
 scopeAfter=after;byId('scopeMessage').textContent='读取中…';
 const result=await request('/api/auth/entity-scopes?kind='+encodeURIComponent(byId('scopeKind').value)+'&after='+encodeURIComponent(after));
 scopeRows=result.entities;scopeNext=result.next_after;byId('scopeNext').disabled=!scopeNext;byId('scopeForm').hidden=!scopeRows.length;
 byId('scopeEntity').innerHTML=scopeRows.map(row=>`<option value="${safe(row.id)}">${safe(row.id)} · ${safe(row.name)}</option>`).join('');editEntityScope();byId('scopeMessage').textContent=scopeRows.length?'已读取，请核对归属后保存。':'没有资料。';
}
byId('scopeLoad').onclick=()=>loadEntityScopes().catch(error=>byId('scopeMessage').textContent=error.message);
byId('scopeNext').onclick=()=>loadEntityScopes(scopeNext).catch(error=>byId('scopeMessage').textContent=error.message);
byId('scopeKind').onchange=()=>{scopeRows=[];scopeNext='';byId('scopeForm').hidden=true;byId('scopeNext').disabled=true;};
byId('scopeEntity').onchange=editEntityScope;
byId('scopeForm').onsubmit=async event=>{
 event.preventDefault();const button=event.submitter;button.disabled=true;
 try{
  const row=scopeRows.find(row=>String(row.id)===byId('scopeEntity').value);if(!row)throw Error('请先读取资料');
  const selected=[...byId('scopeOffices').selectedOptions].map(option=>option.value);
  if(!selected.length||selected.includes('__pending__')&&selected.length>1)throw Error('请选择办公室，或单独选择待核对');
  const officeIds=selected.filter(id=>id!=='__pending__');
  const requestKey='entity-scope:'+byId('scopeKind').value+':'+row.id+':'+row.scope_version+':'+[...officeIds].sort().join(',');
  await confirmRequest('/api/auth/entity-scope',{kind:byId('scopeKind').value,id:row.id,expected_scope:row.scope_version,office_ids:officeIds,request_id:claimRequestId(requestKey)},requestKey);
  await loadEntityScopes(scopeAfter);byId('scopeMessage').textContent='归属已保存；相关人员刷新后按新范围访问。';
 }catch(error){byId('scopeMessage').textContent=error.message;}finally{button.disabled=false;}
};
refreshPeople().catch(error=>byId('pageError').textContent=error.message);
