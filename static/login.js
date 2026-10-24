function loginDestination(search=location.search){return new URLSearchParams(search).get('next')==='/?office=three'?'/?office=three':'/workspace.html';}
let setupMode=false;
const input=id=>document.getElementById(id);
async function initializeLogin(){
 try{
  const response=await fetch('/api/auth/me');if(!response.ok)throw Error('登录服务暂时不可用');const state=await response.json();
  if(state.user){location.replace(loginDestination());return;}
  setupMode=!state.initialized;
  input('setupName').hidden=!setupMode;input('confirmField').hidden=!setupMode;input('loginConfirm').required=setupMode;
  input('loginTitle').textContent=setupMode?'创建你的总管理员账号':'登录工作台';
  input('loginSubmit').textContent=setupMode?'创建并进入工作台':'登录';
  input('loginPassword').autocomplete=setupMode?'new-password':'current-password';
  input('loginHint').textContent=setupMode?'首次设置后，只有这个总账号可以管理人员和分配权限。请保管好登录名和密码。':'每人独立登录，共用一份工作数据。';
  if(setupMode&&!state.can_setup){input('loginForm').hidden=true;input('loginHint').textContent='尚未设置总管理员。请在服务器本机打开桌面软件完成首次创建，其他电脑不能抢先注册。';}
 }catch(error){input('loginError').textContent=error.message;}
}
input('loginForm').onsubmit=async event=>{
 event.preventDefault();input('loginError').textContent='';
 if(setupMode&&input('loginPassword').value!==input('loginConfirm').value){input('loginError').textContent='两次密码不一致';return;}
 input('loginSubmit').disabled=true;
 try{
  const response=await fetch(setupMode?'/api/auth/setup':'/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:input('loginUsername').value,name:input('loginName').value,password:input('loginPassword').value})});
  const result=await response.json();if(!response.ok)throw Error(result.error||'登录失败');input('loginPassword').value='';input('loginConfirm').value='';location.replace(loginDestination());
 }catch(error){input('loginError').textContent=error.message;}finally{input('loginSubmit').disabled=false;}
};
initializeLogin();
