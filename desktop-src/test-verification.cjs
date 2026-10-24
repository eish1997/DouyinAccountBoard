const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const elements={},field=id=>elements[id]??={value:'',innerHTML:''};
const context={hasPermission:()=>true,URLSearchParams,location:{search:''},$:field,document:{addEventListener(){}},phoneDeviceMatches:()=>true,lifecyclePresetMatches:()=>true,esc:v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),page:'accounts',data:{
 accounts:[{id:'43',name:'测试主号',status:'正常',phone_device_id:'29',computer_id:'3',douyin_id:'first'},{id:'50',name:'测试小号',status:'正常',phone_device_id:'329',computer_id:'3',douyin_id:'contains29'},{id:'58',name:'未登记账号',status:'正常',phone_device_id:'29',computer_id:'3'},{id:'65',name:'三办账号',status:'正常',phone_device_id:'office3:29',computer_id:'office3:3'}],
 nurture:{verification_states:['待检查','无需核对，可使用','核对处理中','核对已通过'],records:[{account_id:'43',verification_state:'核对处理中',verification_owner:'测试<负责人>',verification_note:'<script>alert(1)</script>'},{account_id:'50',verification_state:'待检查'},{account_id:'65',verification_state:'核对已通过',completed_on:'2026-10-18'}]},
 publishing_layout:{enabled:true,cells:[{computer_id:'3',slot:1,account_id:'43',label:'29-主号',office:'one'},{computer_id:'3',slot:2,account_id:'50',label:'329-小号',office:'two'},{computer_id:'3',slot:3,account_id:null,label:'29-待匹配',office:'one'}]}}};
vm.createContext(context);
for(const file of ['field-search.js','offices.js','publishing-layout.js','verification.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../static',file),'utf8'),context);
vm.runInContext("activeOffice='all'",context);
const app=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
vm.runInContext(app.slice(app.indexOf('function filtered(){'),app.indexOf('function render(){')),context);
context.renderVerificationFilter();assert.match(field('verificationFilter').innerHTML,/核对处理中/);assert.match(field('verificationFilter').innerHTML,/未登记/);
field('verificationFilter').value='核对处理中';assert.deepEqual(Array.from(context.filtered(),a=>a.id),['43']);
field('accountDeviceQuery').value='29';assert.deepEqual(Array.from(context.filtered(),a=>a.id),['43']);
vm.runInContext("activeOffice='two'",context);assert.equal(context.filtered().length,0);vm.runInContext("activeOffice='all'",context);
context.page='board';assert.deepEqual(Array.from(context.filteredPublishingCells(context.filtered()),cell=>cell.account_id),['43']);
assert.equal(context.filteredPublishingCells(context.filtered()).some(cell=>!cell.account_id),false);
const markup=context.verificationMarkup(context.data.accounts[0]);assert.match(markup,/核对：核对处理中/);assert.match(markup,/测试&lt;负责人&gt;/);assert.doesNotMatch(markup,/<script>/);
assert.match(context.verificationMarkup(context.data.accounts[0],true),/核对：核对处理中/);
context.data.nurture.records[0].verification_state='核对已通过';assert.equal(context.filtered().length,0);
field('accountDeviceQuery').value='';field('verificationFilter').value='核对已通过';context.page='accounts';assert.deepEqual(Array.from(context.filtered(),a=>a.id),['43','65']);
assert.doesNotMatch(context.verificationMarkup(context.data.accounts[2]),/核对：待检查/);
field('verificationFilter').value='未登记';assert.deepEqual(Array.from(context.filtered(),a=>a.id),['58']);
context.clearAccountSearch();assert.equal(field('verificationFilter').value,'');assert.equal(context.filtered().length,4);
assert.match(app,/verificationMatches\(a\)/);assert.match(app,/verificationMarkup\(a,true\)/);assert.match(app,/verificationMarkup\(a\)/);
console.log('核对同步显示/状态更新后筛选/独立字段与办公室交集/未登记不猜测/未匹配格排除/重置/HTML转义通过');

context.hasPermission=()=>false;
assert.match(context.verificationMarkup(context.data.accounts[0]),/disabled title="需要管理养号进度及截图权限"/);
assert.match(context.verificationMarkup(context.data.accounts[0],true),/data-verification="43" disabled/);
context.hasPermission=()=>true;
assert.match(context.verificationMarkup(context.data.accounts[0],true),/data-verification="43" >修改核对/);
assert.match(context.verificationMarkup(context.data.accounts[2],true),/data-verification="58" >修改核对/);
const versions=fs.readFileSync(path.join(__dirname,'../static/edit-versions.js'),'utf8');
assert.match(versions,/'verificationEditor'/);
assert.match(app,/!\$\('verificationEditor'\)\?\.open/);
console.log('核对编辑入口权限、备注转义、弹窗冻结版本及定时刷新避让通过');
