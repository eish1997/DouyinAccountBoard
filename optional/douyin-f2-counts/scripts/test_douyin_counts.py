import json,tempfile,unittest
from pathlib import Path
from douyin_counts import load_accounts,search_identity,parse_user,IdentityMismatch,ProfileUnavailable
class CountsInputTest(unittest.TestCase):
 def load(self,values):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'accounts.json';p.write_text(json.dumps(values),encoding='utf-8');return load_accounts(p)
 def test_handle_input_and_leading_zeros(self):
  rows=self.load(['97350513058',{'account':'00123'},'97350513058']);self.assertEqual(len(rows),2);self.assertIsNone(rows[0]['sec_uid']);self.assertEqual(rows[1]['expected_account'],'00123')
 def test_url_and_internal_identifier_still_supported(self):
  rows=self.load(['https://www.douyin.com/user/MS4wABC_123','MS4wXYZ_456']);self.assertEqual(rows[0]['sec_uid'],'MS4wABC_123');self.assertEqual(rows[1]['sec_uid'],'MS4wXYZ_456')
 def test_bad_host_numeric_json_and_invalid_handle_rejected(self):
  for values in [[{'account':123}],['https://evil.example/user/MS4wABC_123'],['hello world']]:
   with self.assertRaises(ValueError):self.load(values)
 def test_search_requires_exact_handle_and_internal_identifier(self):
  data={'users':[{'user_info':{'unique_id':'97350513058','sec_uid':'MS4wABC_123'}},{'unique_id':'973505130580','sec_uid':'MS4wWRONG_1'},{'nickname':'97350513058','sec_uid':'MS4wWRONG_2'}]}
  self.assertEqual(search_identity(data,'97350513058'),{'MS4wABC_123'})
 def test_final_identity_and_zero_count(self):
  item={'sec_uid':'MS4wABC_123','expected_account':'97350513058'}
  data={'user':{'sec_uid':'MS4wABC_123','unique_id':'97350513058','aweme_count':0}}
  self.assertEqual(parse_user(data,item)['count'],0)
  data['user']['unique_id']='other'
  with self.assertRaises(IdentityMismatch):parse_user(data,item)
  data['user']['aweme_count']=None
  with self.assertRaises(ProfileUnavailable):parse_user(data,item)
class SearchBrowserTest(unittest.IsolatedAsyncioTestCase):
 async def test_hidden_verification_iframe_does_not_block_exact_match(self):
  from playwright.async_api import async_playwright
  from douyin_counts import resolve_handle
  async with async_playwright() as pw:
   browser=await pw.chromium.launch()
   page=await browser.new_page()
   body='<meta charset="utf-8"><input placeholder="搜索你感兴趣的内容"><iframe src="/captcha" style="display:none"></iframe><div><a href="https://www.douyin.com/user/MS4wTEST_123">测试用户</a><span>抖音号：97350513058</span></div>'
   async def route(request):await request.fulfill(body=body if '/search/' in request.request.url else '<html></html>',content_type='text/html')
   await page.route('https://www.douyin.com/**',route)
   result=await resolve_handle(page,'97350513058')
   self.assertEqual(result['sec_uid'],'MS4wTEST_123')
   await browser.close()
 async def test_server_verify_check_requires_manual_handling(self):
  from playwright.async_api import async_playwright
  from douyin_counts import resolve_handle,SearchNeedsVerification
  async with async_playwright() as pw:
   browser=await pw.chromium.launch();page=await browser.new_page()
   body='<meta charset="utf-8"><span>抖音号：</span><script>fetch("/aweme/v1/web/discover/search/")</script>'
   async def route(request):
    if '/aweme/' in request.request.url:await request.fulfill(json={'status_code':0,'user_list':[],'search_nil_info':{'search_nil_type':'verify_check'}})
    else:await request.fulfill(body=body,content_type='text/html')
   await page.route('https://www.douyin.com/**',route)
   with self.assertRaises(SearchNeedsVerification):await resolve_handle(page,'97350513058')
   await browser.close()
 async def test_successful_response_clears_earlier_verify_check(self):
  from playwright.async_api import async_playwright
  from douyin_counts import resolve_handle
  async with async_playwright() as pw:
   browser=await pw.chromium.launch();page=await browser.new_page()
   body='<meta charset="utf-8"><script>fetch("/aweme/search/first").then(r=>r.json()).then(()=>fetch("/aweme/search/second")).then(r=>r.json()).then(()=>document.body.insertAdjacentHTML("beforeend","<span>抖音号：</span>"))</script>'
   async def route(request):
    if request.request.url.endswith('/first'):await request.fulfill(json={'user_list':[],'search_nil_info':{'search_nil_type':'verify_check'}})
    elif request.request.url.endswith('/second'):await request.fulfill(json={'user_list':[{'user_info':{'unique_id':'97350513058','sec_uid':'MS4wTEST_123'}}]})
    else:await request.fulfill(body=body,content_type='text/html')
   await page.route('https://www.douyin.com/**',route)
   result=await resolve_handle(page,'97350513058')
   self.assertEqual(result['sec_uid'],'MS4wTEST_123')
   await browser.close()
 async def test_same_handle_multiple_identities_rejected(self):
  from playwright.async_api import async_playwright
  from douyin_counts import resolve_handle
  async with async_playwright() as pw:
   browser=await pw.chromium.launch();page=await browser.new_page()
   body='<meta charset="utf-8"><input placeholder="搜索你感兴趣的内容">'+''.join('<div><a href="https://www.douyin.com/user/'+uid+'">测试用户</a><span>抖音号：97350513058</span></div>' for uid in ['MS4wTEST_123','MS4wOTHER_456'])
   async def route(request):await request.fulfill(body=body,content_type='text/html')
   await page.route('https://www.douyin.com/**',route)
   with self.assertRaises(IdentityMismatch):await resolve_handle(page,'97350513058')
   await browser.close()
if __name__=='__main__':unittest.main()
