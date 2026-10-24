// Each input searches one business field; multiple inputs are combined with AND.
const accountSearchFields=['query','accountIdQuery','accountDeviceQuery','accountDouyinQuery','accountPhoneQuery'];
const cardSearchFields=['cardQuery','cardLocationQuery','cardOwnerQuery','cardLabelQuery','cardAccountNameQuery'];
const deviceSearchFields=['deviceQuery','deviceNameQuery','deviceOwnerQuery','deviceLocationQuery','deviceLinkedIdQuery','deviceLinkedNameQuery','deviceLinkedDouyinQuery'];
function searchValue(id){return $(id).value.trim().toLowerCase();}
function containsText(value,query){return !query||String(value??'').toLowerCase().includes(query);}
function exactText(value,query){return !query||String(value??'').trim().toLowerCase()===query;}
function deviceNumberMatches(value,query){
 if(!query)return true;
 if(!value)return false;
 if(/^\d+$/.test(query))return /^\d+$/.test(String(value).split(':').pop())&&Number(String(value).split(':').pop())===Number(query);
 return exactText(value,query);
}
function locationNumberMatches(value,query){
 if(!query)return true;
 if(/^\d+$/.test(query))return (String(value??'').match(/\d+/g)||[]).some(number=>Number(number)===Number(query));
 return containsText(value,query);
}
function accountSearchMatches(account){return containsText(account.name,searchValue('query'))&&exactText(account.id,searchValue('accountIdQuery').replace(/^#/,''))&&deviceNumberMatches(account.phone_device_id,searchValue('accountDeviceQuery'))&&exactText(account.douyin_id,searchValue('accountDouyinQuery'))&&exactText(account.registration_phone,searchValue('accountPhoneQuery'));}
function clearAccountSearch(){accountSearchFields.forEach(id=>$(id).value='');if($('verificationFilter'))$('verificationFilter').value='';}
