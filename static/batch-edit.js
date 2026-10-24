const batchSelections=new Map();let batchNurtureRows=[];
function batchAllowed(permission){return typeof hasPermission==='function'?hasPermission(permission):!!can(permission);}
function batchSelection(kind){if(kind==='accounts'&&typeof selected!=='undefined')return selected;if(!batchSelections.has(kind))batchSelections.set(kind,new Set());return batchSelections.get(kind);}
function batchPick(kind,id){return `<input type="checkbox" data-batch-pick="${kind}" value="${esc(id)}" aria-label="批量选择${({accounts:'账号',cards:'手机卡',devices:'手机设备',daily:'发布账号',verification:'养号账号'})[kind]}" ${batchSelection(kind).has(String(id))?'checked':''}>`;}
function batchVisible(kind){
 if(kind==='cards')return filteredCards();if(kind==='devices')return filteredPhones();if(kind==='verification')return batchNurtureRows;
 return filtered().filter(a=>kind!=='daily'||a.computer_id!=='unassigned');
}
function batchCurrentPage(kind){const rows=batchVisible(kind);return kind==='cards'?rows.slice((cardPageIndex-1)*cardPageSize,cardPageIndex*cardPageSize):kind==='devices'?rows.slice((devicePageIndex-1)*devicePageSize,devicePageIndex*devicePageSize):rows;}
function batchBar(kind){
 const selection=batchSelection(kind),visible=batchVisible(kind),ids=new Set(visible.map(r=>String(r.id)));for(const id of selection)if(!ids.has(id))selection.delete(id);
 const title={accounts:'账号',cards:'手机卡',devices:'手机设备',daily:'发布账号',verification:'养号账号'}[kind];
 const actions=kind==='daily'?[['daily','批量记录发布'],['accounts','批量账号状态'],['verification','批量核对']]:kind==='accounts'?[['accounts','批量修改'],['verification','批量核对']]:[[kind,'批量修改']];
 return `<span>已选 ${selection.size} 条${title}</span><button type="button" class="button" data-batch-all="${kind}" data-batch-page="yes">选中本页</button><button type="button" class="button" data-batch-all="${kind}">选中全部筛选结果（${visible.length}）</button><button type="button" class="text-button" data-batch-clear="${kind}">取消选择</button>${actions.map(([action,label])=>`<button type="button" class="button" data-batch-open="${action}" data-batch-source="${kind}" ${!selection.size||!batchFields(action).some(f=>batchAllowed(f[3]))?'disabled':''}>${label}</button>`).join('')}`;
}
function renderBatchControls(){
 for(const [kind,id] of [['accounts','batchAccountsBar'],['daily','batchBoardBar'],['cards','batchCardsBar'],['devices','batchDevicesBar']]){
  const active=typeof page!=='undefined'&&({accounts:'accounts',board:'daily',cards:'cards',phones:'devices'}[page]===kind);if(active&&$(id))$(id).innerHTML=batchBar(kind);
 }
 if($('batchNurtureBar'))$('batchNurtureBar').innerHTML=batchBar('verification');
}
function batchFields(kind){
 const text=(key,label,permission,max=100)=>[key,label,null,permission,max],choice=(key,label,options,permission)=>[key,label,options,permission];
 if(kind==='cards')return [text('owner','卡主 / 提供人','card_manage'),choice('ownership','卡归属',data.phone_cards.ownerships,'card_manage'),text('keeper','内部负责人','card_manage'),text('code_contact','接码联系人 / 联系方式','card_manage',200)];
 if(kind==='devices')return [choice('state','设备状态',['auto',...(data.phone_devices?.statuses||[])],'device_manage'),text('owner','设备负责人','device_manage'),text('location','设备位置','device_manage',200)];
 if(kind==='accounts')return [choice('status','账号状态',data.statuses,'account_status'),choice('card_stability','稳定情况',['待确认','未稳定','已稳定'],'account_progress'),choice('password_state','改密情况',['待确认','未修改','已修改'],'account_progress')];
 if(kind==='daily')return [choice('published','发布记录',['已发','未发'],'publish')];
 return [choice('verification_state','核对状态',data.nurture.verification_states,'nurture_manage'),text('verification_owner','核对负责人','nurture_manage'),text('verification_note','核对备注','nurture_manage',2000)];
}
function batchRecord(kind,id){if(kind==='cards')return data.phone_cards.cards.find(r=>String(r.id)===id);if(kind==='devices')return data.phone_devices.devices.find(r=>r.id===id);return data.accounts.find(r=>r.id===id);}
function batchOldValue(kind,record,key){if(kind==='verification'){const value=(data.nurture.records.find(r=>r.account_id===record.id)||{})[key];return value||(key==='verification_state'?'未登记':'');}if(kind==='daily')return record.published?'已发':'未发';if(key==='state'&&record.state_source==='auto')return '按账号自动判断';return record[key]??'';}
function openBatchEditor(kind,source=kind){
 const ids=[...batchSelection(source)];if(!ids.length)return;if(ids.length>2000)return;
 const fields=batchFields(kind).filter(f=>batchAllowed(f[3])),records=ids.map(id=>batchRecord(kind,id));if(!fields.length||records.some(r=>!r))return;
 let dialog=$('batchEditor');if(!dialog){dialog=document.createElement('dialog');dialog.id='batchEditor';document.body.append(dialog);}
 dialog.innerHTML=`<form id="batchEditForm"><div class="dialog-head"><h2>批量修改 · ${ids.length} 条记录</h2><button type="button" class="text-button" id="closeBatchEditor">关闭</button></div><p>勾选要修改的字段；未勾选的保持原值。已勾选文本字段留空表示清空该字段。</p>${kind==='daily'?`<p>发布工作日期：${esc(data.date)}</p>`:''}${kind==='verification'?'<p>养号中改为待检查或核对处理中会暂停计时；通过后仍需手动继续。</p>':''}${fields.map(([key,label,options,,max])=>`<div class="batch-field"><label><input type="checkbox" data-batch-field="${key}"> 修改${esc(label)}</label>${options?`<select id="batchValue-${key}" aria-label="批量${esc(label)}" disabled>${options.map(value=>`<option value="${esc(value)}">${esc(value==='auto'?'按账号自动判断':value)}</option>`).join('')}</select>`:max===2000?`<textarea id="batchValue-${key}" aria-label="批量${esc(label)}" maxlength="${max}" rows="3" disabled></textarea>`:`<input id="batchValue-${key}" aria-label="批量${esc(label)}" maxlength="${max}" disabled>`}</div>`).join('')}<p id="batchEditError" role="alert"></p><button type="button" class="button" id="previewBatchEdit">预览修改</button><div id="batchEditPreview"></div><button type="submit" class="button primary" id="saveBatchEdit" disabled>确认批量保存</button></form>`;
 captureVersions('batchEditor');dialog.showModal();let prepared=null;
 $('closeBatchEditor').onclick=()=>dialog.close();
 $('batchEditForm').oninput=event=>{if(event.target.dataset.batchField)$('batchValue-'+event.target.dataset.batchField).disabled=!event.target.checked;prepared=null;$('saveBatchEdit').disabled=true;$('batchEditPreview').innerHTML='';};
 $('previewBatchEdit').onclick=()=>{
  const patch={},chosen=fields.filter(([key])=>dialog.querySelector(`[data-batch-field="${key}"]`).checked);
  if(!chosen.length){$('batchEditError').textContent='请至少勾选一个要修改的字段';return;}
  for(const [key] of chosen)patch[key]=$('batchValue-'+key).value;
  if(kind==='daily'){patch.published=patch.published==='已发';patch.date=data.date;}
  prepared={kind,ids,patch};$('batchEditError').textContent='';
  $('batchEditPreview').innerHTML=`<p>确认以下 ${records.length} 条记录${kind==='daily'?'（'+esc(patch.date)+'）':''}：</p><div class="batch-preview-scroll"><table><thead><tr><th>记录</th>${chosen.map(f=>`<th>${esc(f[1])}：原值 → 新值</th>`).join('')}</tr></thead><tbody>${records.map(record=>`<tr><td>${esc(['accounts','daily','verification'].includes(kind)?accountDisplay(record):(record.phone||record.name||'未命名资料'))}</td>${chosen.map(([key])=>`<td>${esc(batchOldValue(kind,record,key))||'（空）'} → <strong>${esc(key==='published'?(patch.published?'已发':'未发'):patch[key]==='auto'?'按账号自动判断':patch[key])||'（清空）'}</strong></td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  $('saveBatchEdit').disabled=false;
 };
 $('batchEditForm').onsubmit=async event=>{event.preventDefault();if(!prepared||$('saveBatchEdit').disabled)return;const button=$('saveBatchEdit');button.disabled=true;const requestKey='batch-edit:'+prepared.kind+':'+prepared.ids.map(String).slice().sort().join(',')+':'+JSON.stringify(prepared.patch,Object.keys(prepared.patch).sort());try{
  const snapshot=capturedVersions.get('batchEditor');const payload={...prepared,request_id:claimRequestId(requestKey)};const result=typeof api==='function'?await api('/api/batch-edit',payload):await send('/api/batch-edit',payload,snapshot);
  settleRequestId(requestKey);batchSelection(source).clear();dialog.close();if(typeof load==='function'){await load();toast('已批量保存 '+result.count+' 条记录');}else await reload();
 }catch(error){settleRequestId(requestKey,error);$('batchEditError').textContent=error.message;}finally{button.disabled=false;}};
}
document.addEventListener('change',event=>{const kind=event.target.dataset.batchPick;if(!kind)return;const selected=batchSelection(kind),id=String(event.target.value);event.target.checked?selected.add(id):selected.delete(id);renderBatchControls();});
document.addEventListener('click',event=>{
 const all=event.target.closest('[data-batch-all]'),clear=event.target.closest('[data-batch-clear]'),open=event.target.closest('[data-batch-open]');
 if(open)return openBatchEditor(open.dataset.batchOpen,open.dataset.batchSource);
 if(all){const kind=all.dataset.batchAll;for(const row of all.dataset.batchPage?batchCurrentPage(kind):batchVisible(kind))batchSelection(kind).add(String(row.id));}
 if(clear)batchSelection(clear.dataset.batchClear).clear();
 if(all||clear){if(typeof page!=='undefined')render();else document.querySelectorAll('[data-batch-pick]').forEach(input=>input.checked=batchSelection(input.dataset.batchPick).has(String(input.value)));renderBatchControls();}
});
