import contextlib
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from auth import Auth, AuthError, Store
from function import Coordinator, PanelBackend, XrayTransaction
from active import Active
import panel

P='synthetic-test-password-123'
class Gate:
    def __call__(self,kind):return contextlib.nullcontext()
    def checkpoint(self,*a):pass
class FakeService:
    readiness=lambda *a:None
    def __init__(self):self.calls=[]
    def snapshot(self):return {'services':{'xray':{'installed':True,'active':True},'cloudflared':{'installed':False,'active':False}},'heartbeat':False}
    def inspect(self,name):return {'installed':True,'active':True}
    def apply_xray(self,was_active,rollback=False):self.calls.append(('apply',was_active,rollback))
    def set_service(self,name,enabled):self.calls.append((name,enabled));return {}

class PanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.a=Auth(self.root/'vault',gate=Gate());self.a.initialize(P,P,local_authority=True);self.token=self.a.login(P,source='test')
        self.service=FakeService();self.config=self.root/'xray.json'
        self.inbound={'tag':'vps-monitor-ws','listen':'127.0.0.1','port':19191,'protocol':'vless','settings':{'clients':[{'id':'11111111-1111-4111-8111-111111111111'}]},'streamSettings':{'network':'ws','security':'none','wsSettings':{'path':'/actual'}}}
        self.config.write_text(json.dumps({'inbounds':[self.inbound],'outbounds':[{'protocol':'freedom'}]}))
        self.metadata=self.root/'panel-config.json';self.metadata.write_text(json.dumps({'ws':{'host':'example.test','port':443,'listen_port':19191}}))
        self.x=XrayTransaction(self.a.store,self.config,Gate(),lambda raw:None,self.service,self.a.validate);self.x.single_writer=True
        self.c=Coordinator(self.a,self.service,Gate(),xray=self.x)
        self.backend=PanelBackend(self.c,self.metadata,18999)
    def tearDown(self):self.a.close();self.tmp.cleanup()
    def test_import_no_legacy_auth_files_and_no_listener(self):
        style,html,script=panel.render_components()
        self.assertIn('访客网络信息',html);self.assertIn('browserTargets',script)
        self.assertNotIn('loginForm',html);self.assertNotIn('main.PAGE',Path(panel.__file__).read_text())
        self.assertNotIn('ThreadingHTTPServer((',Path(panel.__file__).read_text())
    def test_default_persisted(self):
        self.backend.preferences(self.token,'panel')
        b=PanelBackend(self.c,self.metadata)
        self.assertEqual(b.preferences(self.token)['default_panel'],'panel')
        self.assertEqual(self.service.calls,[])
    def test_node_hidden_without_real_config(self):
        self.config.write_text('{"inbounds":[]}')
        self.assertEqual(self.backend.nodes(self.token),[])
    def test_name_changes_only_metadata(self):
        before=self.config.read_bytes();node=self.backend.nodes(self.token)[0]
        self.backend.rename(self.token,node['id'],'用户节点')
        self.assertEqual(self.backend.nodes(self.token)[0]['name'],'用户节点')
        self.assertEqual(before,self.config.read_bytes())
    def test_no_secret_or_qr_preload(self):
        nodes=self.backend.nodes(self.token);self.assertNotIn('uri',nodes[0])
        script=panel.render_components()[2]
        self.assertNotIn('?action=qr',script)
        self.assertIn("image.removeAttribute('src')",script)
        self.assertIn('二维码需点击眼睛',panel.render_components()[1])
        self.assertEqual(self.service.calls,[])
    def test_qr_on_demand_auth(self):
        node=self.backend.nodes(self.token)[0]
        qr=self.backend.qr(self.token,node['id']);self.assertTrue(qr)
        with self.assertRaises(AuthError):self.backend.qr('invalid',node['id'])
    def test_unowned_port_rejected(self):
        node=self.backend.nodes(self.token)[0]
        with self.assertRaises(AuthError):self.backend.port_preview(self.token,node['id'],19192)
    def test_preview_candidate_bound_and_cancel_no_change(self):
        from auth import dumps
        self.a.store.write('xray-owned.json',dumps(['vps-monitor-ws']))
        before=self.config.read_bytes();node=self.backend.nodes(self.token)[0]
        with mock.patch.object(self.backend,'check_port'):
            p=self.backend.port_preview(self.token,node['id'],19192)
        self.assertEqual(before,self.config.read_bytes());self.assertEqual(self.service.calls,[])
        self.backend.apply_candidate(self.token,p['confirmation'])
        self.assertEqual(json.loads(self.config.read_text())['inbounds'][0]['port'],19192)
        with self.assertRaises(AuthError):self.backend.apply_candidate(self.token,p['confirmation'])
    def test_unsupported_protocol_no_change(self):
        with self.assertRaises(AuthError):self.backend._inbound('ws+reality',{})
    def test_cloud_guide_does_not_mutate(self):
        before=self.metadata.read_bytes();d=self.backend.guide(self.token)
        self.assertIn('example.test -> HTTP -> 127.0.0.1:19191',d['ws_rule'])
        self.assertIn('18999',d['panel_rule']);self.assertEqual(before,self.metadata.read_bytes())
