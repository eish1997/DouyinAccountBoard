const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('static/login.js','utf8'),context={URLSearchParams,location:{search:''}};
vm.createContext(context);vm.runInContext(source.split('\n')[0],context);
assert.equal(context.loginDestination('?next=%2F%3Foffice%3Dthree'),'/?office=three');
for(const next of ['https://evil.example','//evil.example','javascript:alert(1)','/?office=one','/users.html'])assert.equal(context.loginDestination('?next='+encodeURIComponent(next)),'/workspace.html');
assert.equal(context.loginDestination(''),'/workspace.html');
assert.equal((source.match(/location.replace\(loginDestination\(\)\)/g)||[]).length,2);
assert.match(fs.readFileSync('static/app.js','utf8'),/login.html\?next=/);
console.log('办公室三登录返回/已有会话返回/普通登录保持/外站与脚本跳转拒绝通过');
