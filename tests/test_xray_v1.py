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
    def apply_xray(self,was_active,rollback=False):
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
    def test_pending_rejects_incomplete_or_forged_journal(self):
        # _rollback()/recover() trust old/candidate/meta/was_active/owned as direct
        # chmod/chown/restart/ownership inputs. A partial or tampered journal must be
        # rejected at read time instead of driving those privileged operations.
        import hashlib
        from auth import dumps, b64
        old=self.path.read_bytes();candidate=b'{"inbounds":[]}'
        base={'stage':'PREPARED','old_hash':hashlib.sha256(old).hexdigest(),
              'new_hash':hashlib.sha256(candidate).hexdigest(),'old':b64(old),
              'candidate':b64(candidate),'meta':[0o600,os.getuid(),os.getgid()],
              'attempts':0,'was_active':True,'owned':['vps-monitor-ws']}
        def journal(**override):
            record=dict(base);record.update(override)
            self.store.write('xray-transaction.json',dumps(record))
        journal();self.assertEqual(self.x.pending()['stage'],'PREPARED')
        broken={
            'missing old bundle':{'old':None},
            'missing candidate bundle':{'candidate':None},
            'missing owned list':{'owned':None},
            'old bundle does not match old_hash':{'old':b64(b'other')},
            'candidate bundle does not match new_hash':{'candidate':b64(b'other')},
            'metadata length':{'meta':[0o600,os.getuid()]},
            'metadata type':{'meta':[0o600,'0',os.getgid()]},
            'metadata mode range':{'meta':[0o7777,os.getuid(),os.getgid()]},
            'activity flag type':{'was_active':'yes'},
            'unknown owned tag':{'owned':['vps-monitor-ws','arbitrary']},
            'unsorted owned tags':{'owned':['vps-monitor-ws','vps-monitor-reality']},
            'duplicate owned tags':{'owned':['vps-monitor-ws','vps-monitor-ws']},
            'attempt overflow':{'attempts':2},
            'unknown stage':{'stage':'DONE'},
        }
        for name,change in broken.items():
            with self.subTest(name=name):
                journal(**change)
                with self.assertRaises(AuthError):
                    self.x.pending()
