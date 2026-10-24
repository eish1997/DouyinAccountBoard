// Synthetic DOM-only checks; no browser profile, server, database or external network.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const fields={},field=id=>fields[id]??={textContent:'',disabled:false,hidden:false};
const state={can_manage:true,collection:{enabled:false,worker_online:false,worker_status:'disabled',waiting:1,done:1,failed:1,paused:false}};
const context={document:{getElementById:field,visibilityState:'visible'},setInterval(){},fetch:async()=>({ok:true,json:async()=>state}),send:async()=>{throw Error('No write expected');}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/collection-panel.js','utf8'),context);
(async()=>{
  await context.refreshCollectionPanel();
  assert.match(field('workCollectionInfo').textContent,/采集暂未启用/);
  assert.equal(field('workCollectionPause').disabled,true);
  assert.equal(field('workCollectionResume').disabled,true);
  const collectButton={disabled:false},publishButton={disabled:false};
  const authContext={data:{user:{id:1,name:'合成管理员',role:'owner',permissions:['collection_manage','publish']},collection:{enabled:false}},
    $:field,document:{addEventListener(){},querySelectorAll(selector){return selector==='[data-collect]'?[collectButton]:selector==='[data-publish]'?[publishButton]:[];}}};
  vm.createContext(authContext);
  vm.runInContext(fs.readFileSync('static/auth-ui.js','utf8'),authContext);
  authContext.applyPermissions();
  for(const id of ['pauseCollect','cancelCollect','collectSelected','collectVisible','autoCollect','resumeCollect','openCollector'])assert.equal(field(id).disabled,true);
  assert.equal(collectButton.disabled,true);
  assert.equal(publishButton.disabled,false);
  assert.equal(field('autoCollect').checked,false);
  assert.match(field('collectionInfo').textContent,/采集暂未启用/);
  console.log('PASS: disabled collection notice and controls (synthetic DOM)');
})().catch(error=>{console.error(error);process.exitCode=1;});
