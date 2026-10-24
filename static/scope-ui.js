// Explicit office selection; legacy local mode has no scope payload.
const ScopeUI=(()=>{
 const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 function model(board,kind,entity={}){
  const enabled=!!board?.user?.scope_mode,all=board?.user?.scope_mode==='all';
  const offices=board?.offices||[],existing=entity.id!==undefined&&entity.id!==''&&entity.id!==null;
  const chosen=existing?(entity.office_ids||[]):offices.length===1?[offices[0].id]:[];
  const permission=(existing?{accounts:'account_manage',cards:'card_manage',devices:'device_manage'}:{accounts:'account_create',cards:'card_create',devices:'device_create'})[kind];
  const writable=(board?.user?.role==='owner'||board?.user?.permissions?.includes(permission))&&(all||!existing||chosen.length>0&&chosen.every(id=>board.user.office_ids.includes(id)));
  return {enabled,all,offices,existing,chosen,writable,single:kind==='accounts'};
 }
 function mount(hostId,board,kind,entity={},options={}){
  const host=document.getElementById(hostId);if(!host)return;
  let field=host.querySelector('[data-office-picker]');
  const state=model(board,kind,entity);
  if(!state.enabled){if(field)field.remove();return;}
  if(!field){field=document.createElement('fieldset');field.dataset.officePicker=kind;const submit=host.querySelector('button[type="submit"]');if(submit)submit.before(field);else host.append(field);}
  field.dataset.officePicker=kind;
  field.disabled=!state.writable&&!options.importing;
  const ids=new Set(state.offices.map(o=>o.id));
  const entries=[...state.offices,...state.chosen.filter(id=>!ids.has(id)).map(id=>({id,name:'其他办公室（无授权）'}))];
  const chosen=state.existing&&!state.chosen.length&&state.all?['__pending__']:state.chosen;
  field.innerHTML=`<legend>${options.importing?'新增行的办公室（已有资料保留归属）':'资料所属办公室'}</legend><label>${state.single?'选择一个办公室':'可选择多个办公室，共用资料修改需全部范围授权'}<select data-office-select ${state.single?'':'multiple size="4"'} aria-label="资料所属办公室">${state.single?'<option value="">请选择办公室</option>':''}${state.all?`<option value="__pending__" ${chosen.includes('__pending__')?'selected':''}>待核对，暂不分配</option>`:''}${entries.map(o=>`<option value="${escape(o.id)}" ${chosen.includes(o.id)?'selected':''}>${escape(o.name)}</option>`).join('')}</select></label><p>归属不会随登录手机或编号自动变化。${field.disabled?'当前权限不能更改此资料归属。':''}</p>`;
 }
 function payload(hostId,{optional=false}={}){
  const field=document.getElementById(hostId)?.querySelector('[data-office-picker]');
  if(!field||field.disabled)return {};
  const values=[...field.querySelector('select').selectedOptions].map(o=>o.value).filter(Boolean);
  if(!values.length){if(optional)return {};throw Error('请明确选择资料所属办公室');}
  if(values.includes('__pending__')&&values.length>1)throw Error('待核对不能与办公室同时选择');
  const offices=values.filter(v=>v!=='__pending__');
  return field.dataset.officePicker==='accounts'?{office_id:offices[0]||null}:{office_ids:offices};
 }
 return Object.freeze({model,mount,payload});
})();
