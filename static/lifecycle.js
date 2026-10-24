const lifecycleStages=['满30天未注册','已注册辅助账号','未满30天','注册日期待补充','满30天注册情况待确认','暂无法注册','账号类型待确认','小号'];
function lifecycleDetails(a){
 const detail=`<button class="text-button lifecycle-detail" data-edit="${esc(a.id)}">详情 ↗</button>`;
 if(a.account_type==='小号')return `<span class="progress-tag">小号</span><small>${a.parent_account_id?'大号：'+esc(a.parent_name):'关联大号待确认'}</small>${detail}`;
 if(a.account_type!=='大号')return `<span class="progress-tag muted">类型待确认</span>${detail}`;
 const children=a.child_accounts||[];
 const label=a.effective_auxiliary_state==='已注册'?'已注册小号':!a.registered_on?'日期待补充':a.days_remaining>0?'还差'+a.days_remaining+'天':'已满30天';
 const dateHint=a.effective_auxiliary_state==='已注册'? (children.length?'已关联小号':'小号资料待关联') : a.mature_on?'满30天：'+a.mature_on:'补日期后计算满30天';
 return `<span class="progress-tag ${a.effective_auxiliary_state==='已注册'?'complete':''}">${esc(label)}</span><small>${esc(dateHint)}</small><div class="progress-actions"><select data-aux-status="${esc(a.id)}" aria-label="账号 ${esc(a.name)} 的辅助账号注册状态" ${children.length?'disabled title="已有小号关联，注册状态为已注册"':''}>${['待确认','未注册','已注册','暂无法注册'].map(s=>`<option ${s===a.effective_auxiliary_state?'selected':''}>${s}</option>`).join('')}</select>${detail}</div>`;
}
function lifecycleSummary(){
 $('lifecycleSummary').hidden=page!=='accounts';
 const scoped=officeAccounts();
 const big=scoped.filter(a=>a.account_type==='大号');
 const presets=[['all','全部大号',big.length],['ready','满30天待注册',big.filter(a=>a.lifecycle==='满30天未注册').length],['registered','已注册小号',big.filter(a=>a.effective_auxiliary_state==='已注册').length],['young','未满30天',big.filter(a=>a.registered_on&&a.days_remaining>0&&a.effective_auxiliary_state!=='已注册').length],['missing','待补日期',big.filter(a=>!a.registered_on&&a.effective_auxiliary_state!=='已注册').length],['blocked','暂无法注册',big.filter(a=>a.effective_auxiliary_state==='暂无法注册').length],['unknown','类型待确认',scoped.filter(a=>a.account_type==='待确认').length]];
 $('lifecycleSummary').innerHTML='<p>辅助账号注册进度 · 点击下方直接筛选 · '+esc(officeName())+'统计，按今天 '+esc(data.today)+' 计算</p><div class="lifecycle-grid">'+presets.map(([key,label,count])=>`<button type="button" data-lifecycle-preset="${key}"><small>${label}</small><strong>${count}</strong></button>`).join('')+'</div>';
}
function lifecycleEdit(a){
 $('editPhone').value=a.registration_phone||'';
 $('editType').value=a.account_type||'待确认';$('editRegistered').value=a.registered_on||'';$('editRegistered').max=data.today;
 $('editAux').value=a.auxiliary_state||'待确认';$('editLifecycleNote').value=a.lifecycle_note||'';
 $('editParent').innerHTML='<option value="">待确认 / 暂不关联</option>'+data.accounts.filter(p=>p.account_type==='大号'&&p.id!==a.id).map(p=>`<option value="${esc(p.id)}">${esc(accountDisplay(p))} · ${esc(p.douyin_id||'抖音号待补充')}</option>`).join('');
 $('editParent').value=a.parent_account_id||'';toggleLifecycleEdit();
}
function toggleLifecycleEdit(){const small=$('editType').value==='小号';$('parentField').hidden=!small;$('auxField').hidden=small;if(!small)$('editParent').value='';}
function lifecyclePayload(){return {account_type:$('editType').value,parent_account_id:$('editParent').value,registered_on:$('editRegistered').value,auxiliary_state:$('editAux').value,lifecycle_note:$('editLifecycleNote').value,registration_phone:$('editPhone').value};}
document.addEventListener('DOMContentLoaded',()=>{
 document.addEventListener('click',event=>{const preset=event.target.closest('[data-lifecycle-preset]');if(!preset)return;const key=preset.dataset.lifecyclePreset;$('accountType').value=key==='unknown'?'待确认':'大号';$('ageFilter').value=key==='young'?'young':'';$('lifecycleFilter').value=({ready:'满30天未注册',registered:'已注册辅助账号',blocked:'暂无法注册'})[key]||'';activeLifecyclePreset=key;render();});
 $('accountsBody').addEventListener('change',async event=>{const select=event.target.closest('[data-aux-status]');if(!select)return;select.disabled=true;const requestKey='lifecycle:'+select.dataset.auxStatus;try{await api('/api/lifecycle-status',{id:select.dataset.auxStatus,auxiliary_state:select.value,request_id:claimRequestId(requestKey)});settleRequestId(requestKey);await load();toast('注册状态已保存，日期保留');}catch(error){settleRequestId(requestKey,error);toast(error.message);await load();}});
 $('editType').onchange=toggleLifecycleEdit;
 $('newAccount').onclick=()=>edit('');
 ['accountType','lifecycleFilter','ageFilter'].forEach(id=>$(id).onchange=()=>{activeLifecyclePreset='';render();});
 $('lifecycleFilter').innerHTML='<option value="">全部注册进度</option>'+lifecycleStages.map(s=>'<option>'+s+'</option>').join('');
});
let activeLifecyclePreset='';
function lifecyclePresetMatches(a){if(activeLifecyclePreset==='missing')return a.account_type==='大号'&&!a.registered_on&&a.effective_auxiliary_state!=='已注册';if(activeLifecyclePreset==='young')return a.effective_auxiliary_state!=='已注册';return true;}
