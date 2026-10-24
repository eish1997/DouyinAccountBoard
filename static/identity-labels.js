/* User-facing account identity; database keys remain private to associations. */
function accountDisplay(account){
  return [account.name||'未命名账号',account.douyin_id?'抖音号 '+account.douyin_id:'抖音号待补充',account.phone_device_name||''].filter(Boolean).join(' · ');
}
