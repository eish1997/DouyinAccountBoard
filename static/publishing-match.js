let matchingCell=null,matchingBatch=null,matchingAccountId=null;
function publishingCandidateScope(account,cell=matchingCell){return (account.office_group==='three'||String(account.phone_device_id||'').startsWith('office3:'))===String(cell?.computer_id||'').startsWith('office3:');}
function publishingCandidatePositions(id){return data.publishing_layout.cells.filter(cell=>cell.account_id===id&&!(cell.computer_id===matchingCell.computer_id&&cell.slot===matchingCell.slot));}
// Similarity only ranks suggestions; account identity still needs explicit confirmation.
function publishingNameCore(value){return String(value||'').normalize('NFKC').replace(/^\s*[.。]?\s*\d+(?:号手机|多开(?:抖音)?|小号)?\s*[-—－–]/,'').replace(/[(（]\s*\d+\s*(?:个)?\s*[)）]\s*$/,'').replace(/[\s·,，.。_-]/g,'').toLowerCase();}
function publishingNameDistance(left,right){
 const a=Array.from(left),b=Array.from(right);let row=Array.from({length:b.length+1},(_,i)=>i);
 for(let i=0;i<a.length;i++){const next=[i+1];for(let j=0;j<b.length;j++)next.push(Math.min(next[j]+1,row[j+1]+1,row[j]+(a[i]===b[j]?0:1)));row=next;}
 return row[b.length];
}
function publishingSuggestion(account,cell=matchingCell){
 const label=String(cell?.label||''),prefix=label.match(/^\s*[.。]?\s*(\d+)(?:号手机|多开(?:抖音)?|小号)?\s*[-—－–]/);
 const left=publishingNameCore(label),right=publishingNameCore(account.name),distance=publishingNameDistance(left,right),length=Math.max(left.length,right.length);
 const samePhone=!!prefix&&deviceNumberMatches(account.phone_device_id,prefix[1]);
 const exact=!!left&&left===right,similar=Math.min(left.length,right.length)>=5&&distance<=2&&distance/length<=0.3;
 return {recommended:samePhone&&(exact||similar),samePhone,exact,distance,phone:prefix?.[1]||''};
}
function publishingCandidates(){return data.accounts.filter(account=>publishingCandidateScope(account)&&deviceNumberMatches(account.phone_device_id,searchValue('matchPhoneQuery'))&&exactText(account.id,searchValue('matchIdQuery').replace(/^#/,''))&&containsText(account.name,searchValue('matchNameQuery'))&&exactText(account.douyin_id,searchValue('matchDouyinQuery'))).sort((a,b)=>{const x=publishingSuggestion(a),y=publishingSuggestion(b);return Number(y.recommended)-Number(x.recommended)||(x.recommended&&y.recommended?x.distance-y.distance:0);});}
function renderPublishingCandidates(){
 matchingAccountId=null;$('matchSelection').textContent='请选择一个账号，再点击确认关联。';$('confirmPublishingMatch').disabled=true;
 const choices=publishingCandidates();$('matchCount').textContent=`找到 ${choices.length} 个账号，最多显示40个；可以继续填写条件缩小范围。同手机且名称相近的账号优先推荐；请核对后选择并确认，不会自动关联。`;
 $('matchCandidates').innerHTML=choices.slice(0,40).map(account=>{const positions=publishingCandidatePositions(account.id),suggestion=publishingSuggestion(account);return `<tr><td><input type="radio" name="publishingAccount" aria-label="选择账号 ${esc(account.name)}" value="${esc(account.id)}" ${positions.length?'disabled':''}></td><td><strong>${esc(accountDisplay(account))}</strong>${suggestion.recommended?`<small class="badge ${positions.length?'':'good'}">${positions.length?'名称相近 · 已有安排':suggestion.exact?'推荐核对 · 名称一致':'近似建议'} · 同手机${esc(suggestion.phone)}${suggestion.exact?'':' · 名称相差'+suggestion.distance+'字'}</small>`:''}<small>${esc(account.account_type||'待确认')} · ${esc(account.status)}</small></td><td>${esc(account.phone_device_name||account.phone_device_id||'未关联手机')}</td><td>${esc(account.douyin_id||'未填写')}</td><td>${positions.length?`已有安排：${positions.map(cell=>esc(cell.source_cell)).join('、')}，不可重复关联`:'可选择'}</td></tr>`;}).join('')||'<tr><td colspan="5">没有符合条件的账号。可清空手机序号等条件后再查找；不会自动新建账号。</td></tr>';
}
function openPublishingMatch(computer,slot){
 if(!hasPermission('publishing_manage'))return toast('当前账号没有管理发布格子关联权限');
 const cell=data.publishing_layout.cells.find(cell=>cell.computer_id===computer&&cell.slot===slot);
 if(!cell||!cell.label||(cell.account_id&&!cell.manual_link))return;
 matchingCell={...cell};matchingBatch=data.publishing_layout.batch;
 const host=data.computers.find(host=>host.id===computer),current=data.accounts.find(account=>account.id===cell.account_id);
 $('matchTitle').textContent=`${cell.source_cell} · ${cell.account_id?'调整手动关联':'关联待匹配账号'}`;
 $('matchOriginal').textContent=`${host?.name||computer} / 发布设备 ${slot} · 原文：${cell.label}`;
 $('matchCurrent').textContent=current?`当前关联：${accountDisplay(current)}`:`未匹配原因：${cell.reason||'需核对账号身份'}`;
 ['matchPhoneQuery','matchIdQuery','matchNameQuery','matchDouyinQuery'].forEach(id=>$(id).value='');
 const prefix=String(cell.label).match(/^\s*[.。]?\s*(\d+)(?:号手机|多开(?:抖音)?|小号)?\s*[-—－–]/);if(prefix)$('matchPhoneQuery').value=prefix[1];
 $('clearPublishingMatch').hidden=!cell.manual_link;$('matchError').textContent='';renderPublishingCandidates();$('publishingMatchDialog').showModal();captureVersions('publishingMatchDialog');
}
async function savePublishingMatch(action,button){
 if(action==='link'&&!matchingAccountId)return;
 button.disabled=true;$('matchError').textContent='';
 const requestKey='publishing-link:'+matchingCell.computer_id+':'+matchingCell.slot+':'+action;
 try{await api('/api/publishing/link',{batch:matchingBatch,computer_id:matchingCell.computer_id,slot:matchingCell.slot,expected_account_id:matchingCell.account_id,action,account_id:matchingAccountId,request_id:claimRequestId(requestKey)});settleRequestId(requestKey);$('publishingMatchDialog').close();await load();toast(action==='clear'?'已取消手动关联，恢复待匹配':'已关联，发布看板已更新');}catch(error){settleRequestId(requestKey,error);$('matchError').textContent=error.message;}finally{button.disabled=false;}
}
document.addEventListener('DOMContentLoaded',()=>{
 document.addEventListener('click',event=>{const button=event.target.closest('[data-match-computer]');if(button)openPublishingMatch(button.dataset.matchComputer,Number(button.dataset.matchSlot));});
 ['matchPhoneQuery','matchIdQuery','matchNameQuery','matchDouyinQuery'].forEach(id=>$(id).oninput=renderPublishingCandidates);
 $('resetMatchSearch').onclick=()=>{['matchPhoneQuery','matchIdQuery','matchNameQuery','matchDouyinQuery'].forEach(id=>$(id).value='');renderPublishingCandidates();};
 $('closePublishingMatch').onclick=()=>$('publishingMatchDialog').close();
 $('matchCandidates').onchange=event=>{if(event.target.name!=='publishingAccount')return;const account=data.accounts.find(account=>account.id===event.target.value);if(!account)return;matchingAccountId=account.id;$('matchSelection').textContent=`确认将 ${matchingCell.source_cell}「${matchingCell.label}」关联到「${account.name}」，抖音号 ${account.douyin_id||'未填写'}，登录手机 ${account.phone_device_id||'未关联'}。`;$('confirmPublishingMatch').disabled=false;};
 $('publishingMatchForm').onsubmit=event=>{event.preventDefault();savePublishingMatch('link',event.submitter);};
 $('clearPublishingMatch').onclick=()=>savePublishingMatch('clear',$('clearPublishingMatch'));
});
