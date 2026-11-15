"""Stage C tests for resource_benchmark: measurement, statistics and authority.

These tests never launch a real systemd scope, never install, stop or restart
anything, and never run an installer operation on the host. A real VPS is only
required to observe BLOCKED; measurement is exercised through the fake meter and
the injectable fixture contract.
"""
import copy
import inspect
import io
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import resource_benchmark as rb
import resource_probe as rp

REPO = Path(__file__).resolve().parents[1]

DEFAULT_EVENTS = 'low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n'
CONTROLLERS = 'cpuset cpu io memory pids\n'


def _leaf(maximum=200000000, high=0, swap='0'):
    return {'path': '/x', 'dir': '/x', 'current': 1000, 'max': maximum,
            'max_unlimited': maximum is None, 'peak': 1200, 'peak_status': 'ok',
            'events': {'low': 0, 'high': 0, 'max': 0, 'oom': 0, 'oom_kill': 0},
            'events_low_present': True, 'high': high, 'high_status': 'ok' if high is not None else 'absent',
            'swap_max': None if swap == 'max' else int(swap), 'swap_max_status': 'unlimited' if swap == 'max' else 'ok',
            'subtree': [], 'subtree_status': 'absent'}


def make_doc(envelope, op='install-panel', samples=(1000, 1010, 1020, 1005, 1015)):
    """Build a self-consistent external measurement document for ``envelope``."""
    doc = {'schema': rb.EXTERNAL_SCHEMA,
           'schema_version': rb.EXTERNAL_SCHEMA_VERSION,
           'benchmark_version': rb.BENCHMARK_VERSION,
           'probe_version': rb.PROBE_VERSION_EXPECTED,
           'operation': op,
           'n': len(samples),
           'samples': list(samples),
           'operator': {'name': 'ops', 'contact': 'ops@example.invalid',
                        'collected_at': '2026-01-01T00:00:00Z'},
           'environment': {'benchmark_version': rb.BENCHMARK_VERSION,
                           'probe_version': rb.PROBE_VERSION_EXPECTED,
                           'valid': True,
                           'system': {'arch': 'x86_64', 'cpu_count': 4, 'page_size': 4096},
                           'meminfo': {'MemTotal': 2000000000, 'MemAvailable': 1500000000,
                                       'SwapTotal': 0, 'SwapFree': 0},
                           'cgroup': envelope}}
    doc['fingerprint'] = rb.environment_fingerprint(doc['environment'])
    doc['integrity'] = {'algorithm': 'sha256', 'digest': rb.external_digest(doc)}
    return doc


# ------------------------------------------------- capability test doubles --
# A capability is a real business entry point plus a verifiable result contract.
# The stand-in command below prints exactly the JSON result a real entry point is
# required to emit, so tests can exercise the contract without a deployed panel.
# The negative tests prove that a bare ``exit 0``, an empty command, a wrong
# entry point, a mismatched result or a shared meaningless success can never be
# accepted in its place.

def capability_argv(op):
    program = 'import json; print(json.dumps({"operation": %r, "ok": True}))' % (op,)
    return [sys.executable, '-c', program]


def capability_spec(op, argv=None, timeout=5, entrypoint=None, result=None):
    return {'entrypoint': rb.CAPABILITY_ENTRYPOINTS[op] if entrypoint is None else entrypoint,
            'argv': capability_argv(op) if argv is None else argv,
            'timeout': timeout,
            'result': ({'kind': 'json', 'op_field': 'operation', 'expect': {'ok': True}}
                       if result is None else result)}


def capability_caps(*ops):
    return dict((op, capability_spec(op)) for op in ops)


def panel_capabilities():
    """Capability specs for every capability-driven operation (test double)."""
    return capability_caps(*(rb.PANEL_OPS + (rb.MODULE_METADATA_OP,)))


def _closable():
    class _Handle:
        def close(self):
            pass
    return _Handle()


_STAT_FIELDS = {'mode': 0, 'ino': 1, 'dev': 2, 'nlink': 3, 'uid': 4, 'gid': 5,
                'size': 6, 'atime': 7, 'mtime': 8, 'ctime': 9}


def _mutate_stat(st, **fields):
    """Return a copy of a stat result with selected fields overridden."""
    values = list(st)
    for name, value in fields.items():
        values[_STAT_FIELDS[name]] = value
    return os.stat_result(values)


class Tree:
    """Builds a temporary /proc + cgroup2 tree for one namespace."""

    def __init__(self, root):
        self.root = Path(root)

    def put(self, rel, text):
        target = self.root / rel.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def level(self, rel, current=1000000, maximum=200000000, high=0, swap='0',
              subtree=None, peak=1200000, events=DEFAULT_EVENTS):
        base = rel.rstrip('/')
        self.put(base + '/memory.current', str(current))
        self.put(base + '/memory.max', 'max' if maximum is None else str(maximum))
        if events is not None:
            self.put(base + '/memory.events', events)
        if peak is not None:
            self.put(base + '/memory.peak', str(peak))
        if high is not None:
            self.put(base + '/memory.high', 'max' if high == 'max' else str(high))
        if swap is not None:
            self.put(base + '/memory.swap.max', str(swap))
        if subtree is not None:
            self.put(base + '/cgroup.subtree_control', subtree)

    def standard(self, mount_point='/sys/fs/cgroup',
                 self_path='/system.slice/node-panel.service', controllers=True):
        self.put('proc/self/mountinfo',
                 '29 23 0:26 / %s rw - cgroup2 cgroup2 rw\n' % mount_point)
        self.put('proc/self/cgroup', '0::%s\n' % self_path)
        self.put('proc/meminfo',
                 'MemTotal:        2000000 kB\nMemAvailable:    1500000 kB\n'
                 'SwapTotal:             0 kB\nSwapFree:              0 kB\n')
        pre = mount_point.rstrip('/')
        if controllers:
            self.put(pre + '/cgroup.controllers', CONTROLLERS)
        self.put(pre + '/cgroup.subtree_control', 'memory\n')
        self.level(pre + self_path, current=1000000, maximum=200000000, high=0,
                   swap='0', subtree=None, peak=1200000)
        self.level(pre + '/system.slice', current=5000000, maximum=None, high='max',
                   swap='max', subtree='memory\n')
        self.level(pre, current=9000000, maximum=None, high='max', swap='max', subtree=None)
        return self

    def scope(self, path='/system.slice/run-abc.scope', maximum=200000000, high=0, swap='0'):
        pre = '/sys/fs/cgroup'
        self.level(pre + path, current=1000000, maximum=maximum, high=high, swap=swap,
                   subtree=None, peak=1200000)
        return path

    def source(self):
        return rp.Source(self.root)


class BenchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def tree(self, **kwargs):
        return Tree(self.tmp.name).standard(**kwargs)


# ------------------------------------------------------------- increments ----

class IncrementTests(BenchBase):
    def test_basic_increment(self):
        self.assertEqual(rb.compute_increment(100, 250), 150)

    def test_increment_never_negative(self):
        self.assertEqual(rb.compute_increment(250, 100), 0)

    def test_increment_rejects_missing(self):
        with self.assertRaises(rb.BenchmarkError):
            rb.compute_increment(None, 10)
        with self.assertRaises(rb.BenchmarkError):
            rb.compute_increment(10, None)

    def test_increment_rejects_non_integer(self):
        with self.assertRaises(rb.BenchmarkError):
            rb.compute_increment(1.5, 3)
        with self.assertRaises(rb.BenchmarkError):
            rb.compute_increment(-1, 3)

    def test_increment_not_underestimate(self):
        # A high-water peak dominates every earlier reading.
        for pre in (0, 100, 500):
            for peak in (100, 400, 1000):
                self.assertGreaterEqual(rb.compute_increment(pre, peak), max(peak - pre, 0))


# ---------------------------------------------------------------- peak I/O ---

class PeakReadTests(BenchBase):
    def test_peak_ok(self):
        tree = self.tree()
        value, status = rb.read_peak_value(tree.source(), '/sys/fs/cgroup/system.slice/node-panel.service')
        self.assertEqual((value, status), (1200000, 'ok'))

    def test_peak_absent_is_not_zero(self):
        tree = self.tree()
        os.remove(str(Path(self.dir) / 'sys/fs/cgroup/system.slice/node-panel.service/memory.peak'))
        value, status = rb.read_peak_value(tree.source(), '/sys/fs/cgroup/system.slice/node-panel.service')
        self.assertIsNone(value)
        self.assertEqual(status, 'absent')

    def test_peak_malformed(self):
        tree = self.tree()
        Path(self.dir, 'sys/fs/cgroup/system.slice/node-panel.service/memory.peak').write_text('nope\n')
        value, status = rb.read_peak_value(tree.source(), '/sys/fs/cgroup/system.slice/node-panel.service')
        self.assertIsNone(value)
        self.assertEqual(status, 'malformed')


# ------------------------------------------------------------- statistics ----

class StatisticsTests(unittest.TestCase):
    def test_quantile_interpolates(self):
        self.assertEqual(rb._quantile([1, 2, 3, 4], 0.25), 1.75)
        self.assertEqual(rb._quantile([1, 2, 3, 4], 0.75), 3.25)
        self.assertEqual(rb._quantile([5], 0.5), 5.0)

    def test_tukey_rejects_extreme(self):
        kept, rejected = rb.tukey_filter([10, 10, 10, 10, 1000])
        self.assertEqual(rejected, [1000])
        self.assertEqual(kept, [10, 10, 10, 10])

    def test_tukey_small_sample_kept_whole(self):
        kept, rejected = rb.tukey_filter([1, 2, 3])
        self.assertEqual(kept, [1, 2, 3])
        self.assertEqual(rejected, [])

    def test_comp_ratio(self):
        self.assertEqual(rb.comp_ratio([10, 10, 10]), 0.0)
        self.assertAlmostEqual(rb.comp_ratio([10, 20]), 10 / 15.0)

    def test_drawdown_is_non_negative(self):
        self.assertEqual(rb.drawdown([100, 50, 80]), 50)
        self.assertEqual(rb.drawdown([10, 20, 30]), 0)

    def test_safety_discount(self):
        self.assertAlmostEqual(rb.safety_discount([100, 80, 100]), 0.2)
        self.assertEqual(rb.safety_discount([]), 0.0)

    def test_compute_constants(self):
        per_op = {'login': [1000, 1010, 1020], 'qr': [500, 500, 500]}
        idle = [0, 4]
        available = [150000000, 149999000, 150000000]
        constants = rb.compute_constants(per_op, idle, available)
        self.assertEqual(constants['comp_margin'], 4)
        self.assertEqual(constants['drawdown'], 1000)
        self.assertEqual(constants['comp_reserve_margin'], 1004)
        self.assertAlmostEqual(constants['comp_ratio'], 20 / 1010.0)

    def test_op_budget_never_underestimates(self):
        constants = {'comp_ratio': 0.0, 'comp_margin': 0}
        self.assertGreaterEqual(rb.op_budget([100, 100, 100, 100, 100], constants), 100)
        self.assertEqual(rb.op_budget([100, 100, 100, 100, 100], constants), 100)

    def test_available_floor_scales_minimum(self):
        self.assertEqual(rb.available_floor([100, 80], 0.2), 64)
        with self.assertRaises(rb.BenchmarkError):
            rb.available_floor([], 0.2)

    def test_feasibility_worst_case(self):
        profiles = dict((op, 1000) for op in rb.ALL_OPS)
        profiles['login'] = 2000
        profiles['qr'] = 5000
        result = rb.feasibility(profiles, 3000, 100000)
        self.assertEqual(result['required'], 3000 + 5000 + 2000)
        self.assertTrue(result['feasible'])

    def test_rejection_thresholds(self):
        self.assertEqual(rb.rejection_reasons({'comp_ratio': 0.5, 'safety_discount': 0.2}), [])
        reasons = rb.rejection_reasons({'comp_ratio': 2.0, 'safety_discount': 0.9})
        self.assertEqual(len(reasons), 2)


# -------------------------------------------------------- cgroup envelopes ---

class EnvelopeTests(BenchBase):
    def test_envelope_shape(self):
        tree = self.tree()
        env = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        self.assertTrue(env['valid'])
        self.assertEqual(env['mount_root'], '/')
        self.assertEqual(env['mount_point'], '/sys/fs/cgroup')
        self.assertEqual(len(env['levels']), 3)
        leaf = env['levels'][0]
        self.assertEqual(leaf['max'], 200000000)
        self.assertEqual(leaf['high'], 0)
        self.assertEqual(leaf['swap_max'], 0)
        self.assertEqual(env['controllers']['controllers_status'], 'ok')
        self.assertIn('memory', env['controllers']['controllers'])

    def test_envelope_rejects_escape(self):
        tree = self.tree()
        with self.assertRaises(rp.ProbeError):
            rb.read_envelope(tree.source(), '/../../etc')

    def test_equivalent_when_limits_match(self):
        tree = self.tree()
        tree.scope()
        service = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        scope = rb.read_envelope(tree.source(), '/system.slice/run-abc.scope')
        self.assertEqual(rb.compare_environments(service, scope)['state'], rb.EQUIVALENT)

    def test_not_equivalent_when_limit_differs(self):
        tree = self.tree()
        tree.scope(maximum=100000000)
        service = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        scope = rb.read_envelope(tree.source(), '/system.slice/run-abc.scope')
        verdict = rb.compare_environments(service, scope)
        self.assertEqual(verdict['state'], rb.NOT_EQUIVALENT)
        self.assertTrue(any('limits differ' in r for r in verdict['reasons']))

    def test_not_equivalent_when_depth_differs(self):
        tree = self.tree()
        tree.scope(path='/run-abc.scope')
        service = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        scope = rb.read_envelope(tree.source(), '/run-abc.scope')
        verdict = rb.compare_environments(service, scope)
        self.assertEqual(verdict['state'], rb.NOT_EQUIVALENT)
        self.assertTrue(any('depth' in r for r in verdict['reasons']))

    def test_indeterminate_when_controllers_unreadable(self):
        tree = self.tree(controllers=False)
        tree.scope()
        service = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        scope = rb.read_envelope(tree.source(), '/system.slice/run-abc.scope')
        self.assertEqual(rb.compare_environments(service, scope)['state'], rb.INDETERMINATE)

    def test_indeterminate_when_limit_interface_unreadable(self):
        tree = Tree(self.dir).standard()
        # Remove memory.high at the leaf: unreadable is never "unlimited".
        os.remove(str(Path(self.dir) / 'sys/fs/cgroup/system.slice/node-panel.service/memory.high'))
        tree.scope()
        os.remove(str(Path(self.dir) / 'sys/fs/cgroup/system.slice/run-abc.scope/memory.high'))
        service = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        scope = rb.read_envelope(tree.source(), '/system.slice/run-abc.scope')
        self.assertEqual(rb.compare_environments(service, scope)['state'], rb.INDETERMINATE)

    def test_controller_chain_flags_missing_controller(self):
        tree = self.tree()
        env = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        env['controllers']['controllers'] = ['cpuset', 'cpu']
        indeterminate, reasons = rb._controller_chain(env)
        self.assertFalse(indeterminate)
        self.assertTrue(any('not available' in r for r in reasons))

    def test_overall_equivalence_worst_case(self):
        tree = self.tree()
        tree.scope()
        tree.scope(path='/system.slice/run-ok.scope')
        service = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        ok = rb.read_envelope(tree.source(), '/system.slice/run-abc.scope')
        ok2 = rb.read_envelope(tree.source(), '/system.slice/run-ok.scope')
        self.assertEqual(rb.overall_equivalence(service, [ok, ok2])['state'], rb.EQUIVALENT)
        self.assertEqual(rb.overall_equivalence(service, [])['state'], rb.INDETERMINATE)
        self.assertEqual(rb.overall_equivalence(None, [ok])['state'], rb.INDETERMINATE)

    def test_overall_equivalence_one_bad_scope(self):
        tree = self.tree()
        tree.scope()
        tree.scope(path='/system.slice/run-bad.scope', maximum=100000000)
        service = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        good = rb.read_envelope(tree.source(), '/system.slice/run-abc.scope')
        bad = rb.read_envelope(tree.source(), '/system.slice/run-bad.scope')
        self.assertEqual(rb.overall_equivalence(service, [good, bad])['state'], rb.NOT_EQUIVALENT)


class ScopePropertyTests(BenchBase):
    def test_scope_properties_from_leaf(self):
        tree = self.tree()
        env = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        self.assertEqual(rb.scope_properties(env),
                         ['MemoryMax=200000000', 'MemoryHigh=0', 'MemorySwapMax=0'])

    def test_scope_properties_unlimited(self):
        tree = self.tree()
        env = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        env['levels'][0]['max_unlimited'] = True
        env['levels'][0]['high_status'] = 'unlimited'
        env['levels'][0]['swap_max_status'] = 'unlimited'
        self.assertEqual(rb.scope_properties(env),
                         ['MemoryMax=infinity', 'MemoryHigh=infinity', 'MemorySwapMax=infinity'])

    def test_scope_properties_refuses_unreadable(self):
        tree = self.tree()
        env = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        env['levels'][0]['high_status'] = 'absent'
        with self.assertRaises(rb.BenchmarkError):
            rb.scope_properties(env)


# --------------------------------------------------------------- fixtures ----

class FixtureTests(BenchBase):
    def test_auth_fixture_isolates_samples(self):
        fixture = rb.AuthFixture('login')
        first = fixture.prepare()
        second = fixture.prepare()
        try:
            self.assertNotEqual(first['root'], second['root'])
            self.assertTrue(first['root'].is_dir())
            self.assertTrue(second['root'].is_dir())
        finally:
            fixture.cleanup(first)
            fixture.cleanup(second)
            self.assertFalse(first['root'].exists())

    def test_capability_fixture_fails_closed(self):
        with self.assertRaises(rb.Blocked):
            rb.CapabilityFixture('qr', {}).prepare()
        with self.assertRaises(rb.Blocked):
            rb.CapabilityFixture('qr', {'qr': {'argv': []}}).prepare()
        with self.assertRaises(rb.Blocked):
            rb.CapabilityFixture('qr', {'qr': {'argv': ['/bin/true'], 'timeout': 0}}).prepare()

    def test_capability_fixture_runs_spec(self):
        fixture = rb.CapabilityFixture('qr', {'qr': capability_spec('qr')})
        prepared = fixture.prepare()
        self.assertEqual(fixture.invoke(prepared), 0)

    def test_default_capabilities_are_empty(self):
        self.assertEqual(rb.default_capabilities(), {})

    def test_load_capabilities(self):
        self.assertEqual(rb.load_capabilities(None), {})
        path = os.path.join(self.dir, 'caps.json')
        Path(path).write_text(json.dumps({'qr': {'argv': ['/bin/true']}}))
        self.assertIn('qr', rb.load_capabilities(path))
        Path(path).write_text('not json')
        with self.assertRaises(rb.BenchmarkError):
            rb.load_capabilities(path)

    def test_installer_ops_never_fixtured_locally(self):
        for op in rb.EXTERNAL_OPS:
            with self.assertRaises(rb.Blocked):
                rb.build_fixture(op, {})


# ------------------------------------------------------------- external I/O --

class ExternalDocumentTests(BenchBase):
    def envelope(self, **kwargs):
        tree = self.tree(**kwargs)
        return tree.source(), rb.read_envelope(tree.source(), '/system.slice/node-panel.service')

    def make_doc(self, envelope, op='install-panel', samples=(1000, 1010, 1020, 1005, 1015)):
        return make_doc(envelope, op=op, samples=samples)

    def test_valid_document_verifies(self):
        _src, envelope = self.envelope()
        doc = self.make_doc(envelope)
        verdict = rb.validate_external_document(doc, envelope, expected_op='install-panel')
        self.assertTrue(verdict['verified'], verdict['issues'])
        self.assertEqual(verdict['increments'], [1000, 1010, 1020, 1005, 1015])

    def test_schema_mismatch(self):
        _src, envelope = self.envelope()
        doc = self.make_doc(envelope)
        doc['schema'] = 'wrong'
        self.assertFalse(rb.validate_external_document(doc, envelope)['verified'])

    def test_too_few_samples(self):
        _src, envelope = self.envelope()
        doc = self.make_doc(envelope, samples=(1, 2, 3, 4))
        verdict = rb.validate_external_document(doc, envelope)
        self.assertFalse(verdict['verified'])
        self.assertTrue(any('N_MIN' in i for i in verdict['issues']))

    def test_fingerprint_mismatch(self):
        _src, envelope = self.envelope()
        doc = self.make_doc(envelope)
        doc['fingerprint'] = '0' * 64
        self.assertFalse(rb.validate_external_document(doc, envelope)['verified'])

    def test_integrity_mismatch(self):
        _src, envelope = self.envelope()
        doc = self.make_doc(envelope)
        doc['integrity']['digest'] = '0' * 64
        verdict = rb.validate_external_document(doc, envelope)
        self.assertFalse(verdict['verified'])
        self.assertTrue(any('integrity' in i for i in verdict['issues']))

    def test_non_equivalent_environment_rejected(self):
        _src, envelope = self.envelope()
        tampered = copy.deepcopy(envelope)
        tampered['levels'][0]['max'] = 100000000
        tampered['levels'][0]['max_unlimited'] = False
        doc = self.make_doc(tampered)
        verdict = rb.validate_external_document(doc, envelope)
        self.assertFalse(verdict['verified'])
        self.assertTrue(any('EQUIVALENT' in i for i in verdict['issues']))

    def test_expected_op_mismatch(self):
        _src, envelope = self.envelope()
        doc = self.make_doc(envelope, op='install-panel')
        verdict = rb.validate_external_document(doc, envelope, expected_op='install-xray')
        self.assertFalse(verdict['verified'])

    def test_load_external_operations_blocks_when_missing(self):
        records = rb.load_external_operations({}, None)
        for op in rb.EXTERNAL_OPS:
            self.assertEqual(records[op]['status'], rb.STATUS_BLOCKED)
            self.assertFalse(records[op].get('samples'))

    def test_load_external_operations_accepts_valid_file(self):
        _src, envelope = self.envelope()
        doc = self.make_doc(envelope)
        path = os.path.join(self.dir, 'install-panel.json')
        Path(path).write_text(json.dumps(doc))
        records = rb.load_external_operations({'install-panel': path}, envelope)
        self.assertEqual(records['install-panel']['status'], rb.STATUS_OK)
        self.assertEqual(records['install-xray']['status'], rb.STATUS_BLOCKED)

    def test_parse_external_specs(self):
        self.assertEqual(rb.parse_external_specs(['install-panel=/a.json']),
                         {'install-panel': '/a.json'})
        with self.assertRaises(rb.BenchmarkError):
            rb.parse_external_specs(['login=/a.json'])
        with self.assertRaises(rb.BenchmarkError):
            rb.parse_external_specs(['install-panel'])


# --------------------------------------------------------------- profiles ----

def _constants(drawdown=1024, margin=0, discount=0.0):
    return {'comp_ratio': 0.0, 'comp_margin': margin, 'comp_reserve_margin': drawdown + margin,
            'safety_discount': discount, 'drawdown': drawdown, 'per_op_ratio': {}}


def complete_profile(reserve=1024, floor=50000000, generated_at='2026-01-01T00:00:00+00:00',
                     samples=None):
    profiles = dict((op, 4096) for op in rb.ALL_OPS)
    per_op_samples = list(samples) if samples is not None else [4096] * rb.N_MIN
    profile = {'schema_version': rb.PROFILE_SCHEMA_VERSION,
               'benchmark_version': rb.BENCHMARK_VERSION,
               'probe_version': rb.PROBE_VERSION_EXPECTED,
               'status': rb.STATUS_COMPLETE,
               'generated_at': generated_at,
               'cgroup_equivalence': rb.EQUIVALENT,
               'profiles': profiles,
               'reserve': reserve,
               'available_floor': floor,
               'constants': _constants(),
               'operations': dict((op, {'status': rb.STATUS_OK, 'samples': list(per_op_samples)})
                                  for op in rb.ALL_OPS),
               'environment_fingerprint': None,
               'environment': None,
               'issues': []}
    profile['fingerprint'] = rb.profile_fingerprint(profile)
    return profile


def incomplete_profile():
    profile = complete_profile()
    profile['status'] = rb.STATUS_INCOMPLETE
    profile['profiles'] = {'login': 4096}
    profile['reserve'] = 0
    profile['available_floor'] = 0
    profile['fingerprint'] = rb.profile_fingerprint(profile)
    return profile


class ProfileValidationTests(BenchBase):
    def test_complete_profile_roundtrips(self):
        raw = rb.dumps_profile(complete_profile())
        profile = rb.loads_profile(raw)
        self.assertEqual(profile['status'], rb.STATUS_COMPLETE)
        self.assertEqual(set(profile['profiles']), set(rb.ALL_OPS))

    def test_incomplete_profile_roundtrips(self):
        raw = rb.dumps_profile(incomplete_profile())
        profile = rb.loads_profile(raw)
        self.assertEqual(profile['status'], rb.STATUS_INCOMPLETE)

    def test_normal_lane_budget_rejected(self):
        profile = complete_profile()
        profile['profiles']['normal'] = 4096
        profile['fingerprint'] = rb.profile_fingerprint(profile)
        with self.assertRaises(rb.BenchmarkError):
            rb.validate_profile(profile)

    def test_complete_requires_all_ops(self):
        profile = complete_profile()
        del profile['profiles']['qr']
        profile['fingerprint'] = rb.profile_fingerprint(profile)
        with self.assertRaises(rb.BenchmarkError):
            rb.validate_profile(profile)

    def test_complete_requires_equivalence(self):
        profile = complete_profile()
        profile['cgroup_equivalence'] = rb.NOT_EQUIVALENT
        profile['fingerprint'] = rb.profile_fingerprint(profile)
        with self.assertRaises(rb.BenchmarkError):
            rb.validate_profile(profile)

    def test_complete_requires_positive_reserve(self):
        profile = complete_profile(reserve=0)
        with self.assertRaises(rb.BenchmarkError):
            rb.validate_profile(profile)

    def test_fingerprint_detects_tampering(self):
        raw = rb.dumps_profile(complete_profile())
        profile = json.loads(raw)
        profile['reserve'] = 999999
        with self.assertRaises(rb.BenchmarkError):
            rb.loads_profile(json.dumps(profile).encode())

    def test_missing_field_rejected(self):
        profile = complete_profile()
        del profile['reserve']
        with self.assertRaises(rb.BenchmarkError):
            rb.validate_profile(profile)


# ----------------------------------------------------------- atomic writer ---

class AtomicWriteTests(BenchBase):
    def test_roundtrip(self):
        path = os.path.join(self.dir, 'x.json')
        rb._atomic_write(path, b'hello')
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), b'hello')

    def test_fsync_failure_is_uncertain_but_visible(self):
        path = os.path.join(self.dir, 'x.json')
        with mock.patch.object(rb, '_fsync_dir', side_effect=OSError('boom')):
            with self.assertRaises(rb.UncertainWrite):
                rb._atomic_write(path, b'data')
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), b'data')


class ProfileWriterTests(BenchBase):
    def writer(self):
        return rb.ProfileWriter(os.path.join(self.dir, 'resource-profile.json'))

    def test_requires_confirmation(self):
        result = self.writer().install(rb.dumps_profile(complete_profile()), confirmed=False)
        self.assertEqual(result['state'], rb.ABORTED)

    def test_incomplete_candidate_refused_even_with_force(self):
        writer = self.writer()
        result = writer.install(rb.dumps_profile(incomplete_profile()), force=True, confirmed=True)
        self.assertEqual(result['state'], rb.ABORTED)
        self.assertFalse(os.path.exists(writer.path))

    def test_incomplete_never_reaches_formal_path(self):
        writer = self.writer()
        result = writer.install(rb.dumps_profile(incomplete_profile()), confirmed=True)
        self.assertEqual(result['state'], rb.ABORTED)
        self.assertFalse(os.path.exists(writer.path))

    def test_commit_first_time(self):
        writer = self.writer()
        raw = rb.dumps_profile(complete_profile())
        result = writer.install(raw, confirmed=True)
        self.assertEqual(result['state'], rb.COMMITTED)
        with open(writer.path, 'rb') as handle:
            self.assertEqual(handle.read(), raw)
        self.assertFalse(os.path.exists(writer.backup_path))

    def test_existing_valid_requires_force(self):
        writer = self.writer()
        writer.install(rb.dumps_profile(complete_profile()), confirmed=True)
        result = writer.install(rb.dumps_profile(complete_profile(reserve=2048)), confirmed=True)
        self.assertEqual(result['state'], rb.ABORTED)

    def test_force_backs_up_and_replaces(self):
        writer = self.writer()
        first = rb.dumps_profile(complete_profile(reserve=1024, generated_at='t1'))
        second = rb.dumps_profile(complete_profile(reserve=2048, generated_at='t2'))
        writer.install(first, confirmed=True)
        result = writer.install(second, force=True, confirmed=True)
        self.assertEqual(result['state'], rb.COMMITTED)
        self.assertTrue(result['backup_written'])
        with open(writer.backup_path, 'rb') as handle:
            self.assertEqual(handle.read(), first)
        with open(writer.path, 'rb') as handle:
            self.assertEqual(handle.read(), second)

    def test_backup_failure_aborts_and_leaves_formal(self):
        writer = self.writer()
        first = rb.dumps_profile(complete_profile(reserve=1024, generated_at='t1'))
        second = rb.dumps_profile(complete_profile(reserve=2048, generated_at='t2'))
        writer.install(first, confirmed=True)
        with mock.patch.object(rb.ProfileWriter, '_write_file', side_effect=OSError('boom')):
            result = writer.install(second, force=True, confirmed=True)
        self.assertEqual(result['state'], rb.ABORTED)
        with open(writer.path, 'rb') as handle:
            self.assertEqual(handle.read(), first)

    def test_formal_fsync_failure_is_commit_uncertain_and_poisons(self):
        writer = self.writer()
        with mock.patch.object(rb, '_fsync_dir', side_effect=OSError('boom')):
            result = writer.install(rb.dumps_profile(complete_profile()), confirmed=True)
        self.assertEqual(result['state'], rb.COMMIT_UNCERTAIN)
        self.assertTrue(writer.uncertain)
        again = writer.install(rb.dumps_profile(complete_profile()), force=True, confirmed=True)
        self.assertEqual(again['state'], rb.COMMIT_UNCERTAIN)

    def test_recheck_reports_both_files(self):
        writer = self.writer()
        writer.install(rb.dumps_profile(complete_profile()), confirmed=True)
        state = writer.recheck()
        self.assertTrue(state['formal_valid'])
        self.assertFalse(state['backup_valid'])


# --------------------------------------------------------- orchestration -----

class _SessionHarness(BenchBase):
    def build(self, missing=None, capabilities=None):
        results = {}
        for op in rb.LOCAL_OPS:
            if op == missing:
                continue
            results[op] = {'status': rb.STATUS_OK, 'increment': 1000}
        counter = itertools.count()
        results[rb.IDLE_OP] = lambda op, c=counter: {'status': rb.STATUS_OK,
                                                     'increment': (0, 4)[next(c) % 2]}
        meter = rb.FakeMeter(results)
        pool = itertools.cycle([150000000, 149999000])
        sampler = lambda: next(pool)
        if capabilities is None:
            capabilities = panel_capabilities()
        session = rb.MeasurementSession(meter, sampler, capabilities, samples=rb.N_MIN)
        return session.run()

    def synthetic_env(self):
        cgroup = {'valid': True, 'mount_root': '/', 'mount_point': '/sys/fs/cgroup',
                  'self_path': '/system.slice/node-panel.service',
                  'controllers': {'controllers': ['cpuset', 'cpu', 'io', 'memory', 'pids'],
                                  'controllers_status': 'ok', 'subtree': ['memory'],
                                  'subtree_status': 'ok'},
                  'levels': [dict(_leaf(), path='/x'), dict(_leaf(maximum=None, high='max', swap='max'), path='/system.slice'),
                             dict(_leaf(maximum=None, high='max', swap='max'), path='/')]}
        return {'benchmark_version': rb.BENCHMARK_VERSION,
                'probe_version': rb.PROBE_VERSION_EXPECTED,
                'valid': True,
                'system': {'arch': 'x86_64', 'cpu_count': 4, 'page_size': 4096},
                'meminfo': {'MemTotal': 2000000000, 'MemAvailable': 1500000000,
                            'SwapTotal': 0, 'SwapFree': 0},
                'cgroup': cgroup}


class MeterDegradationTests(BenchBase):
    class _RaisingMeter:
        def measure(self, op, fixture=None):
            raise rb.Blocked('no scope available')

    def test_measure_operation_absorbs_blocked_meter(self):
        results = rb.measure_operation(self._RaisingMeter(), 'login', rb.AuthFixture('login'), samples=3)
        self.assertEqual(len(results), 3)
        for record in results:
            self.assertEqual(record['status'], rb.STATUS_BLOCKED)

    def test_session_survives_blocked_meter(self):
        session = rb.MeasurementSession(self._RaisingMeter(), lambda: 100000000, {},
                                        samples=2, idle_samples=2)
        outcome = session.run(local_ops=['login'])
        self.assertEqual(outcome['idle'], [])
        self.assertEqual(outcome['operations']['login']['status'], rb.STATUS_BLOCKED)


class OrchestrationTests(_SessionHarness):
    def externals(self):
        return dict((op, {'status': rb.STATUS_OK, 'samples': [2000] * 5,
                          'filtered': [2000] * 5, 'source': 'external'}) for op in rb.EXTERNAL_OPS)

    def test_full_session_composes_complete_profile(self):
        outcome = self.build()
        constants = rb.compute_constants(outcome['per_op'], outcome['idle'], outcome['available'])
        profile = rb.compose_profile(outcome, self.externals(), constants, self.synthetic_env(),
                                     {'state': rb.EQUIVALENT, 'reasons': []})
        self.assertEqual(profile['status'], rb.STATUS_COMPLETE)
        self.assertEqual(set(profile['profiles']), set(rb.ALL_OPS))
        self.assertNotIn('normal', profile['profiles'])
        self.assertGreater(profile['reserve'], 0)
        self.assertGreater(profile['available_floor'], 0)
        rb.validate_profile(profile)
        rb.loads_profile(rb.dumps_profile(profile))

    def test_missing_operation_yields_incomplete(self):
        outcome = self.build(missing='qr')
        constants = rb.compute_constants(outcome['per_op'], outcome['idle'], outcome['available'])
        profile = rb.compose_profile(outcome, self.externals(), constants, self.synthetic_env(),
                                     {'state': rb.EQUIVALENT, 'reasons': []})
        self.assertEqual(profile['status'], rb.STATUS_INCOMPLETE)
        rb.validate_profile(profile)

    def test_non_equivalent_environment_forbids_complete(self):
        outcome = self.build()
        constants = rb.compute_constants(outcome['per_op'], outcome['idle'], outcome['available'])
        profile = rb.compose_profile(outcome, self.externals(), constants, self.synthetic_env(),
                                     {'state': rb.NOT_EQUIVALENT, 'reasons': ['limits differ']})
        self.assertEqual(profile['status'], rb.STATUS_INCOMPLETE)

    def test_rejection_threshold_forbids_complete(self):
        outcome = self.build()
        constants = rb.compute_constants(outcome['per_op'], outcome['idle'], outcome['available'])
        constants['comp_ratio'] = 2.0
        profile = rb.compose_profile(outcome, self.externals(), constants, self.synthetic_env(),
                                     {'state': rb.EQUIVALENT, 'reasons': []})
        self.assertEqual(profile['status'], rb.STATUS_INCOMPLETE)

    def test_oversized_budget_downgrades_to_incomplete(self):
        outcome = self.build()
        outcome['per_op']['qr'] = [rb.RAM_LIMIT + 10] * 5
        outcome['operations']['qr']['filtered'] = [rb.RAM_LIMIT + 10] * 5
        constants = rb.compute_constants(outcome['per_op'], outcome['idle'], outcome['available'])
        profile = rb.compose_profile(outcome, self.externals(), constants, self.synthetic_env(),
                                     {'state': rb.EQUIVALENT, 'reasons': []})
        self.assertEqual(profile['status'], rb.STATUS_INCOMPLETE)
        self.assertNotIn('qr', profile['profiles'])
        rb.validate_profile(profile)


# ------------------------------------------------------- capture + CLI -------

class CaptureTests(BenchBase):
    def test_capture_environment(self):
        tree = self.tree()
        env = rb.capture_environment(source=tree.source(), system={'arch': 'x86_64', 'cpu_count': 2},
                                     clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertTrue(env['valid'])
        self.assertIsNotNone(env['fingerprint'])
        self.assertEqual(env['cgroup']['mount_point'], '/sys/fs/cgroup')


class CliTests(BenchBase):
    def run_main(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = rb.main(argv)
        return code, buffer.getvalue()

    def test_verify_cli(self):
        path = os.path.join(self.dir, 'p.json')
        Path(path).write_bytes(rb.dumps_profile(complete_profile()))
        code, _out = self.run_main(['verify', '--profile', path])
        self.assertEqual(code, 0)

    def test_install_cli_refuses_incomplete(self):
        candidate = os.path.join(self.dir, 'c.candidate.json')
        Path(candidate).write_bytes(rb.dumps_profile(incomplete_profile()))
        formal = os.path.join(self.dir, 'resource-profile.json')
        code, out = self.run_main(['install', '--candidate', candidate, '--profile', formal, '--yes'])
        self.assertEqual(code, 2)
        self.assertIn(rb.ABORTED, out)
        self.assertFalse(os.path.exists(formal))

    def test_install_cli_commits_complete(self):
        candidate = os.path.join(self.dir, 'c.candidate.json')
        Path(candidate).write_bytes(rb.dumps_profile(complete_profile()))
        formal = os.path.join(self.dir, 'resource-profile.json')
        code, out = self.run_main(['install', '--candidate', candidate, '--profile', formal, '--yes'])
        self.assertEqual(code, 0)
        self.assertIn(rb.COMMITTED, out)
        self.assertTrue(os.path.exists(formal))

    def test_external_cli(self):
        tree = self.tree()
        envelope = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        doc = make_doc(envelope)
        path = os.path.join(self.dir, 'ext.json')
        Path(path).write_text(json.dumps(doc))
        code, out = self.run_main(['external', '--document', path, '--root', self.dir,
                                   '--service-cgroup', '/system.slice/node-panel.service'])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)['verified'])

    def test_recheck_cli(self):
        formal = os.path.join(self.dir, 'resource-profile.json')
        code, out = self.run_main(['recheck', '--profile', formal])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)['formal_valid'])


# --------------------------------------------------------------- safety ------

class SafetyTests(BenchBase):
    def test_import_has_no_filesystem_side_effects(self):
        code = (
            "import builtins, argparse, hashlib, json, math, os, re, subprocess, sys, tempfile, time\n"
            "import contextlib, datetime, pathlib\n"
            "import resource_probe\n"
            "def guard(*args, **kwargs):\n"
            "    raise RuntimeError('unexpected open during import')\n"
            "builtins.open = guard\n"
            "import resource_benchmark\n"
            "print('PASS')\n"
        )
        proc = subprocess.run([sys.executable, '-c', code], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('PASS', proc.stdout)

    def test_scope_control_stays_alive_until_ack(self):
        source = inspect.getsource(rb._cmd_scope_control)
        self.assertIn('stdin.readline', source)

    def test_installer_measure_refused_on_host(self):
        meter = rb.RealScopeMeter(systemd_run='/nonexistent/systemd-run')
        for op in rb.EXTERNAL_OPS:
            with self.assertRaises(rb.Blocked):
                meter.measure(op)

    def test_benchmark_does_not_use_psi(self):
        source = inspect.getsource(rb)
        self.assertNotIn('read_psi', source)
        self.assertNotIn('pressure', source)

    def test_safety_checks_survive_optimized_mode(self):
        code = (
            "import resource_benchmark as rb\n"
            "try:\n"
            "    rb.validate_profile({})\n"
            "except rb.BenchmarkError:\n"
            "    print('PASS')\n"
            "else:\n"
            "    raise SystemExit('FAIL: empty profile accepted under -O')\n"
        )
        proc = subprocess.run([sys.executable, '-O', '-c', code], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn('PASS', proc.stdout)


def run_cli(argv):
    """Run the CLI capturing stdout and stderr (stderr carries refusal output)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rb.main(argv)
    return code, out.getvalue(), err.getvalue()


# ------------------------------------------------ measurement boundary (P0-1) --

class MeasurementBoundaryTests(BenchBase):
    def _recorder(self, order):
        class Recorder:
            def prepare(self):
                order.append('prepare')
                return {'ok': True}

            def invoke(self, prepared):
                order.append('invoke')
                return 0

            def cleanup(self, prepared):
                order.append('cleanup')

        return Recorder()

    def test_preparation_precedes_pre_current_and_cleanup_follows_peak(self):
        tree = self.tree()
        order = []
        original = rb.read_self_memory

        def recording_read(source=None):
            order.append('read')
            return original(source)

        with mock.patch.object(rb, 'build_fixture', return_value=self._recorder(order)), \
                mock.patch.object(rb, 'read_self_memory', side_effect=recording_read):
            result = rb.run_scope_operation('login', source=tree.source())
        self.assertEqual(result['status'], rb.STATUS_OK)
        # probe -> prepare -> pre_current -> invoke -> peak -> cleanup
        self.assertEqual(order, ['read', 'prepare', 'read', 'invoke', 'read', 'cleanup'])

    def test_preparation_cost_is_not_charged_to_the_increment(self):
        tree = self.tree()
        reads = itertools.count(1)

        def fake_read(source=None):
            n = next(reads)
            peak = 3000 if n >= 3 else 1000
            return {'dir': '/x', 'current': 1000, 'peak': peak, 'peak_status': 'ok'}

        with mock.patch.object(rb, 'build_fixture', return_value=self._recorder([])), \
                mock.patch.object(rb, 'read_self_memory', side_effect=fake_read):
            result = rb.run_scope_operation('login', source=tree.source())
        self.assertEqual(result['status'], rb.STATUS_OK)
        self.assertEqual(result['increment'], 2000)


class FixtureCleanupTests(BenchBase):
    def test_cleanup_runs_even_when_the_operation_fails(self):
        tree = self.tree()
        order = []

        class Failing:
            def prepare(self):
                order.append('prepare')
                return {}

            def invoke(self, prepared):
                order.append('invoke')
                raise rb.OperationFailed('boom')

            def cleanup(self, prepared):
                order.append('cleanup')

        with mock.patch.object(rb, 'build_fixture', return_value=Failing()):
            result = rb.run_scope_operation('qr', source=tree.source(), capabilities={'qr': {}})
        self.assertEqual(result['status'], rb.STATUS_ERROR)
        self.assertEqual(order, ['prepare', 'invoke', 'cleanup'])

    def test_cleanup_failure_is_reported_not_hidden(self):
        tree = self.tree()

        class Leaky:
            def prepare(self):
                return {}

            def invoke(self, prepared):
                return 0

            def cleanup(self, prepared):
                raise OSError('cannot remove temporary vault')

        with mock.patch.object(rb, 'build_fixture', return_value=Leaky()):
            result = rb.run_scope_operation('qr', source=tree.source(), capabilities={'qr': {}})
        self.assertEqual(result['status'], rb.STATUS_OK)
        self.assertIn('cleanup', result['cleanup_issue'])

    def test_cleanup_failure_does_not_mask_a_measurement_failure(self):
        tree = self.tree()

        class Broken:
            def prepare(self):
                return {}

            def invoke(self, prepared):
                raise rb.OperationFailed('invoke failed')

            def cleanup(self, prepared):
                raise OSError('cannot remove temporary vault')

        with mock.patch.object(rb, 'build_fixture', return_value=Broken()):
            result = rb.run_scope_operation('qr', source=tree.source(), capabilities={'qr': {}})
        self.assertEqual(result['status'], rb.STATUS_ERROR)
        self.assertIn('invoke failed', result['reason'])
        self.assertIn('cleanup', result['cleanup_issue'])

    def test_auth_cleanup_removes_its_own_vault(self):
        fixture = rb.AuthFixture('login')
        root = Path(tempfile.mkdtemp(prefix='rpb-auth-'))
        fixture.cleanup({'auth': _closable(), 'root': root})
        self.assertFalse(root.exists())

    def test_auth_cleanup_reports_a_removal_failure(self):
        fixture = rb.AuthFixture('login')
        root = Path(tempfile.mkdtemp(prefix='rpb-auth-'))
        try:
            with mock.patch.object(shutil, 'rmtree', side_effect=OSError('busy')):
                with self.assertRaises(OSError) as caught:
                    fixture.cleanup({'auth': _closable(), 'root': root})
            self.assertIn('busy', str(caught.exception))
        finally:
            shutil.rmtree(str(root), ignore_errors=True)

    def test_auth_cleanup_tolerates_an_absent_vault(self):
        fixture = rb.AuthFixture('login')
        missing = Path(os.path.join(self.dir, 'rpb-auth-missing'))
        fixture.cleanup({'auth': _closable(), 'root': missing})  # must not raise

    def test_auth_cleanup_never_removes_a_foreign_directory(self):
        fixture = rb.AuthFixture('login')
        foreign = Path(tempfile.mkdtemp(prefix='user-data-'))
        try:
            fixture.cleanup({'auth': _closable(), 'root': foreign})
            self.assertTrue(foreign.exists())
        finally:
            shutil.rmtree(str(foreign), ignore_errors=True)

    def test_auth_cleanup_reports_a_close_failure(self):
        fixture = rb.AuthFixture('login')
        root = Path(tempfile.mkdtemp(prefix='rpb-auth-'))

        class BadClose:
            def close(self):
                raise OSError('close failed')

        try:
            with self.assertRaises(OSError) as caught:
                fixture.cleanup({'auth': BadClose(), 'root': root})
            self.assertIn('close failed', str(caught.exception))
            self.assertFalse(root.exists())  # the directory is still removed
        finally:
            shutil.rmtree(str(root), ignore_errors=True)


# ------------------------------------------------- capability results (P0-5) --

class CapabilityResultTests(BenchBase):
    """A capability must bind to its real business entry point and prove it ran.

    A process that merely exits zero is never proof that a business operation
    happened, so the fixture requires an explicit entry point plus a verifiable
    result contract and rejects anything that does not satisfy it.
    """

    def fixture(self, op, spec):
        return rb.CapabilityFixture(op, {op: spec})

    def run_scope(self, op, spec):
        tree = self.tree()
        return rb.run_scope_operation(op, source=tree.source(), capabilities={op: spec})

    def test_arbitrary_exit_zero_command_is_rejected(self):
        # 'raise SystemExit(0)' proves nothing about the qr business operation.
        spec = capability_spec('qr', argv=[sys.executable, '-c', 'raise SystemExit(0)'])
        fixture = self.fixture('qr', spec)
        with self.assertRaises(rb.OperationFailed) as caught:
            fixture.invoke(fixture.prepare())
        self.assertIn('did not prove', str(caught.exception))

    def test_arbitrary_exit_zero_is_not_a_valid_scope_sample(self):
        spec = capability_spec('qr', argv=[sys.executable, '-c', 'raise SystemExit(0)'])
        self.assertEqual(self.run_scope('qr', spec)['status'], rb.STATUS_ERROR)

    def test_empty_command_is_blocked(self):
        with self.assertRaises(rb.Blocked):
            self.fixture('qr', capability_spec('qr', argv=[])).prepare()

    def test_missing_capability_is_blocked(self):
        with self.assertRaises(rb.Blocked):
            rb.CapabilityFixture('qr', {}).prepare()
        result = rb.run_scope_operation('qr', source=self.tree().source(), capabilities={})
        self.assertEqual(result['status'], rb.STATUS_BLOCKED)

    def test_wrong_business_entrypoint_is_blocked(self):
        spec = capability_spec('qr', entrypoint='function.GenericTool.__call__')
        with self.assertRaises(rb.Blocked):
            self.fixture('qr', spec).prepare()

    def test_missing_result_contract_is_blocked(self):
        spec = capability_spec('qr')
        del spec['result']
        with self.assertRaises(rb.Blocked):
            self.fixture('qr', spec).prepare()

    def test_nonzero_exit_is_a_failed_operation(self):
        argv = [sys.executable, '-c',
                'import json, sys; print(json.dumps({"operation": "qr", "ok": True})); sys.exit(3)']
        fixture = self.fixture('qr', capability_spec('qr', argv=argv))
        with self.assertRaises(rb.OperationFailed):
            fixture.invoke(fixture.prepare())

    def test_timeout_is_a_failed_operation(self):
        argv = [sys.executable, '-c', 'import time; time.sleep(30)']
        fixture = self.fixture('qr', capability_spec('qr', argv=argv, timeout=1))
        with self.assertRaises(rb.OperationFailed):
            fixture.invoke(fixture.prepare())

    def test_unlaunchable_command_is_a_failed_operation(self):
        fixture = self.fixture('qr', capability_spec('qr', argv=['/nonexistent/benchmark-binary-xyz']))
        with self.assertRaises(rb.OperationFailed):
            fixture.invoke(fixture.prepare())

    def test_result_that_does_not_identify_the_operation_is_rejected(self):
        # A diagnostics-shaped success can never stand in for the qr operation.
        argv = [sys.executable, '-c',
                'import json; print(json.dumps({"operation": "diagnostics", "ok": True}))']
        fixture = self.fixture('qr', capability_spec('qr', argv=argv))
        with self.assertRaises(rb.OperationFailed):
            fixture.invoke(fixture.prepare())

    def test_result_with_the_wrong_business_value_is_rejected(self):
        argv = [sys.executable, '-c',
                'import json; print(json.dumps({"operation": "qr", "ok": False}))']
        fixture = self.fixture('qr', capability_spec('qr', argv=argv))
        with self.assertRaises(rb.OperationFailed):
            fixture.invoke(fixture.prepare())

    def test_correct_entrypoint_with_verified_result_passes(self):
        spec = capability_spec('qr')
        self.assertEqual(spec['entrypoint'], rb.CAPABILITY_ENTRYPOINTS['qr'])
        fixture = self.fixture('qr', spec)
        self.assertEqual(fixture.invoke(fixture.prepare()), 0)
        self.assertEqual(self.run_scope('qr', spec)['status'], rb.STATUS_OK)

    def test_operations_cannot_share_one_meaningless_success(self):
        # The same qr result must not be accepted for a different operation.
        shared = capability_spec('qr')
        spec = capability_spec('diagnostics', argv=list(shared['argv']))
        fixture = self.fixture('diagnostics', spec)
        with self.assertRaises(rb.OperationFailed):
            fixture.invoke(fixture.prepare())

    def test_scope_operation_rejects_nonzero_capability(self):
        tree = self.tree()
        argv = [sys.executable, '-c',
                'import json, sys; print(json.dumps({"operation": "qr", "ok": True})); sys.exit(3)']
        caps = {'qr': capability_spec('qr', argv=argv)}
        result = rb.run_scope_operation('qr', source=tree.source(), capabilities=caps)
        self.assertEqual(result['status'], rb.STATUS_ERROR)

    def test_missing_capability_blocks_scope_operation(self):
        tree = self.tree()
        result = rb.run_scope_operation('qr', source=tree.source(), capabilities={})
        self.assertEqual(result['status'], rb.STATUS_BLOCKED)


# ------------------------------------------------------ scope timeout (P1-2) --

class _BlockingStream:
    def readline(self):
        time.sleep(30)
        return ''


class _FakeScopeProc:
    def __init__(self):
        self.stdin = io.StringIO()
        self.stdout = _BlockingStream()
        self.stderr = io.StringIO()
        self.killed = False

    def poll(self):
        return -9 if self.killed else None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class ScopeTimeoutTests(BenchBase):
    def test_blocking_readline_times_out(self):
        read_end, write_end = os.pipe()
        stream = os.fdopen(read_end, 'r')
        try:
            line, failure = rb.RealScopeMeter._read_line_with_timeout(stream, 0.2)
        finally:
            # Close the write end so the helper reader thread sees EOF, then
            # close our handle to the read end so no descriptor is leaked.
            os.close(write_end)
            stream.close()
        self.assertIsNone(line)
        self.assertEqual(failure, 'timeout')

    def test_measure_times_out_and_reaps_the_scope(self):
        captured = {}

        def fake_popen(argv, **kwargs):
            captured['proc'] = _FakeScopeProc()
            return captured['proc']

        meter = rb.RealScopeMeter(systemd_run='/bin/echo', timeout=1, popen=fake_popen)
        with self.assertRaises(rb.Blocked):
            meter.measure('login')
        self.assertTrue(captured['proc'].killed)


# ------------------------------------------- controller chain leaf/ancestor ---

class ControllerChainTests(BenchBase):
    def test_middle_ancestor_without_subtree_control_is_indeterminate(self):
        tree = Tree(self.dir).standard()
        os.remove(str(Path(self.dir) / 'sys/fs/cgroup/system.slice/cgroup.subtree_control'))
        tree.scope()
        service = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        scope = rb.read_envelope(tree.source(), '/system.slice/run-abc.scope')
        self.assertEqual(rb.compare_environments(service, scope)['state'], rb.INDETERMINATE)

    def test_middle_ancestor_absent_subtree_is_indeterminate_directly(self):
        tree = Tree(self.dir).standard()
        os.remove(str(Path(self.dir) / 'sys/fs/cgroup/system.slice/cgroup.subtree_control'))
        service = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        indeterminate, _reasons = rb._controller_chain(service)
        self.assertTrue(indeterminate)

    def test_leaf_without_subtree_control_is_still_equivalent(self):
        tree = self.tree()
        tree.scope()
        service = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        scope = rb.read_envelope(tree.source(), '/system.slice/run-abc.scope')
        self.assertEqual(rb.compare_environments(service, scope)['state'], rb.EQUIVALENT)


# --------------------------------------- external local-envelope gate (P1-4) --

class ExternalLocalEnvelopeTests(BenchBase):
    def test_missing_local_envelope_is_not_verified(self):
        tree = Tree(self.dir).standard()
        envelope = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        doc = make_doc(envelope)
        verdict = rb.validate_external_document(doc, None)
        self.assertFalse(verdict['verified'])
        self.assertTrue(any('equivalence' in i for i in verdict['issues']))

    def test_external_cli_reports_not_verified_without_local_environment(self):
        tree = Tree(self.dir).standard()
        envelope = rb.read_envelope(tree.source(), '/system.slice/node-panel.service')
        doc = make_doc(envelope)
        path = os.path.join(self.dir, 'ext.json')
        Path(path).write_text(json.dumps(doc))
        code, out, _err = run_cli(['external', '--document', path, '--root', self.dir])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(out)['verified'])


# ----------------------------------------- profile fingerprint integrity -----

class FingerprintIntegrityTests(BenchBase):
    def test_fingerprint_covers_operations(self):
        profile = complete_profile()
        profile['operations']['qr']['samples'] = [9999]
        with self.assertRaises(rb.BenchmarkError):
            rb.validate_profile(profile)

    def test_fingerprint_covers_generated_at(self):
        profile = complete_profile()
        profile['generated_at'] = '2099-01-01T00:00:00+00:00'
        with self.assertRaises(rb.BenchmarkError):
            rb.validate_profile(profile)

    def test_fingerprint_covers_environment(self):
        profile = complete_profile()
        profile['environment'] = {'injected': True}
        with self.assertRaises(rb.BenchmarkError):
            rb.validate_profile(profile)

    def test_non_hex_fingerprint_is_rejected(self):
        profile = complete_profile()
        profile['fingerprint'] = 'not-a-real-digest'
        with self.assertRaises(rb.BenchmarkError):
            rb.validate_profile(profile)

    def test_loads_profile_validates_fully(self):
        raw = rb.dumps_profile(complete_profile())
        tampered = json.loads(raw)
        tampered['profiles']['qr'] = 1
        with self.assertRaises(rb.BenchmarkError):
            rb.loads_profile(json.dumps(tampered).encode())


# ------------------------------------------------ feasibility gate (P0-2) -----

class FeasibilityGateTests(BenchBase):
    def _operations(self):
        return dict((op, {'status': rb.STATUS_OK, 'samples': [4096] * rb.N_MIN}) for op in rb.ALL_OPS)

    def _profiles(self):
        return dict((op, 4096) for op in rb.ALL_OPS)

    def test_infeasible_profile_is_downgraded_to_incomplete(self):
        profile = rb.build_profile(self._operations(), _constants(), self._profiles(),
                                   1024, 5000, None, rb.EQUIVALENT)
        self.assertEqual(profile['status'], rb.STATUS_INCOMPLETE)
        self.assertTrue(any('feasibility' in r for r in profile['issues']))

    def test_feasible_profile_stays_complete(self):
        profile = rb.build_profile(self._operations(), _constants(), self._profiles(),
                                   1024, 50000000, None, rb.EQUIVALENT)
        self.assertEqual(profile['status'], rb.STATUS_COMPLETE)

    def test_complete_profile_never_adds_a_normal_lane(self):
        profile = rb.build_profile(self._operations(), _constants(), self._profiles(),
                                   1024, 50000000, None, rb.EQUIVALENT)
        self.assertNotIn('normal', profile['profiles'])

    def test_validate_rejects_infeasible_complete(self):
        profile = complete_profile()
        profile['available_floor'] = 100
        profile['fingerprint'] = rb.profile_fingerprint(profile)
        with self.assertRaises(rb.BenchmarkError):
            rb.validate_profile(profile)


# ------------------------------------------- formal sample count (P0-6) ------

class FormalSampleCountTests(BenchBase):
    """A formal (complete) profile needs at least N_MIN valid samples per op."""

    def complete(self, samples):
        return complete_profile(samples=samples)

    def test_fewer_than_min_samples_is_rejected(self):
        for count in (1, 2, 4):
            with self.assertRaises(rb.BenchmarkError):
                rb.validate_profile(self.complete([4096] * count))

    def test_min_and_more_samples_are_accepted(self):
        rb.validate_profile(self.complete([4096] * rb.N_MIN))
        rb.validate_profile(self.complete([4096] * (rb.N_MIN + 3)))
        rb.validate_profile(self.complete([0] * rb.N_MIN))

    def test_invalid_sample_values_are_rejected(self):
        for bad in (True, False, -1, 1.5, '10', None):
            with self.assertRaises(rb.BenchmarkError):
                rb.validate_profile(self.complete([4096] * rb.N_MIN + [bad]))

    def test_load_entry_point_rejects_a_single_sample_profile(self):
        raw = rb.dumps_profile(self.complete([4096]))
        with self.assertRaises(rb.BenchmarkError):
            rb.loads_profile(raw)

    def test_load_entry_point_rejects_an_invalid_sample_value(self):
        raw = rb.dumps_profile(self.complete([4096] * rb.N_MIN + [True]))
        with self.assertRaises(rb.BenchmarkError):
            rb.loads_profile(raw)

    def test_write_entry_point_cannot_bypass_the_sample_minimum(self):
        writer = rb.ProfileWriter(os.path.join(self.dir, 'resource-profile.json'))
        raw = rb.dumps_profile(self.complete([4096]))
        result = writer.install(raw, confirmed=True)
        self.assertEqual(result['state'], rb.ABORTED)
        self.assertFalse(os.path.exists(writer.path))

    def test_build_profile_downgrades_too_few_samples_to_incomplete(self):
        operations = dict((op, {'status': rb.STATUS_OK, 'samples': [4096]}) for op in rb.ALL_OPS)
        profiles = dict((op, 4096) for op in rb.ALL_OPS)
        profile = rb.build_profile(operations, _constants(), profiles, 1024, 50000000, None, rb.EQUIVALENT)
        self.assertEqual(profile['status'], rb.STATUS_INCOMPLETE)
        self.assertTrue(any('valid samples' in issue for issue in profile['issues']))

    def test_incomplete_cannot_masquerade_as_complete(self):
        profile = self.complete([4096])
        profile['status'] = rb.STATUS_INCOMPLETE
        profile['fingerprint'] = rb.profile_fingerprint(profile)
        rb.validate_profile(profile)  # an incomplete candidate with one sample is legal
        profile['status'] = rb.STATUS_COMPLETE
        profile['fingerprint'] = rb.profile_fingerprint(profile)
        with self.assertRaises(rb.BenchmarkError):
            rb.validate_profile(profile)


# ------------------------------------------------- candidate path safety -----

class CandidatePathTests(BenchBase):
    def writer(self):
        return rb.ProfileWriter(os.path.join(self.dir, 'resource-profile.json'))

    def test_default_candidate_is_distinct(self):
        writer = self.writer()
        self.assertIsNone(writer.candidate_conflict(writer.candidate_path()))

    def test_candidate_on_formal_is_rejected(self):
        writer = self.writer()
        self.assertIsNotNone(writer.candidate_conflict(writer.path))

    def test_candidate_on_backup_is_rejected(self):
        writer = self.writer()
        self.assertIsNotNone(writer.candidate_conflict(writer.backup_path))

    def test_candidate_alias_is_rejected(self):
        writer = self.writer()
        alias = os.path.join(self.dir, '.', 'resource-profile.json')
        self.assertIsNotNone(writer.candidate_conflict(alias))

    def test_candidate_symlink_to_formal_is_rejected(self):
        writer = self.writer()
        Path(writer.path).write_bytes(rb.dumps_profile(complete_profile()))
        link = os.path.join(self.dir, 'sneaky.json')
        os.symlink(writer.path, link)
        self.assertIsNotNone(writer.candidate_conflict(link))

    def test_benchmark_cli_refuses_candidate_on_formal(self):
        formal = os.path.join(self.dir, 'resource-profile.json')
        code, _out, _err = run_cli(['benchmark', '--profile', formal, '--candidate', formal,
                                    '--root', self.dir])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(formal))

    def test_benchmark_cli_refuses_candidate_on_backup(self):
        formal = os.path.join(self.dir, 'resource-profile.json')
        code, _out, _err = run_cli(['benchmark', '--profile', formal,
                                    '--candidate', formal + '.bak', '--root', self.dir])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(formal + '.bak'))


# ------------------------------------- commit-uncertain cross-process (P0-4) --

class CommitRecoveryTests(BenchBase):
    def writer(self):
        return rb.ProfileWriter(os.path.join(self.dir, 'resource-profile.json'))

    def test_uncertain_state_survives_writer_recreation(self):
        first = self.writer()
        with mock.patch.object(rb, '_fsync_dir', side_effect=OSError('boom')):
            result = first.install(rb.dumps_profile(complete_profile()), confirmed=True)
        self.assertEqual(result['state'], rb.COMMIT_UNCERTAIN)
        self.assertTrue(os.path.exists(first.journal_path))
        del first
        second = self.writer()
        self.assertTrue(second.uncertain)
        blocked = second.install(rb.dumps_profile(complete_profile()), force=True, confirmed=True)
        self.assertEqual(blocked['state'], rb.COMMIT_UNCERTAIN)
        state = second.recheck()
        self.assertTrue(state['formal_valid'])
        self.assertTrue(state['released'])
        self.assertFalse(os.path.exists(second.journal_path))
        committed = second.install(rb.dumps_profile(complete_profile()), force=True, confirmed=True)
        self.assertEqual(committed['state'], rb.COMMITTED)

    def test_force_does_not_bypass_recovery(self):
        first = self.writer()
        with mock.patch.object(rb, '_fsync_dir', side_effect=OSError('boom')):
            first.install(rb.dumps_profile(complete_profile()), confirmed=True)
        second = self.writer()
        blocked = second.install(rb.dumps_profile(complete_profile()), force=True, confirmed=True)
        self.assertEqual(blocked['state'], rb.COMMIT_UNCERTAIN)

    def test_recheck_keeps_the_block_when_state_is_unconfirmable(self):
        writer = self.writer()
        writer.mark_uncertain('simulated crash before any durable state')
        state = writer.recheck()
        self.assertFalse(state['recoverable'])
        self.assertFalse(state['released'])
        self.assertTrue(state['uncertain'])

    def test_marker_creation_failure_is_fail_closed(self):
        writer = self.writer()
        with mock.patch.object(rb, '_atomic_write', side_effect=OSError('no space')), \
                mock.patch('builtins.open', side_effect=OSError('no space')):
            result = writer.mark_uncertain('marker could not be created')
        self.assertEqual(result['state'], rb.COMMIT_UNCERTAIN)
        self.assertFalse(result['marker_persisted'])
        self.assertTrue(writer.uncertain)
        self.assertFalse(os.path.exists(writer.journal_path))
        blocked = writer.install(rb.dumps_profile(complete_profile()), force=True, confirmed=True)
        self.assertEqual(blocked['state'], rb.COMMIT_UNCERTAIN)

    def test_marker_fsync_failure_is_fail_closed(self):
        writer = self.writer()
        with mock.patch.object(rb, '_atomic_write', side_effect=OSError('atomic failed')), \
                mock.patch('os.fsync', side_effect=OSError('fsync failed')):
            result = writer.mark_uncertain('marker fsync failed')
        self.assertEqual(result['state'], rb.COMMIT_UNCERTAIN)
        self.assertFalse(result['marker_persisted'])
        self.assertTrue(writer.uncertain)

    def test_corrupt_marker_keeps_the_block(self):
        writer = self.writer()
        Path(writer.journal_path).write_bytes(b'{not json')
        blocked = writer.install(rb.dumps_profile(complete_profile()), force=True, confirmed=True)
        self.assertEqual(blocked['state'], rb.COMMIT_UNCERTAIN)
        state = writer.recheck()
        self.assertEqual(state['marker'], 'corrupt')
        self.assertFalse(state['released'])
        self.assertTrue(state['uncertain'])
        self.assertTrue(os.path.exists(writer.journal_path))

    def test_unreadable_marker_is_fail_closed(self):
        writer = self.writer()
        Path(writer.journal_path).write_bytes(b'{}')
        with mock.patch.object(rb.os, 'lstat', side_effect=OSError('io error')):
            self.assertTrue(writer._journal_present())
            state = writer.recheck()
        self.assertFalse(state['released'])

    def test_non_regular_marker_keeps_the_block(self):
        writer = self.writer()
        os.mkdir(writer.journal_path)
        state = writer.recheck()
        self.assertEqual(state['marker'], 'not a regular file')
        self.assertFalse(state['released'])
        blocked = writer.install(rb.dumps_profile(complete_profile()), confirmed=True)
        self.assertEqual(blocked['state'], rb.COMMIT_UNCERTAIN)

    def test_recheck_keeps_the_block_when_no_recoverable_file(self):
        writer = self.writer()
        writer.mark_uncertain('crash before any durable state')
        self.assertEqual(writer._journal_status(), 'ok')
        state = writer.recheck()
        self.assertFalse(state['recoverable'])
        self.assertFalse(state['released'])
        self.assertTrue(state['uncertain'])
        self.assertTrue(os.path.exists(writer.journal_path))


# ------------------------------------------ protected-file safety (P1-6) ------

class ProtectedFileSafetyTests(BenchBase):
    def writer(self, name='resource-profile.json'):
        return rb.ProfileWriter(os.path.join(self.dir, name))

    def test_new_formal_is_created_with_strict_permissions(self):
        writer = self.writer()
        writer.install(rb.dumps_profile(complete_profile()), confirmed=True)
        self.assertEqual(os.stat(writer.path).st_mode & 0o777, 0o600)

    def test_read_refuses_to_follow_a_symlink(self):
        target = os.path.join(self.dir, 'real.json')
        Path(target).write_bytes(rb.dumps_profile(complete_profile()))
        link = os.path.join(self.dir, 'link.json')
        os.symlink(target, link)
        raw, reason = rb.ProfileWriter._read_file(link)
        self.assertIsNone(raw)
        self.assertIn('symbolic', reason)

    def test_install_aborts_on_a_symlinked_formal(self):
        target = os.path.join(self.dir, 'real.json')
        keep = rb.dumps_profile(complete_profile(reserve=2048))
        Path(target).write_bytes(keep)
        writer = self.writer()
        os.symlink(target, writer.path)
        result = writer.install(rb.dumps_profile(complete_profile()), force=True, confirmed=True)
        self.assertEqual(result['state'], rb.ABORTED)
        with open(target, 'rb') as handle:
            self.assertEqual(handle.read(), keep)

    def test_install_aborts_on_a_symlinked_backup(self):
        writer = self.writer()
        writer.install(rb.dumps_profile(complete_profile(reserve=1024)), confirmed=True)
        os.symlink(os.path.join(self.dir, 'elsewhere.json'), writer.backup_path)
        result = writer.install(rb.dumps_profile(complete_profile(reserve=2048)), force=True, confirmed=True)
        self.assertEqual(result['state'], rb.ABORTED)

    def test_world_writable_parent_directory_is_rejected(self):
        unsafe = os.path.join(self.dir, 'open')
        os.mkdir(unsafe)
        os.chmod(unsafe, 0o777)
        writer = rb.ProfileWriter(os.path.join(unsafe, 'resource-profile.json'))
        result = writer.install(rb.dumps_profile(complete_profile()), confirmed=True)
        self.assertEqual(result['state'], rb.ABORTED)

    def test_invalid_formal_is_not_reused_as_backup(self):
        writer = self.writer()
        Path(writer.path).write_bytes(b'not a profile')
        result = writer.install(rb.dumps_profile(complete_profile()), confirmed=True)
        self.assertEqual(result['state'], rb.COMMITTED)
        self.assertFalse(result['backup_written'])
        self.assertFalse(os.path.exists(writer.backup_path))

    def test_recheck_treats_an_invalid_formal_as_invalid(self):
        writer = self.writer()
        Path(writer.path).write_bytes(b'not a profile')
        state = writer.recheck()
        self.assertFalse(state['formal_valid'])
        self.assertFalse(state['backup_valid'])

    # --- descriptor-level (TOCTOU) verification -----------------------------

    def test_read_detects_a_descriptor_that_is_not_the_named_file(self):
        target = os.path.join(self.dir, 'f.json')
        Path(target).write_bytes(b'{}')
        real_fstat = os.fstat

        def fake_fstat(fd):
            return _mutate_stat(real_fstat(fd), ino=real_fstat(fd).st_ino + 1)

        with mock.patch.object(rb.os, 'fstat', side_effect=fake_fstat):
            raw, reason = rb.ProfileWriter._read_file(target)
        self.assertIsNone(raw)
        self.assertIn('changed during open', reason)

    def test_read_detects_a_replacement_after_open(self):
        target = os.path.join(self.dir, 'f.json')
        Path(target).write_bytes(b'{}')
        real_lstat = os.lstat
        calls = itertools.count()

        def fake_lstat(path, *args, **kwargs):
            st = real_lstat(path, *args, **kwargs)
            if os.fspath(path) == target and next(calls) >= 1:
                return _mutate_stat(st, ino=st.st_ino + 7)
            return st

        with mock.patch.object(rb.os, 'lstat', side_effect=fake_lstat):
            raw, reason = rb.ProfileWriter._read_file(target)
        self.assertIsNone(raw)
        self.assertIn('changed during read', reason)

    def test_read_rejects_a_non_regular_file(self):
        target = os.path.join(self.dir, 'adir')
        os.mkdir(target)
        raw, reason = rb.ProfileWriter._read_file(target)
        self.assertIsNone(raw)
        self.assertIn('not a regular file', reason)

    def test_read_rejects_a_group_or_world_writable_file(self):
        target = os.path.join(self.dir, 'f.json')
        Path(target).write_bytes(b'{}')
        os.chmod(target, 0o666)
        raw, reason = rb.ProfileWriter._read_file(target)
        self.assertIsNone(raw)
        self.assertIn('writable', reason)

    def test_read_rejects_an_untrusted_file_owner(self):
        target = os.path.join(self.dir, 'f.json')
        Path(target).write_bytes(b'{}')
        real_fstat = os.fstat

        def fake_fstat(fd):
            return _mutate_stat(real_fstat(fd), uid=424242)

        with mock.patch.object(rb.os, 'fstat', side_effect=fake_fstat):
            raw, reason = rb.ProfileWriter._read_file(target)
        self.assertIsNone(raw)
        self.assertIn('uid', reason)

    def test_read_rejects_an_untrusted_parent_directory_owner(self):
        target_dir = os.path.abspath(self.dir)
        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            st = real_stat(path, *args, **kwargs)
            if os.fspath(path) == target_dir:
                return _mutate_stat(st, uid=424242)
            return st

        with mock.patch.object(rb.os, 'stat', side_effect=fake_stat):
            raw, reason = rb.ProfileWriter._read_file(os.path.join(self.dir, 'p.json'))
        self.assertIsNone(raw)
        self.assertIn('owned by uid', reason)

    def test_group_writable_parent_directory_is_rejected(self):
        unsafe = os.path.join(self.dir, 'grp')
        os.mkdir(unsafe)
        os.chmod(unsafe, 0o770)
        raw, reason = rb.ProfileWriter._read_file(os.path.join(unsafe, 'p.json'))
        self.assertIsNone(raw)
        self.assertIn('group-writable', reason)

    def test_parent_that_is_not_a_directory_is_rejected(self):
        plain = os.path.join(self.dir, 'plain')
        Path(plain).write_bytes(b'x')
        raw, reason = rb.ProfileWriter._read_file(os.path.join(plain, 'p.json'))
        self.assertIsNone(raw)
        self.assertIn('not a directory', reason)

    def test_descriptor_is_closed_on_a_read_error(self):
        target = os.path.join(self.dir, 'f.json')
        Path(target).write_bytes(b'{}')
        seen = {}
        real_open, real_close = os.open, os.close

        def tracking_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            seen['fd'] = fd
            return fd

        def tracking_close(fd):
            if seen.get('fd') == fd:
                seen['closed'] = True
            return real_close(fd)

        with mock.patch.object(rb.os, 'open', side_effect=tracking_open), \
                mock.patch.object(rb.os, 'close', side_effect=tracking_close), \
                mock.patch.object(rb, '_read_fd', side_effect=OSError('boom')):
            raw, reason = rb.ProfileWriter._read_file(target)
        self.assertIsNone(raw)
        self.assertIn('unreadable', reason)
        self.assertTrue(seen.get('closed', False))

    def test_descriptor_is_closed_on_success(self):
        target = os.path.join(self.dir, 'f.json')
        Path(target).write_bytes(b'{}')
        seen = {}
        real_open, real_close = os.open, os.close

        def tracking_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            seen['fd'] = fd
            return fd

        def tracking_close(fd):
            if seen.get('fd') == fd:
                seen['closed'] = True
            return real_close(fd)

        with mock.patch.object(rb.os, 'open', side_effect=tracking_open), \
                mock.patch.object(rb.os, 'close', side_effect=tracking_close):
            raw, reason = rb.ProfileWriter._read_file(target)
        self.assertEqual(raw, b'{}')
        self.assertIsNone(reason)
        self.assertTrue(seen.get('closed', False))


# ------------------------------------------- optimized-mode safety (-O) -------

class OptimizedModeSafetyTests(BenchBase):
    def test_writer_safety_survives_optimized_mode(self):
        code = (
            "import os, tempfile\n"
            "import resource_benchmark as rb\n"
            "d = tempfile.mkdtemp()\n"
            "p = os.path.join(d, 'resource-profile.json')\n"
            "w = rb.ProfileWriter(p)\n"
            "if w.candidate_conflict(p) is None:\n"
            "    raise SystemExit('FAIL: candidate conflict not detected')\n"
            "result = w.install(b'{}', confirmed=True)\n"
            "if result['state'] != rb.ABORTED:\n"
            "    raise SystemExit('FAIL: incomplete candidate accepted')\n"
            "print('PASS')\n"
        )
        proc = subprocess.run([sys.executable, '-O', '-c', code], cwd=REPO,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn('PASS', proc.stdout)


if __name__ == '__main__':
    unittest.main()
