import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from auth import Store, AuthError
from function import XrayTransaction

class Gate:
    def __call__(self, kind): return contextlib.nullcontext()
    def checkpoint(self): pass

class Service:
    def __init__(self): self.calls=[]; self.fail=False
    def inspect(self,name): return {'installed':True,'active':True}
    def apply_xray(self,was_active):
        self.calls.append(was_active)
        if self.fail:
            self.fail=False
            raise RuntimeError('injected start failure')

class XrayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.path=root/'config.json';self.path.write_text(json.dumps({'inbounds':[{'tag':'user-existing','port':1234}],'outbounds':[{'protocol':'freedom'}]}))
        self.store=Store(root/'private');self.service=Service();self.tests=[]
        self.x=XrayTransaction(self.store,self.path,Gate(),lambda raw:self.tests.append(raw),self.service,lambda token:1)
        self.new={'vps-monitor-ws':{'tag':'vps-monitor-ws','protocol':'vless','port':10001}}
    def tearDown(self): self.store.close();self.tmp.cleanup()
    def test_no_confirmation_no_change(self):
        old=self.path.read_bytes()
        with self.assertRaises(AuthError):self.x.configure('token','invalid',self.new)
        self.assertEqual(old,self.path.read_bytes());self.assertEqual(self.service.calls,[])
    def test_commit_preserves_unrelated(self):
        permit=self.x.confirm('token')
        result=self.x.configure('token',permit['confirmation'],self.new)
        self.assertEqual(result['stage'],'COMMITTED')
        config=json.loads(self.path.read_bytes())
        self.assertEqual(config['inbounds'][0],{'tag':'user-existing','port':1234})
        self.assertEqual(config['outbounds'],[{'protocol':'freedom'}]);self.assertEqual(len(self.tests),1)
        with self.assertRaises(AuthError):self.x.configure('token',permit['confirmation'],self.new)
    def test_external_change_invalidates_confirmation(self):
        permit=self.x.confirm('token');self.path.write_text('{"inbounds":[],"changed":true}')
        old=self.path.read_bytes()
        with self.assertRaises(AuthError):self.x.configure('token',permit['confirmation'],self.new)
        self.assertEqual(old,self.path.read_bytes());self.assertEqual(self.service.calls,[])
    def test_start_failure_rolls_back_once(self):
        old=self.path.read_bytes();self.service.fail=True
        permit=self.x.confirm('token')
        with self.assertRaises(RuntimeError):self.x.configure('token',permit['confirmation'],self.new)
        self.assertEqual(old,self.path.read_bytes());self.assertEqual(self.x.pending()['stage'],'ROLLED_BACK')
        self.assertEqual(len(self.service.calls),2)
    def test_existing_unowned_tag_rejected(self):
        self.path.write_text(json.dumps({'inbounds':[self.new['vps-monitor-ws']]}))
        permit=self.x.confirm('token')
        with self.assertRaises(AuthError):self.x.configure('token',permit['confirmation'],self.new)
    def test_test_failure_does_not_publish(self):
        old=self.path.read_bytes()
        def fail(raw):raise RuntimeError('invalid config')
        self.x.test=fail;permit=self.x.confirm('token')
        with self.assertRaises(RuntimeError):self.x.configure('token',permit['confirmation'],self.new)
        self.assertEqual(old,self.path.read_bytes());self.assertEqual(self.service.calls,[])
