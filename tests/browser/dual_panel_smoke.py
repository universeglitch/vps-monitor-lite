"""Development-only browser harness, mocks services and uses a temporary vault."""
import contextlib, json, tempfile, threading, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from pathlib import Path
from auth import Auth,dumps
from function import Coordinator,XrayTransaction,PanelBackend
from main import Server,Handler
from playwright.sync_api import sync_playwright
class Gate:
 def __call__(self,k):return contextlib.nullcontext()
 def checkpoint(self,*a):pass
class Service:
 readiness=lambda *a:None
 def snapshot(self):return {'services':{'xray':{'installed':True,'active':True},'cloudflared':{'installed':False,'active':False}},'heartbeat':False}
 def inspect(self,n):return {'installed':True,'active':True}
 def set_heartbeat(self,v):pass
 def apply_xray(self,*a,**kw):raise AssertionError('browser smoke must not restart')
with tempfile.TemporaryDirectory() as temp:
 root=Path(temp);a=Auth(root/'vault',gate=Gate());a.initialize('synthetic-browser-password','synthetic-browser-password',local_authority=True)
 token=a.login('synthetic-browser-password',source='test')
 config=root/'xray.json'; config.write_text(json.dumps({'inbounds':[{'tag':'user-ws','port':10000,'protocol':'vless','settings':{'clients':[{'id':'11111111-1111-4111-8111-111111111111'}]},'streamSettings':{'network':'ws','wsSettings':{'path':'/test'}}}]}))
 meta=root/'panel.json';meta.write_text(json.dumps({'ws':{'host':'example.test','port':443,'listen_port':10000}}))
 c=Coordinator(a,Service(),Gate());c.xray=XrayTransaction(a.store,config,Gate(),lambda x:None,c.active,a.validate)
 server=Server(('127.0.0.1',0),Handler,c,'https://example.test');server.panel_backend=PanelBackend(c,meta,server.server_port)
 # Avoid external network diagnostics; preserve real UI and HTTP authentication.
 real=server.panel_backend.detail
 server.panel_backend.detail=lambda t,action,headers: {'dns':'未测试','tcp':'未测试','https':'未测试'} if action=='serverdiag' else {'ip':'test','note':'browser harness'} if action=='visitor' else real(t,action,headers)
 threading.Thread(target=server.serve_forever,daemon=True).start()
 errors=[];requests=[]
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True)
  ctx=browser.new_context()
  ctx.add_cookies([{'name':'__Host-vps_session','value':token,'url':'https://example.test/','secure':True,'httpOnly':True,'sameSite':'Strict'}])
  def proxy(route):
   req=route.request;from urllib.parse import urlsplit
   if urlsplit(req.url).netloc!='example.test':return route.abort()
   import http.client
   conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
   h=dict(req.headers);h['Host']='example.test'
   if req.method=='POST':h['Origin']='https://example.test'
   conn.request(req.method,urlsplit(req.url).path or '/',body=req.post_data_buffer,headers=h)
   res=conn.getresponse();body=res.read();headers={k:v for k,v in res.getheaders() if k.lower() not in ('transfer-encoding','content-length')};route.fulfill(status=res.status,headers=headers,body=body);conn.close()
  ctx.route('**/*',proxy)
  page=ctx.new_page();page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:requests.append(r.url))
  page.goto('https://example.test/');page.wait_for_function("() => document.getElementById('app').hidden===false")
  page.locator('#modules').click();page.get_by_role('button',name='Cloudflare 配置指导').click();page.get_by_role('heading',name='Cloudflare Tunnel 需要你自行配置').wait_for();page.locator('#close').click()
  page.locator('#detail').click();page.wait_for_timeout(700)
  assert page.url=='https://example.test/'
  assert page.locator('#detailHost').is_visible()
  assert not any('/node/qr' in r for r in requests), 'QR prefetched without reveal'
  page.get_by_role('button',name='◉ 显示二维码').click()
  page.wait_for_function("() => document.getElementById('detailHost').shadowRoot.querySelector('.qr').hasAttribute('src')")
  assert any('/node/qr' in r for r in requests)
  page.get_by_role('button',name='◉ 隐藏二维码').click()
  assert page.evaluate("!document.getElementById('detailHost').shadowRoot.querySelector('.qr').hasAttribute('src')")
  page.get_by_role('button',name='设为默认面板',exact=True).last.click()
  page.wait_for_timeout(200)
  page.get_by_role('button',name='切换面板',exact=True).click();page.wait_for_timeout(700)
  assert page.locator('#app').is_visible();assert page.url=='https://example.test/'
  page.reload();page.wait_for_timeout(1000)
  assert page.locator('#detailHost').is_visible(), 'default panel not restored'
  # Screenshots intentionally optional: node connection information is sensitive.
  assert not errors,errors
  browser.close()
 server.shutdown();server.server_close();a.close()
 print('PASS same URL; one listener; authenticated detail mount; QR reveal/hide on demand; default persists; bidirectional switch; no JS exceptions')
