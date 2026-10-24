// All views read the same latest nurturing record; absence never means a check passed.
function accountVerification(account){return (data.nurture?.records||[]).find(record=>record.account_id===account.id)||null;}
function verificationState(account){return accountVerification(account)?.verification_state||'未登记';}
function verificationMatches(account){const value=$('verificationFilter')?.value||'';return !value||verificationState(account)===value;}
function renderVerificationFilter(){const select=$('verificationFilter'),value=select.value,states=data.nurture?.verification_states||['待检查','无需核对，可使用','核对处理中','核对已通过'];select.innerHTML='<option value="">全部核对情况</option>'+[...states,'未登记'].map(state=>`<option value="${esc(state)}">${esc(state)}</option>`).join('');select.value=value;}
function verificationMarkup(account,compact=false){
 const record=accountVerification(account),state=verificationState(account),tone=state==='核对处理中'?'warn':['无需核对，可使用','核对已通过'].includes(state)?'good':'';
 const hint=record?(record.verification_note||'已登记：'+state):'尚未登记核对情况，不代表需要核对或已通过';
 return `<div class="verification-info ${compact?'compact':''}" title="${esc(hint)}"><span class="badge ${tone}">核对：${esc(state)}</span>${record?.verification_owner?`<small>负责人：${esc(record.verification_owner)}</small>`:''}${!compact&&record?.verification_updated_on?`<small>更新：${esc(record.verification_updated_on)}</small>`:''}${!compact&&record?.verification_note?`<small class="verification-note">备注：${esc(record.verification_note)}</small>`:''}${`<button type="button" class="text-button" data-verification="${esc(account.id)}" ${hasPermission('nurture_manage')?'':'disabled title="需要管理养号进度及截图权限"'}>修改核对</button>`}</div>`;
}

function openVerificationEditor(id){
 if(!hasPermission('nurture_manage'))return toast('需要管理养号进度及截图权限');
 const account=data.accounts.find(a=>a.id===id);if(!account)return;
 const record=accountVerification(account);let dialog=$('verificationEditor');
 if(!dialog){dialog=document.createElement('dialog');dialog.id='verificationEditor';document.body.append(dialog);}
 dialog.innerHTML=`<form id="verificationForm"><div class="dialog-head"><h2>修改核对 · ${esc(accountDisplay(account))}</h2><button type="button" class="text-button" id="closeVerification">关闭</button></div><label>核对情况<select id="verificationEditState">${data.nurture.verification_states.map(state=>`<option ${state===(record?.verification_state||'待检查')?'selected':''}>${esc(state)}</option>`).join('')}</select></label><label>核对负责人<input id="verificationEditOwner" maxlength="100" value="${esc(record?.verification_owner||'')}"></label><label>核对备注<textarea id="verificationEditNote" maxlength="2000" rows="4">${esc(record?.verification_note||'')}</textarea></label><p class="permission-note">${!record?'首次保存会登记核对记录，不改变账号状态，不自动开始养号。':record.completed_on?'本轮养号已结束，修改核对信息不会重开养号。':'正在养号时改为待检查或核对处理中，会自动暂停计时；通过后需手动继续养号。'}保存后同步账号管理、发布看板和养号面板。</p><p id="verificationEditError" role="alert"></p><button id="saveAccountVerification" class="button primary" type="submit">保存核对</button></form>`;
 captureVersions('verificationEditor');dialog.showModal();
 $('closeVerification').onclick=()=>dialog.close();
 $('verificationForm').onsubmit=async event=>{
  event.preventDefault();const button=$('saveAccountVerification');if(button.disabled)return;button.disabled=true;
  const requestKey='account-verification:'+id;
  try{await api('/api/workspace/nurture/verification',{id,verification_state:$('verificationEditState').value,verification_owner:$('verificationEditOwner').value,verification_note:$('verificationEditNote').value,request_id:claimRequestId(requestKey)});settleRequestId(requestKey);dialog.close();await load();toast('核对记录已保存，发布看板已同步');}
  catch(error){settleRequestId(requestKey,error);$('verificationEditError').textContent=error.message;}finally{button.disabled=false;}
 };
}
document.addEventListener('click',event=>{const button=event.target.closest('[data-verification]');if(button&&!button.disabled)openVerificationEditor(button.dataset.verification);});
