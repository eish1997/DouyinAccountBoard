document.addEventListener('DOMContentLoaded',()=>{
 $('exportVisibleAccounts').onclick=async()=>{const button=$('exportVisibleAccounts');button.disabled=true;try{const result=await api('/api/workspace/export',{kind:'accounts',ids:filtered().map(account=>account.id),format:'xlsx',date:data.date}),bytes=Uint8Array.from(atob(result.content),value=>value.charCodeAt(0)),url=URL.createObjectURL(new Blob([bytes],{type:result.mime})),link=document.createElement('a');link.href=url;link.download=result.filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}catch(error){toast(error.message);}finally{button.disabled=!hasPermission('data_export');}};
});
let openedWorkspaceLink=false;
document.addEventListener('board-ready',()=>{
 $('exportVisibleAccounts').disabled=!hasPermission('data_export');
 if(openedWorkspaceLink)return;openedWorkspaceLink=true;
 const params=new URLSearchParams(location.search),account=params.get('account_id'),card=params.get('card_id'),device=params.get('device_id'),computer=params.get('match_computer');
 if(account&&data.accounts.some(item=>item.id===account)){activeOffice='all';clearAccountSearch();page='accounts';render();edit(account);}
 else if(card&&data.phone_cards.cards.some(item=>String(item.id)===card)){page='cards';render();editCard(Number(card));}
 else if(device&&data.phone_devices.devices.some(item=>item.id===device)){page='phones';render();editPhoneDevice(device);}
 else if(computer){activeOffice='all';clearAccountSearch();page='board';render();openPublishingMatch(computer,Number(params.get('match_slot')));}
});
