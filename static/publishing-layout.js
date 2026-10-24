function publishingEnabled(){return !!data.publishing_layout?.enabled;}
function actualPublishingText(account){const cells=(data.publishing_layout?.cells||[]).filter(cell=>cell.account_id===account.id);return cells.length?cells.map(cell=>esc(data.computers.find(host=>host.id===cell.computer_id)?.name||cell.computer_id)+' / 设备'+cell.slot).join('；'):'暂无实际发布安排';}
function pageAccounts(){
 if(page!=='board'||!publishingEnabled())return data.accounts;
 const byId=new Map(data.accounts.map(a=>[a.id,a]));
 return data.publishing_layout.cells.filter(c=>c.account_id&&byId.has(c.account_id)).map(c=>({...byId.get(c.account_id),computer_id:c.computer_id,slot:c.slot,publishing_cell:c.source_cell,publishing_label:c.label}));
}
function publishingCellOffice(cell){
 const account=data.accounts.find(a=>a.id===cell.account_id);
 return account?accountOffice(account):data.user?.scope_mode?(data.computers.find(c=>c.id===cell.computer_id)?.office_ids?.[0]||'pending'):cell.office;
}
function filteredPublishingCells(accounts){
 if(!publishingEnabled())return [];
 const visible=new Set(accounts.map(a=>a.id)),q=$('query').value.trim().toLowerCase();
 return data.publishing_layout.cells.filter(c=>{
  if(activeOffice!=='all'&&publishingCellOffice(c)!==activeOffice)return false;
  if($('computer').value&&$('computer').value!==c.computer_id)return false;
  if(c.account_id)return visible.has(c.account_id);
  if(['status','published','accountType','ageFilter','lifecycleFilter','loginPhoneFilter','accountIdQuery','accountDouyinQuery','accountPhoneQuery','verificationFilter'].some(id=>$(id)?.value))return false;
  const prefix=String(c.label||'').match(/^\s*[.。]?\s*(\d+)(?:号手机|多开(?:抖音)?|小号)?\s*[-—－–]\s*(.*)$/);
  return containsText(prefix?prefix[2]:c.label,q)&&deviceNumberMatches(prefix?prefix[1]:'',searchValue('accountDeviceQuery'));
 });
}
