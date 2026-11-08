import contextlib
import http.client
import json
import os
import tempfile
import threading
import unittest
from auth import Auth
from active import Active
from function import Coordinator
from main import Server, Handler

class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.a=Auth(os.path.join(self.tmp.name,'vault'),gate=lambda k:contextlib.nullcontext())
        self.a.initialize('synthetic-test-password-123','synthetic-test-password-123',local_authority=True)
        self.s=Server(('127.0.0.1',0),Handler,Coordinator(self.a,Active(run=lambda *a:(0,'LoadState=not-found\n')),None),'https://example.test')
        self.thread=threading.Thread(target=self.s.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.s.shutdown();self.s.server_close();self.a.close();self.tmp.cleanup()
    def request(self,path,data=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.s.server_port,timeout=3)
        h={'Host':'example.test','Origin':'https://example.test','Content-Type':'application/json'}
        h.update(headers or {})
        raw=json.dumps(data) if data is not None else None
        c.request('POST' if data is not None else 'GET',path,body=raw,headers=h)
        r=c.getresponse();body=r.read();out=(r.status,dict(r.getheaders()),body);c.close();return out
    def test_unauthenticated_direct_status_denied(self):
        self.assertEqual(self.request('/status')[0],401)
    def test_origin_rejected(self):
        self.assertEqual(self.request('/login',{'password':'synthetic-test-password-123'},{'Origin':'https://attacker.test'})[0],403)
    def test_session_and_csrf(self):
        status,h,b=self.request('/login',{'password':'synthetic-test-password-123'})
        self.assertEqual(status,200)
        cookie=h['Set-Cookie'];self.assertIn('HttpOnly; Secure; SameSite=Strict',cookie)
        cookie=cookie.split(';')[0]
        status,h,b=self.request('/session',headers={'Cookie':cookie});self.assertEqual(status,200)
        csrf=json.loads(b)['csrf']
        self.assertEqual(self.request('/api/create',{'name':'test'},{'Cookie':cookie})[0],403)
        status,h,b=self.request('/api/create',{'name':'test'},{'Cookie':cookie,'X-CSRF-Token':csrf})
        self.assertEqual(status,200);self.assertIn('key',json.loads(b));self.assertEqual(h['Cache-Control'],'no-store')
    def test_bootstrap_not_exposed(self):
        self.assertEqual(self.request('/initialize',{})[0],403)

class DualEntryTests(WebTests):
    def authenticated(self):
        _,h,_=self.request('/login',{'password':'synthetic-test-password-123'})
        cookie=h['Set-Cookie'].split(';')[0]
        _,_,b=self.request('/session',headers={'Cookie':cookie})
        return {'Cookie':cookie,'X-CSRF-Token':json.loads(b)['csrf']}
    def test_same_entry_no_panel_route(self):
        status,h,b=self.request('/')
        self.assertEqual(status,200);self.assertIn(b'mountDetailed',b)
        self.assertNotIn(b'<iframe',b)
        self.assertEqual(self.request('/panel')[0],401)
        self.assertEqual(self.request('/view',{'view':'panel'})[0],403)
        headers=self.authenticated()
        status,h,b=self.request('/view',{'view':'panel'},headers)
        self.assertEqual(status,200);self.assertIn('markup',json.loads(b))
        self.assertEqual(self.request('/preferences',{'default_panel':'panel'},headers)[0],200)
        status,h,b=self.request('/preferences',{},headers)
        self.assertEqual(json.loads(b)['default_panel'],'panel')
    def test_missing_config_hides_nodes(self):
        h=self.authenticated();status,_,b=self.request('/nodes',{},h)
        self.assertEqual(status,200);self.assertEqual(json.loads(b)['nodes'],[])
