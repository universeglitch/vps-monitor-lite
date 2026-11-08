import contextlib
import unittest

from active import Active, ServiceError
from auth import Busy
from function import ResourceGate


class FakeMemory:
    def __init__(self): self.available = 100000000; self.pressure = False
    def sample(self): return {'available': self.available, 'pressure': self.pressure}


class ControlTests(unittest.TestCase):
    def test_missing_profile_denies(self):
        gate = ResourceGate(FakeMemory())
        with self.assertRaises(Busy):
            with gate('login'): pass

    def test_pressure_denies(self):
        mem = FakeMemory(); gate = ResourceGate(mem, {'login': 20, 'backup': 40}, 10)
        mem.pressure = True
        with self.assertRaises(Busy):
            with gate('login'): pass
        mem.pressure = False; mem.available = 5
        with self.assertRaises(Busy):
            with gate('backup'): pass

    def test_gate_releases_on_failure(self):
        gate = ResourceGate(FakeMemory(), {'login': 20}, 10)
        with self.assertRaises(ValueError):
            with gate('login'): raise ValueError()
        with gate('login'): self.assertTrue(gate.active['login'])
        self.assertFalse(gate.active['login'])

    def fixture(self):
        calls=[]; running={'value': False}; now=[0]
        def run(*args):
            calls.append(args)
            if args[0]=='show':
                return 0, 'LoadState=loaded\nActiveState='+('active' if running['value'] else 'inactive')+'\nSubState=dead\nRestart=no\n'
            if args[0]=='start': running['value']=True
            if args[0]=='stop': running['value']=False
            return 0, ''
        return Active(run, lambda: now[0]), calls, running, now

    def test_heartbeat_off_no_service_calls(self):
        a,c,r,n=self.fixture(); a.desired['xray']=True
        a.set_heartbeat(False); a.tick()
        self.assertEqual(c, [])

    def test_two_failures_before_restart(self):
        a,c,r,n=self.fixture(); a.desired['xray']=True; a.set_heartbeat(True)
        a.tick(); self.assertFalse(r['value'])
        n[0]=60; a.tick(); self.assertTrue(r['value'])
        self.assertEqual(sum(x[0]=='start' for x in c),1)

    def test_manual_stop_does_not_self_heal(self):
        a,c,r,n=self.fixture(); r['value']=True
        a.set_service('xray',False); a.set_heartbeat(True); a.tick(); a.tick()
        self.assertFalse(r['value'])
        self.assertFalse(any(x[0]=='start' for x in c))

    def test_unknown_service_denied(self):
        a,c,r,n=self.fixture()
        with self.assertRaises(ServiceError): a.set_service('arbitrary.service',True)
        self.assertEqual(c,[])


if __name__=='__main__': unittest.main()
