let pendingReviewPage=0;
const pendingReviewReasons={all:'全部待匹配',position:'原表多个发布位置',check:'原文标注待核对',missing:'缺少明确手机编号',noaccount:'未找到同手机账号',occupied:'近似账号已占位置',ambiguous:'多个近似候选',different:'同手机但名称差异较大',ready:'有唯一近似候选'};
function pendingReviewRows(){
 const cells=data.publishing_layout?.cells||[],positions=new Map();
 for(const cell of cells)if(cell.account_id){if(!positions.has(cell.account_id))positions.set(cell.account_id,[]);positions.get(cell.account_id).push(cell);}
 return cells.filter(cell=>cell.label?.trim()&&!cell.account_id).map(cell=>{
  const prefix=String(cell.label).match(/^\s*[.。]?\s*(\d+)(?:号手机|多开(?:抖音)?|小号)?\s*[-—－–]/),phone=prefix?.[1]||'';
  let candidates=data.accounts.filter(a=>publishingCandidateScope(a,cell)&&(phone?deviceNumberMatches(a.phone_device_id,phone):false)).map(account=>({account,suggestion:publishingSuggestion(account,cell),positions:positions.get(account.id)||[],basis:'同手机'}));
  const samePhoneCount=candidates.length,near=candidates.filter(c=>c.suggestion.recommended);
  let reason=String(cell.reason).includes('多个发布格')?'position':(cell.label+near.map(c=>c.account.name).join('')).match(/待核对|待确认/)?'check':!phone?'missing':!samePhoneCount?'noaccount':!near.length?'different':near.length>1?'ambiguous':near[0].positions.length?'occupied':'ready';
  if(!phone){
   const name=publishingNameCore(cell.label);
   if(name.length>=5)candidates=data.accounts.filter(a=>publishingCandidateScope(a,cell)).map(account=>({account,suggestion:publishingSuggestion(account,cell),positions:positions.get(account.id)||[],basis:'仅名称线索，手机未确认'})).filter(c=>Math.min(name.length,publishingNameCore(c.account.name).length)>=5&&c.suggestion.distance/Math.max(name.length,publishingNameCore(c.account.name).length)<=0.4).sort((a,b)=>a.suggestion.distance-b.suggestion.distance).slice(0,3);
  }
  candidates.sort((a,b)=>Number(b.suggestion.recommended)-Number(a.suggestion.recommended)||a.suggestion.distance-b.suggestion.distance);
  return {cell,phone,reason,candidates};
 });
}
function renderPendingReview(){
 if(!data||!$('pendingReviewToggle'))return;
 const all=pendingReviewRows(),office=$('pendingReviewOffice').value||'all';
 const scope=all.filter(row=>office==='all'||row.cell.office===office),reason=$('pendingReviewReason').value||'all';
 $('pendingReviewToggle').textContent=`待匹配分类处理（${all.length}）`;
 $('pendingReviewReason').innerHTML=Object.entries(pendingReviewReasons).map(([key,label])=>`<option value="${key}">${label}（${key==='all'?scope.length:scope.filter(row=>row.reason===key).length}）</option>`).join('');$('pendingReviewReason').value=reason;
 const query=$('pendingReviewCell').value.trim().toUpperCase();
 const rows=scope.filter(row=>(reason==='all'||row.reason===reason)&&(!query||row.cell.source_cell.toUpperCase()===query));
 pendingReviewPage=Math.min(pendingReviewPage,Math.max(0,Math.ceil(rows.length/15)-1));
 $('pendingReviewSummary').textContent=`全库剩余 ${all.length} 格；当前条件 ${rows.length} 格。此列表使用独立筛选，不受上方账号筛选限制；确认关联后自动移除。`;
 $('pendingReviewRows').innerHTML=rows.slice(pendingReviewPage*15,pendingReviewPage*15+15).map(row=>{
  const {cell,candidates}=row,host=data.computers.find(c=>c.id===cell.computer_id);
  const choices=candidates.slice(0,4).map(({account:a,suggestion:s,positions:p,basis})=>`<div class="pending-candidate"><strong>${esc(accountDisplay(a))}</strong><small>${esc(a.account_type||'待确认')} · ${esc(a.status)} · ${esc(a.phone_device_name||a.phone_device_id||'登录手机未确认')}</small><small>${esc(basis)} · ${s.distance===0?'规范化名称一致':'名称差 '+s.distance+' 字'}${s.recommended?' · 近似建议':' · 需要人工核实'}</small><small>${p.length?'已有安排：'+p.map(c=>esc(data.computers.find(h=>h.id===c.computer_id)?.name||c.computer_id)+' / '+esc(c.source_cell)).join('；')+'，不可重复关联':'尚无发布位置'}</small></div>`).join('');
  return `<tr><td><strong>${esc(host?.name||cell.computer_id)} / ${esc(cell.source_cell)}</strong><small>发布设备 ${cell.slot} · ${esc({one:'办公室一',two:'办公室二',three:'办公室三'}[cell.office]||'待分配')}</small><small>手机序号：${esc(row.phone||'未识别')}</small></td><td>${esc(cell.label)}<small>原原因：${esc(cell.reason||'未登记')}</small></td><td><span class="badge warn">${esc(pendingReviewReasons[row.reason])}</span></td><td>${choices||'<small>没有可靠候选。可进入关联窗口按编号、昵称或抖音号搜索。</small>'}${candidates.length>4?`<small>另有 ${candidates.length-4} 个同机账号，进入关联窗口查看。</small>`:''}</td><td><button class="button" data-match-computer="${esc(cell.computer_id)}" data-match-slot="${cell.slot}" ${hasPermission('publishing_manage')?'':'disabled title="需要管理发布格子关联权限"'}>核对并关联</button></td></tr>`;
 }).join('')||'<tr><td colspan="5">当前条件没有待匹配格。</td></tr>';
 $('pendingReviewPage').textContent=`${rows.length?pendingReviewPage+1:0} / ${Math.ceil(rows.length/15)} 页`;
 $('pendingReviewPrevious').disabled=pendingReviewPage===0;$('pendingReviewNext').disabled=(pendingReviewPage+1)*15>=rows.length;
}
document.addEventListener('board-ready',renderPendingReview);
document.addEventListener('DOMContentLoaded',()=>{
 $('pendingReviewToggle').onclick=()=>{const panel=$('pendingReviewPanel');panel.hidden=!panel.hidden;$('pendingReviewToggle').setAttribute('aria-expanded',String(!panel.hidden));renderPendingReview();};
 for(const id of ['pendingReviewOffice','pendingReviewReason','pendingReviewCell'])$(id).oninput=()=>{pendingReviewPage=0;renderPendingReview();};
 $('pendingReviewPrevious').onclick=()=>{pendingReviewPage--;renderPendingReview();};$('pendingReviewNext').onclick=()=>{pendingReviewPage++;renderPendingReview();};
 $('pendingReviewReset').onclick=()=>{$('pendingReviewOffice').value='all';$('pendingReviewReason').value='all';$('pendingReviewCell').value='';pendingReviewPage=0;renderPendingReview();};
});
