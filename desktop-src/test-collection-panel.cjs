const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const fields={},field=id=>fields[id]??={textContent:'',disabled:true,hidden:false};
let state={can_manage:true,collection:{worker_online:true,worker_status:'待命',waiting:313,done:1,failed:0,paused:true,pause_reason:'手动暂停'}};
const calls=[],data={editor:'未保存内容'};
const ctx={data,document:{getElementById:field,visibilityState:'visible'},setInterval(){},fetch:async()=>({ok:true,json:async()=>state}),send:async(path,body)=>{calls.push([path,body]);}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('static/collection-panel.js','utf8'),ctx);
(async()=>{
 await ctx.refreshCollectionPanel();assert.match(field('workCollectionInfo').textContent,/313 个等待/);assert.match(field('workCollectionInfo').textContent,/已暂停/);
 assert.equal(field('workCollectionPause').disabled,false);assert.equal(field('workCollectionResume').hidden,false);
 await field('workCollectionPause').onclick();assert.equal(calls[0][0],'/api/collection/settings');assert.equal(calls[0][1].pause,true);
 state={...state,can_manage:false};await ctx.refreshCollectionPanel();assert.equal(field('workCollectionPause').disabled,true);assert.equal(field('workCollectionResume').disabled,true);
 assert.equal(ctx.data,data);assert.equal(data.editor,'未保存内容');
 const html=fs.readFileSync('static/workspace.html','utf8');assert.match(html,/id="workCollectionPause"[^>]*>暂停队列/);assert.match(html,/collection-panel.js\?v=20261020-clean/);
 console.log('今日工作页采集进度/暂停入口/继续/权限/保留编辑内容通过');
})().catch(error=>{console.error(error);process.exitCode=1;});
