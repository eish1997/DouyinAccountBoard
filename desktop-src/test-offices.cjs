const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const elements={};const pageClasses=new Set();
const element=id=>elements[id]??=( {value:'',innerHTML:'',textContent:'',hidden:false,addEventListener(){},classList:{add(){},remove(){}}});
const context={location:{search:''},URLSearchParams,document:{body:{classList:{toggle(name,enabled){enabled?pageClasses.add(name):pageClasses.delete(name);}}},getElementById:element,addEventListener(){},querySelector:()=>element('header'),querySelectorAll:()=>[]},window:{addEventListener(){}},setInterval(){},fetch:()=>new Promise(()=>{}),applyPermissions(){},renderPhoneFilter(){},phoneDeviceMatches:()=>true,lifecyclePresetMatches:()=>true,lifecycleSummary(){},lifecycleDetails:()=>'',activeLifecyclePreset:''};
vm.createContext(context);
for(const file of ['field-search.js','offices.js','publishing-layout.js','verification.js','app.js'])vm.runInContext(fs.readFileSync(path.join(__dirname,'../static',file),'utf8'),context);
for(const phone of ['1','300','701','9999'])assert.equal(context.accountOffice({phone_device_id:phone}),'one');
for(const phone of ['301','700'])assert.equal(context.accountOffice({phone_device_id:phone}),'two');
for(const phone of [null,undefined,'','0','-1','圆','301多开','1.5'])assert.equal(context.accountOffice({phone_device_id:phone}),'pending');
vm.runInContext(`data={accounts:[
 {id:'301',phone_device_id:'1',computer_id:'shared',slot:1,status:'正常',published:true},
 {id:'1',phone_device_id:'301',computer_id:'shared',slot:2,status:'正常'},
 {id:'2',phone_device_id:'700',computer_id:'second',slot:1,status:'封禁'},
 {id:'3',phone_device_id:'701',computer_id:'unassigned',slot:null,status:'正常'},
 {id:'4',phone_device_id:null,computer_id:'unknown',slot:1,status:'正常'}
 ],computers:['shared','second','unknown','unassigned'].map(id=>({id,name:id})),date:'2026-10-17',today:'2026-10-17',collection:{},backup:{}};`,context);
assert.equal(context.accountOffice({phone_device_id:'office3:1'}),'three');
assert.equal(context.accountOffice({phone_device_id:null,office_group:'three'}),'three');
const run=code=>vm.runInContext(code,context);
context.hasPermission=()=>false;
assert.deepEqual(Array.from(context.filtered(),a=>a.id),['301','3']);
run("selected.add('1');render()");assert.equal(pageClasses.has('board-fit'),true);
assert.equal(run('selected.size'),0);
assert.match(element('matrixBody').innerHTML,/shared/);
assert.doesNotMatch(element('matrixBody').innerHTML,/second|unknown/);
assert.match(element('matrixBody').innerHTML,/其他办公室账号/);
assert.match(element('scope').textContent,/1 台电脑 \/ 1 个账号/);
run("activeOffice='two';render()");
assert.deepEqual(Array.from(context.filtered(),a=>a.id),['1','2']);
assert.match(element('scope').textContent,/2 台电脑 \/ 2 个账号/);
run("activeOffice='pending';render()");
assert.deepEqual(Array.from(context.filtered(),a=>a.id),['4']);
run("activeOffice='all';page='accounts';render()");
assert.equal(context.filtered().length,5);
element('status').value='封禁';assert.deepEqual(Array.from(context.filtered(),a=>a.id),['2']);
element('status').value='';
run("data.accounts.push({id:'5',phone_device_id:'office3:1',office_group:'three',computer_id:'office3:2',slot:1,status:'正常'});data.computers.push({id:'office3:2',name:'办公室三 · 2号电脑'});activeOffice='three';render()");
assert.deepEqual(Array.from(context.filtered(),a=>a.id),['5']);
assert.match(element('matrixBody').innerHTML,/办公室三 · 2号电脑/);
assert.match(element('scope').textContent,/办公室三.*1 台电脑 \/ 1 个账号/);
run("activeOffice='one';page='board';data.publishing_layout={enabled:true,cells:[{computer_id:'shared',slot:3,account_id:'301',label:'1-发布位置以工作表为准',office:'one',source_cell:'J2'},{computer_id:'shared',slot:9,account_id:null,label:'',office:'pending',source_cell:'P2'},{computer_id:'shared',slot:1,account_id:null,label:'99-身份待核对',office:'one',source_cell:'H2',reason:'昵称未匹配'}]};render()");
assert.equal(context.filtered()[0].slot,3);
assert.match(element('matrixBody').innerHTML,/待匹配账号/);
assert.match(element('matrixHead').innerHTML,/发布设备 9/);
assert.match(element('scope').textContent,/1 个格子待匹配/);
console.log('办公室边界、待分配、跨办公室同主机、统计、隐藏位置及选择范围验证通过');

run("page='accounts';render()");assert.equal(pageClasses.has('board-fit'),false);
