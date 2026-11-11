import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import resource_probe as rp

REPO = Path(__file__).resolve().parents[1]

DEFAULT_MEMINFO = (
    'MemTotal:        2000000 kB\n'
    'MemAvailable:    1500000 kB\n'
    'SwapTotal:             0 kB\n'
    'SwapFree:              0 kB\n'
)
DEFAULT_PSI = (
    'some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n'
    'full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n'
)
DEFAULT_EVENTS = (
    'low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n'
)


class Fixture:
    """Builds a temporary /proc + cgroup tree for one probe namespace."""

    def __init__(self, root):
        self.root = Path(root)

    def put(self, rel, text):
        target = self.root / rel.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def level(self, rel, current, maximum, events=DEFAULT_EVENTS, peak=None):
        base = rel.rstrip('/')
        self.put(base + '/memory.current', str(current))
        self.put(base + '/memory.max', str(maximum))
        if events is not None:
            self.put(base + '/memory.events', events)
        if peak is not None:
            self.put(base + '/memory.peak', str(peak))

    def standard(self, mem_available=1500000, swap=0, leaf_max=200000000,
                 ancestor_max='max', self_path='/system.slice/node-panel.service',
                 mount_point='/sys/fs/cgroup', events=DEFAULT_EVENTS, psi=DEFAULT_PSI,
                 mem_total=2000000):
        meminfo = ('MemTotal:        %d kB\n'
                   'MemAvailable:    %d kB\n'
                   'SwapTotal:             %d kB\n'
                   'SwapFree:              %d kB\n' % (mem_total, mem_available, swap, swap))
        self.put('proc/meminfo', meminfo)
        self.put('proc/self/cgroup', '0::' + self_path + '\n')
        self.put('proc/self/mountinfo',
                 '29 23 0:26 / %s rw - cgroup2 cgroup2 rw\n' % mount_point)
        if psi is not None:
            self.put('proc/pressure/memory', psi)
        prefix = mount_point.rstrip('/')
        self.level(prefix + self_path, 1000000, leaf_max, events=events, peak=1200000)
        self.level(prefix + '/system.slice', 5000000, ancestor_max)
        self.level(prefix, 9000000, 'max')
        return self

    def source(self):
        return rp.Source(self.root)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._extra = []
        self.a = Fixture(self.tmp.name)

    def tearDown(self):
        for extra in self._extra:
            extra.cleanup()
        self.tmp.cleanup()

    def second(self):
        extra = tempfile.TemporaryDirectory()
        self._extra.append(extra)
        return Fixture(extra.name)

    # ------------------------------------------------------------- meminfo --

    def test_meminfo_normal(self):
        self.a.put('proc/meminfo', DEFAULT_MEMINFO)
        info = rp.read_meminfo(self.a.source())
        self.assertEqual(info['MemTotal'], 2000000 * 1024)
        self.assertEqual(info['MemAvailable'], 1500000 * 1024)
        self.assertEqual(info['SwapTotal'], 0)

    def test_meminfo_missing_memavailable(self):
        self.a.put('proc/meminfo', 'MemTotal: 100 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n')
        with self.assertRaises(rp.MemInfoError):
            rp.read_meminfo(self.a.source())

    def test_meminfo_missing_memtotal(self):
        self.a.put('proc/meminfo', 'MemAvailable: 100 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n')
        with self.assertRaises(rp.MemInfoError):
            rp.read_meminfo(self.a.source())

    def test_meminfo_illegal_number(self):
        self.a.put('proc/meminfo',
                   'MemTotal: abc kB\nMemAvailable: 1 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n')
        with self.assertRaises(rp.FormatError):
            rp.read_meminfo(self.a.source())

    def test_meminfo_duplicate_field(self):
        self.a.put('proc/meminfo',
                   'MemTotal: 1 kB\nMemTotal: 2 kB\nMemAvailable: 1 kB\n'
                   'SwapTotal: 0 kB\nSwapFree: 0 kB\n')
        with self.assertRaises(rp.FormatError):
            rp.read_meminfo(self.a.source())

    def test_meminfo_bad_unit(self):
        self.a.put('proc/meminfo',
                   'MemTotal: 100 MB\nMemAvailable: 1 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n')
        with self.assertRaises(rp.FormatError):
            rp.read_meminfo(self.a.source())

    def test_meminfo_negative(self):
        self.a.put('proc/meminfo',
                   'MemTotal: -5 kB\nMemAvailable: 1 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n')
        with self.assertRaises(rp.FormatError):
            rp.read_meminfo(self.a.source())

    def test_meminfo_oversize(self):
        self.a.put('proc/meminfo', DEFAULT_MEMINFO)
        with mock.patch.object(rp, 'MEMINFO_LIMIT', 16):
            with self.assertRaises(rp.TooLarge):
                rp.read_meminfo(self.a.source())

    def test_meminfo_missing_file(self):
        with self.assertRaises(rp.MissingFile):
            rp.read_meminfo(self.a.source())

    # -------------------------------------------------------------- cgroup --

    def test_cgroup_normal(self):
        self.a.standard()
        info = rp.read_cgroup(self.a.source())
        self.assertTrue(info['supported'])
        self.assertEqual(info['self_path'], '/system.slice/node-panel.service')
        paths = [level['path'] for level in info['levels']]
        self.assertEqual(paths, ['/system.slice/node-panel.service', '/system.slice', '/'])
        self.assertEqual(info['levels'][0]['max'], 200000000)
        self.assertFalse(info['levels'][0]['max_unlimited'])
        self.assertIsNone(info['levels'][2]['max'])
        self.assertTrue(info['levels'][2]['max_unlimited'])
        self.assertEqual(info['levels'][0]['peak'], 1200000)

    def test_cgroup_mount_root_not_default(self):
        self.a.put('proc/self/cgroup', '0::/nsroot/system.slice/node-panel.service\n')
        self.a.put('proc/self/mountinfo',
                   '29 23 0:26 /nsroot /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n')
        prefix = '/sys/fs/cgroup'
        self.a.level(prefix + '/system.slice/node-panel.service', 1000000, 200000000)
        self.a.level(prefix + '/system.slice', 5000000, 'max')
        self.a.level(prefix, 9000000, 'max')
        info = rp.read_cgroup(self.a.source())
        self.assertEqual(info['mount_root'], '/nsroot')
        self.assertEqual([l['path'] for l in info['levels']],
                         ['/nsroot/system.slice/node-panel.service', '/nsroot/system.slice', '/nsroot'])

    def test_cgroup_mount_point_not_default(self):
        self.a.standard(mount_point='/cgroup')
        info = rp.read_cgroup(self.a.source())
        self.assertEqual(info['mount_point'], '/cgroup')
        self.assertEqual(info['levels'][0]['dir'], '/cgroup/system.slice/node-panel.service')

    def test_namespace_path_mount_root_default(self):
        point = '/sys/fs/cgroup'
        self.assertEqual(rp._namespace_path(point + '/system.slice/node-panel.service', point, '/'),
                         '/system.slice/node-panel.service')
        self.assertEqual(rp._namespace_path(point + '/system.slice', point, '/'), '/system.slice')
        self.assertEqual(rp._namespace_path(point, point, '/'), '/')

    def test_namespace_path_mount_root_custom(self):
        point = '/sys/fs/cgroup'
        self.assertEqual(rp._namespace_path(point + '/system.slice/node-panel.service', point, '/nsroot'),
                         '/nsroot/system.slice/node-panel.service')
        self.assertEqual(rp._namespace_path(point + '/system.slice', point, '/nsroot'),
                         '/nsroot/system.slice')
        self.assertEqual(rp._namespace_path(point, point, '/nsroot'), '/nsroot')

    def test_cgroup_numeric_ancestor_limit(self):
        self.a.standard(ancestor_max=100000000)
        info = rp.read_cgroup(self.a.source())
        self.assertEqual(info['levels'][1]['max'], 100000000)

    def test_cgroup_missing_required_file(self):
        self.a.standard()
        (self.a.root / 'sys/fs/cgroup/system.slice/memory.max').unlink()
        with self.assertRaises(rp.MissingFile):
            rp.read_cgroup(self.a.source())

    def test_cgroup_optional_peak_absent(self):
        self.a.standard()
        (self.a.root / 'sys/fs/cgroup/system.slice/node-panel.service/memory.peak').unlink()
        info = rp.read_cgroup(self.a.source())
        self.assertIsNone(info['levels'][0]['peak'])
        self.assertEqual(info['levels'][0]['peak_status'], 'absent')

    def test_cgroup_peak_error_is_optional(self):
        self.a.standard()
        source = self.a.source()
        real_open = open

        def fake_open(path, *args, **kwargs):
            if str(path).endswith('memory.peak'):
                raise PermissionError('denied')
            return real_open(path, *args, **kwargs)

        with mock.patch('builtins.open', side_effect=fake_open):
            level = rp.read_cgroup_level(source, '/sys/fs/cgroup/system.slice', '/system.slice')
        self.assertEqual(level['peak_status'], 'error')
        self.assertIsNone(level['peak'])

    def test_cgroup_path_escape(self):
        self.a.put('proc/self/cgroup', '0::/../etc\n')
        with self.assertRaises(rp.CgroupError):
            rp.read_self_cgroup(self.a.source())

    def test_cgroup_illegal_number(self):
        self.a.standard()
        (self.a.root / 'sys/fs/cgroup/system.slice/node-panel.service/memory.current').write_text('abc')
        with self.assertRaises(rp.FormatError):
            rp.read_cgroup(self.a.source())

    def test_cgroup_mapped_dir_absent(self):
        self.a.put('proc/self/cgroup', '0::/system.slice/node-panel.service\n')
        self.a.put('proc/self/mountinfo',
                   '29 23 0:26 / /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n')
        with self.assertRaises(rp.CgroupError):
            rp.read_cgroup(self.a.source())

    def test_cgroup_no_v2_entry(self):
        self.a.put('proc/self/cgroup', '1:name=systemd:/x\n')
        with self.assertRaises(rp.CgroupUnsupported):
            rp.read_self_cgroup(self.a.source())

    def test_cgroup_no_cgroup2_mount(self):
        self.a.put('proc/self/mountinfo',
                   '29 23 0:26 / / rw - ext4 /dev/sda1 rw\n')
        with self.assertRaises(rp.CgroupUnsupported):
            rp.read_mountinfo(self.a.source())

    def test_ancestry_outside_mount_refused(self):
        with self.assertRaises(rp.CgroupError):
            rp._ancestry('/other/path', '/sys/fs/cgroup')

    def test_mountinfo_escaped_mount_point(self):
        self.a.put('proc/self/mountinfo',
                   '29 23 0:26 / /sys/fs/cgroup\\040two rw - cgroup2 cgroup2 rw\n')
        info = rp.read_mountinfo(self.a.source())
        self.assertEqual(info['mount_point'], '/sys/fs/cgroup two')

    def test_cgroup_symlink_component_refused(self):
        self.a.put('proc/meminfo', DEFAULT_MEMINFO)
        self.a.put('proc/self/cgroup', '0::/system.slice/node-panel.service\n')
        self.a.put('proc/self/mountinfo',
                   '29 23 0:26 / /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n')
        # the real files live outside the cgroup mount; a symlink bridges to them
        self.a.put('escape/node-panel.service/memory.current', '1000')
        self.a.put('escape/node-panel.service/memory.max', 'max')
        self.a.put('escape/node-panel.service/memory.events', DEFAULT_EVENTS)
        (self.a.root / 'sys/fs/cgroup').mkdir(parents=True, exist_ok=True)
        os.symlink(str(self.a.root / 'escape'),
                   str(self.a.root / 'sys/fs/cgroup/system.slice'))
        with self.assertRaises(rp.CgroupError):
            rp.read_cgroup(self.a.source())

    def test_cgroup_leaf_symlink_refused(self):
        self.a.put('proc/meminfo', DEFAULT_MEMINFO)
        self.a.put('proc/self/cgroup', '0::/system.slice/node-panel.service\n')
        self.a.put('proc/self/mountinfo',
                   '29 23 0:26 / /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n')
        self.a.put('outside/memory.current', '1000')
        self.a.put('outside/memory.max', 'max')
        self.a.put('outside/memory.events', DEFAULT_EVENTS)
        (self.a.root / 'sys/fs/cgroup/system.slice').mkdir(parents=True, exist_ok=True)
        os.symlink(str(self.a.root / 'outside'),
                   str(self.a.root / 'sys/fs/cgroup/system.slice/node-panel.service'))
        with self.assertRaises(rp.CgroupError):
            rp.read_cgroup(self.a.source())

    # ------------------------------------------------------ _assert_within_mount --

    def test_assert_within_mount_accepts_child_of_root_mount(self):
        # A cgroup2 mount at '/' must accept every legal descendant: the old
        # 'mount_real + os.sep' prefix test compared against '//' and refused
        # them all. Source('/') makes the real path of the mount be '/'.
        source = rp.Source('/')
        rp._assert_within_mount(source, '/', str(self.tmp.name))
        rp._assert_within_mount(source, '/', '/')

    def test_assert_within_mount_accepts_child(self):
        self.a.put('sys/fs/cgroup/system.slice/memory.current', '1')
        rp._assert_within_mount(self.a.source(), '/sys/fs/cgroup',
                                '/sys/fs/cgroup/system.slice')

    def test_assert_within_mount_refuses_outside(self):
        with self.assertRaises(rp.CgroupError):
            rp._assert_within_mount(self.a.source(), '/sys/fs/cgroup', '/other/path')

    def test_assert_within_mount_refuses_prefix_lookalike(self):
        with self.assertRaises(rp.CgroupError):
            rp._assert_within_mount(self.a.source(), '/sys/fs/cgroup', '/sys/fs/cgroup2')

    def test_assert_within_mount_refuses_symlink_below_mount(self):
        self.a.put('escape/memory.current', '1')
        (self.a.root / 'sys/fs/cgroup').mkdir(parents=True, exist_ok=True)
        os.symlink(str(self.a.root / 'escape'),
                   str(self.a.root / 'sys/fs/cgroup/system.slice'))
        with self.assertRaises(rp.CgroupError):
            rp._assert_within_mount(self.a.source(), '/sys/fs/cgroup',
                                    '/sys/fs/cgroup/system.slice/node-panel.service')

    # ------------------------------------------------------- Source.root guard --

    def test_source_rejects_parent_component(self):
        outside = self.second()
        outside.put('secret.txt', 'top-secret')
        rel = os.path.relpath(str(outside.root), str(self.a.root))  # '../<sibling>'
        with self.assertRaises(rp.ReadError):
            self.a.source().read_text('/' + rel + '/secret.txt', rp.MEMINFO_LIMIT)

    def test_source_rejects_deep_parent_component(self):
        outside = self.second()
        outside.put('secret.txt', 'top-secret')
        nested = self.a.root / 'proc' / 'deep' / 'nested'
        nested.mkdir(parents=True)
        rel = os.path.relpath(str(outside.root), str(nested))  # '../../../../<sibling>'
        with self.assertRaises(rp.ReadError):
            self.a.source().read_text('/proc/deep/nested/' + rel + '/secret.txt',
                                      rp.MEMINFO_LIMIT)

    def test_source_rejects_absolute_symlink_escape(self):
        secret = self.second().put('secret.txt', 'top-secret')
        os.symlink(str(secret), str(self.a.root / 'link'))
        with self.assertRaises(rp.ReadError):
            self.a.source().read_text('/link', rp.MEMINFO_LIMIT)

    def test_source_rejects_relative_symlink_escape(self):
        secret = self.second().put('secret.txt', 'top-secret')
        rel = os.path.relpath(str(secret), str(self.a.root))
        os.symlink(rel, str(self.a.root / 'link'))
        with self.assertRaises(rp.ReadError):
            self.a.source().read_text('/link', rp.MEMINFO_LIMIT)

    def test_source_allows_internal_symlink(self):
        self.a.put('proc/meminfo', DEFAULT_MEMINFO)
        os.symlink('meminfo', str(self.a.root / 'proc/alias'))
        text = self.a.source().read_text('/proc/alias', rp.MEMINFO_LIMIT)
        self.assertIn('MemTotal', text)

    # -------------------------------------------------------------- events --

    def test_events_missing_required(self):
        bad = 'low 0\nhigh 0\nmax 0\noom 0\n'
        with self.assertRaises(rp.FormatError):
            rp._parse_events(bad)

    def test_events_illegal_field(self):
        bad = 'low 0\nhigh 0\nmax 0\noom -1\noom_kill 0\n'
        with self.assertRaises(rp.FormatError):
            rp._parse_events(bad)

    def test_events_low_absent_tolerated(self):
        events, low_present = rp._parse_events('high 0\nmax 0\noom 0\noom_kill 0\n')
        self.assertFalse(low_present)
        self.assertEqual(events['low'], 0)
        self.assertEqual(events['oom_kill'], 0)

    # ----------------------------------------------------------------- psi --

    def test_psi_normal(self):
        self.a.put('proc/pressure/memory', DEFAULT_PSI)
        psi = rp.read_psi(self.a.source())
        self.assertTrue(psi['supported'])
        self.assertEqual(psi['some']['total'], 0)
        self.assertEqual(psi['full']['avg10'], 0.0)

    def test_psi_decimal_and_total(self):
        self.a.put('proc/pressure/memory',
                   'some avg10=1.25 avg60=0.50 avg300=0.10 total=42\n'
                   'full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n')
        psi = rp.read_psi(self.a.source())
        self.assertEqual(psi['some']['avg10'], 1.25)
        self.assertIsInstance(psi['some']['total'], int)
        self.assertEqual(psi['some']['total'], 42)

    def test_psi_missing_but_dir_present(self):
        (self.a.root / 'proc/pressure').mkdir(parents=True)
        psi = rp.read_psi(self.a.source())
        self.assertFalse(psi['supported'])
        self.assertEqual(psi['status'], 'missing')

    def test_psi_unsupported(self):
        psi = rp.read_psi(self.a.source())
        self.assertFalse(psi['supported'])
        self.assertEqual(psi['status'], 'unsupported')

    def test_psi_permission(self):
        self.a.put('proc/pressure/memory', DEFAULT_PSI)
        source = self.a.source()
        real_open = open

        def fake_open(path, *args, **kwargs):
            if str(path).endswith('pressure/memory'):
                raise PermissionError('denied')
            return real_open(path, *args, **kwargs)

        with mock.patch('builtins.open', side_effect=fake_open):
            psi = rp.read_psi(source)
        self.assertFalse(psi['supported'])
        self.assertEqual(psi['status'], 'permission')

    def test_source_converts_permission_error(self):
        self.a.put('proc/meminfo', DEFAULT_MEMINFO)
        source = self.a.source()

        def fake_open(path, *args, **kwargs):
            raise PermissionError('denied')

        with mock.patch('builtins.open', side_effect=fake_open):
            with self.assertRaises(rp.PermissionDenied):
                source.read_text('/proc/meminfo', rp.MEMINFO_LIMIT)

    def test_psi_malformed(self):
        self.a.put('proc/pressure/memory', 'garbage\n')
        psi = rp.read_psi(self.a.source())
        self.assertFalse(psi['supported'])
        self.assertEqual(psi['status'], 'malformed')

    def test_psi_missing_section(self):
        self.a.put('proc/pressure/memory', 'some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n')
        psi = rp.read_psi(self.a.source())
        self.assertFalse(psi['supported'])
        self.assertEqual(psi['status'], 'malformed')

    def test_psi_invalid_utf8(self):
        target = self.a.put('proc/pressure/memory', DEFAULT_PSI)
        target.write_bytes(b'some avg10=\xff\xfe avg60=0.00 avg300=0.00 total=0\n'
                           b'full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n')
        psi = rp.read_psi(self.a.source())
        self.assertFalse(psi['supported'])
        self.assertEqual(psi['status'], 'malformed')

    def test_snapshot_survives_invalid_utf8_psi(self):
        self.a.standard(psi=None)
        target = self.a.root / 'proc/pressure/memory'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'\xff\xfe\x00garbage')
        snap = rp.snapshot(source=self.a.source(),
                           system={'cpu_count': 1, 'arch': 'x', 'page_size': 4096})
        self.assertTrue(snap['valid'])
        self.assertFalse(snap['psi']['supported'])
        self.assertEqual(snap['psi']['status'], 'malformed')

    def test_psi_unknown_field_rejected(self):
        self.a.put('proc/pressure/memory',
                   'some avg10=0.00 avg60=0.00 avg300=0.00 total=0 bogus=1\n'
                   'full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n')
        psi = rp.read_psi(self.a.source())
        self.assertFalse(psi['supported'])
        self.assertEqual(psi['status'], 'malformed')

    def test_psi_unknown_avg_window_rejected(self):
        self.a.put('proc/pressure/memory',
                   'some avg10=0.00 avg60=0.00 avg300=0.00 avg999=0.00 total=0\n'
                   'full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n')
        psi = rp.read_psi(self.a.source())
        self.assertFalse(psi['supported'])
        self.assertEqual(psi['status'], 'malformed')

    def test_psi_duplicate_field_rejected(self):
        self.a.put('proc/pressure/memory',
                   'some avg10=0.00 avg10=0.00 avg60=0.00 avg300=0.00 total=0\n'
                   'full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n')
        psi = rp.read_psi(self.a.source())
        self.assertFalse(psi['supported'])
        self.assertEqual(psi['status'], 'malformed')

    def test_parse_psi_field_rules(self):
        with self.assertRaises(rp.FormatError):
            rp._parse_psi('some avg10=0.00 avg60=0.00 avg300=0.00 total=0 extra=1\n'
                          'full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n')
        with self.assertRaises(rp.FormatError):
            rp._parse_psi('some avg10=0.00 avg10=0.00 avg60=0.00 avg300=0.00 total=0\n'
                          'full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n')

    # ------------------------------------------------------------ snapshot --

    def test_snapshot_valid(self):
        self.a.standard()
        snap = rp.snapshot(source=self.a.source(),
                           system={'cpu_count': 1, 'arch': 'x86_64', 'page_size': 4096},
                           clock=lambda: __import__('datetime').datetime(2026, 1, 1))
        self.assertTrue(snap['valid'])
        self.assertEqual(snap['probe_version'], rp.PROBE_VERSION)
        self.assertEqual(len(snap['fingerprint']), 64)
        self.assertEqual(snap['generated_at'], '2026-01-01T00:00:00')

    def test_snapshot_invalid_without_cgroup(self):
        self.a.put('proc/meminfo', DEFAULT_MEMINFO)
        snap = rp.snapshot(source=self.a.source(), system={'cpu_count': 1, 'arch': 'x', 'page_size': 4096})
        self.assertFalse(snap['valid'])
        self.assertIsNone(snap['fingerprint'])
        self.assertFalse(snap['cgroup']['supported'])

    def test_fingerprint_excludes_dynamic_values(self):
        self.a.standard(mem_available=1500000)
        first = rp.snapshot(source=self.a.source(), system={'cpu_count': 1, 'arch': 'x', 'page_size': 4096})
        b = self.second().standard(mem_available=1000000)
        second = rp.snapshot(source=b.source(), system={'cpu_count': 1, 'arch': 'x', 'page_size': 4096})
        self.assertEqual(first['fingerprint'], second['fingerprint'])

    # ------------------------------------------------------------- compare --

    def test_static_consistent(self):
        self.a.standard()
        b = self.second().standard()
        first = rp.snapshot(source=self.a.source(), system={'cpu_count': 1, 'arch': 'x', 'page_size': 4096})
        second = rp.snapshot(source=b.source(), system={'cpu_count': 1, 'arch': 'x', 'page_size': 4096})
        self.assertTrue(rp.compare_static(first, second)['consistent'])

    def test_static_swaptotal_change(self):
        self.a.standard()
        b = self.second().standard(swap=2048)
        first = rp.snapshot(source=self.a.source(), system={'cpu_count': 1, 'arch': 'x', 'page_size': 4096})
        second = rp.snapshot(source=b.source(), system={'cpu_count': 1, 'arch': 'x', 'page_size': 4096})
        result = rp.compare_static(first, second)
        self.assertFalse(result['consistent'])
        self.assertIn('SwapTotal', result['changed'])

    def test_static_memtotal_change(self):
        self.a.standard()
        b = self.second().standard(mem_total=1000000)
        result = rp.compare_static(self._snap(self.a), self._snap(b))
        self.assertFalse(result['consistent'])
        self.assertIn('MemTotal', result['changed'])

    def test_static_cgroup_path_change(self):
        self.a.standard()
        b = self.second().standard(self_path='/system.slice/other.service')
        first = rp.snapshot(source=self.a.source(), system={'cpu_count': 1, 'arch': 'x', 'page_size': 4096})
        second = rp.snapshot(source=b.source(), system={'cpu_count': 1, 'arch': 'x', 'page_size': 4096})
        result = rp.compare_static(first, second)
        self.assertFalse(result['consistent'])
        self.assertIn('self_path', result['changed'])

    def test_static_limit_change(self):
        self.a.standard(leaf_max=200000000)
        b = self.second().standard(leaf_max=180000000)
        first = rp.snapshot(source=self.a.source(), system={'cpu_count': 1, 'arch': 'x', 'page_size': 4096})
        second = rp.snapshot(source=b.source(), system={'cpu_count': 1, 'arch': 'x', 'page_size': 4096})
        result = rp.compare_static(first, second)
        self.assertFalse(result['consistent'])
        self.assertIn('limits', result['changed'])

    def _snap(self, fixture, **kw):
        return rp.snapshot(source=fixture.source(),
                           system={'cpu_count': 1, 'arch': 'x', 'page_size': 4096}, **kw)

    def test_static_ancestry_depth_change(self):
        self.a.standard()
        b = self.second()
        b.put('proc/meminfo', DEFAULT_MEMINFO)
        b.put('proc/self/cgroup', '0::/system.slice/docker.slice/node-panel.service\n')
        b.put('proc/self/mountinfo', '29 23 0:26 / /sys/fs/cgroup rw - cgroup2 cgroup2 rw\n')
        b.put('proc/pressure/memory', DEFAULT_PSI)
        b.level('/sys/fs/cgroup/system.slice/docker.slice/node-panel.service', 1000000, 200000000, peak=1200000)
        b.level('/sys/fs/cgroup/system.slice/docker.slice', 2000000, 'max')
        b.level('/sys/fs/cgroup/system.slice', 5000000, 'max')
        b.level('/sys/fs/cgroup', 9000000, 'max')
        first, second = self._snap(self.a), self._snap(b)
        result = rp.compare_static(first, second)
        self.assertFalse(result['consistent'])
        self.assertIn('self_path', result['changed'])
        self.assertIn('limits', result['changed'])
        self.assertTrue(rp.compare_dynamic(first, second)['ancestry_changed'])

    def test_dynamic_available_drop(self):
        self.a.standard(mem_available=1500000)
        b = self.second().standard(mem_available=1000000)
        result = rp.compare_dynamic(self._snap(self.a), self._snap(b))
        self.assertEqual(result['available_delta'], (1000000 - 1500000) * 1024)

    def test_dynamic_current_growth(self):
        self.a.standard()
        b = self.second().standard()
        (b.root / 'sys/fs/cgroup/system.slice/node-panel.service/memory.current').write_text('3000000')
        result = rp.compare_dynamic(self._snap(self.a), self._snap(b))
        self.assertEqual(result['current_delta']['/system.slice/node-panel.service'], 2000000)

    def test_dynamic_oom_increase(self):
        self.a.standard()
        b = self.second().standard(
            events='low 0\nhigh 0\nmax 0\noom 1\noom_kill 1\n')
        result = rp.compare_dynamic(self._snap(self.a), self._snap(b))
        self.assertEqual(result['new_oom'], 1)
        self.assertEqual(result['new_oom_kill'], 1)

    def test_dynamic_high_max_increase(self):
        self.a.standard()
        b = self.second().standard(events='low 0\nhigh 2\nmax 3\noom 0\noom_kill 0\n')
        result = rp.compare_dynamic(self._snap(self.a), self._snap(b))
        self.assertEqual(result['high_increase'], 2)
        self.assertEqual(result['max_increase'], 3)

    def test_dynamic_counter_reset_detected(self):
        self.a.standard(events='low 0\nhigh 0\nmax 0\noom 2\noom_kill 0\n')
        b = self.second().standard(events='low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n')
        result = rp.compare_dynamic(self._snap(self.a), self._snap(b))
        self.assertIn('/system.slice/node-panel.service', result['counter_reset'])
        self.assertLess(result['new_oom'], 0)

    def test_dynamic_psi_change(self):
        self.a.standard()
        b = self.second().standard(
            psi='some avg10=5.00 avg60=0.00 avg300=0.00 total=7\n'
                'full avg10=1.00 avg60=0.00 avg300=0.00 total=3\n')
        result = rp.compare_dynamic(self._snap(self.a), self._snap(b))
        self.assertTrue(result['psi']['comparable'])
        self.assertTrue(result['psi']['changed'])

    def test_dynamic_without_psi(self):
        self.a.standard(psi=None, mem_available=1500000)
        b = self.second().standard(psi=None, mem_available=1000000)
        result = rp.compare_dynamic(self._snap(self.a), self._snap(b))
        self.assertFalse(result['psi']['comparable'])
        self.assertEqual(result['psi']['status'], 'unsupported')
        self.assertEqual(result['available_delta'], (1000000 - 1500000) * 1024)

    # -------------------------------------------------------------- safety --

    def test_import_has_no_filesystem_side_effects(self):
        code = (
            "import builtins, re, json, hashlib, os, platform, datetime, pathlib\n"
            "def guard(*args, **kwargs):\n"
            "    raise RuntimeError('unexpected open during import: %r' % (args[:1],))\n"
            "builtins.open = guard\n"
            "import resource_probe\n"
            "print('PASS')\n"
        )
        proc = subprocess.run([sys.executable, '-c', code], cwd=REPO,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('PASS', proc.stdout)

    def test_no_subprocess_dependency(self):
        self.assertFalse(hasattr(rp, 'subprocess'))
        self.assertFalse(hasattr(rp, 'system'))

    def test_source_never_opens_for_write(self):
        self.a.put('proc/meminfo', DEFAULT_MEMINFO)
        source = self.a.source()
        modes = []
        real_open = open

        def recording_open(path, mode='r', *args, **kwargs):
            modes.append(mode)
            return real_open(path, mode, *args, **kwargs)

        with mock.patch('builtins.open', side_effect=recording_open):
            source.read_text('/proc/meminfo', rp.MEMINFO_LIMIT)
        self.assertTrue(modes)
        for mode in modes:
            self.assertEqual(mode, 'rb')

    def test_safety_check_survives_optimized_mode(self):
        code = (
            "import resource_probe as rp, tempfile, pathlib\n"
            "d = tempfile.mkdtemp()\n"
            "p = pathlib.Path(d)\n"
            "(p / 'proc').mkdir()\n"
            "(p / 'proc/meminfo').write_text('MemTotal: 1 kB\\n')\n"
            "try:\n"
            "    rp.read_meminfo(rp.Source(p))\n"
            "except rp.MemInfoError:\n"
            "    print('PASS')\n"
            "else:\n"
            "    raise SystemExit('FAIL: incomplete meminfo accepted under -O')\n"
        )
        proc = subprocess.run([sys.executable, '-O', '-c', code], cwd=REPO,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn('PASS', proc.stdout)

    def test_source_reads_only_under_root(self):
        self.a.put('proc/meminfo', DEFAULT_MEMINFO)
        source = rp.Source(self.a.root)
        self.assertIn('MemTotal', source.read_text('/proc/meminfo', rp.MEMINFO_LIMIT))
        with self.assertRaises(rp.MissingFile):
            source.read_text('/proc/does-not-exist', rp.MEMINFO_LIMIT)


if __name__ == '__main__':
    unittest.main()
