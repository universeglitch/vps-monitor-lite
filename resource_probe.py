"""Read-only Linux resource probing for the V1 resource-profile benchmark.

This module only reads and parses kernel interfaces. It never writes, never
runs commands, never imports the service modules (auth/main/panel/active) and
performs no reads at import time: importing it is side-effect free and callers
must invoke snapshot() explicitly.

Filesystem access is routed through Source so tests can mount a temporary
/proc and cgroup tree without touching the real host.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
from datetime import datetime, timezone
from pathlib import Path

# Module identity. Distinct from the profile schema_version so a profile can
# record exactly which probe produced its environment block.
PROBE_VERSION = 'resource-probe-v1'

MEMINFO_LIMIT = 1 << 20
MOUNTINFO_LIMIT = 1 << 20
CGROUP_FILE_LIMIT = 64 << 10
PSI_LIMIT = 8 << 10

MEMINFO_KEYS = ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree')
CGROUP_REQUIRED_FILES = ('memory.current', 'memory.max', 'memory.events')
EVENT_KEYS_REQUIRED = ('high', 'max', 'oom', 'oom_kill')
EVENT_KEYS_OPTIONAL = ('low',)
PSI_KEYS = ('avg10', 'avg60', 'avg300', 'total')


class ProbeError(Exception):
    """Base class for every probe failure."""


class ReadError(ProbeError):
    """A read failed for a reason other than absence or permission."""


class MissingFile(ReadError):
    """The interface file does not exist."""


class PermissionDenied(ReadError):
    """The interface file exists but cannot be read."""


class TooLarge(ReadError):
    """The interface file exceeds the accepted read limit."""


class FormatError(ProbeError):
    """Content exists but cannot be parsed or has an illegal shape."""


class MemInfoError(ProbeError):
    """Base memory information cannot supply a trustworthy answer."""


class CgroupUnsupported(ProbeError):
    """No cgroup v2 memory controller is visible in this namespace."""


class CgroupError(ProbeError):
    """cgroup v2 exists but its boundary cannot be proven completely."""


class InconsistentData(ProbeError):
    """Two readings contradict each other (counter reset, limit moved)."""


class Source:
    """Read-only view of a filesystem root, injectable for tests.

    Every path is interpreted relative to ``root`` so a test can pass a
    temporary directory that contains ``proc/`` and ``sys/`` trees.
    """

    def __init__(self, root='/'):
        self.root = Path(root)

    def _resolve(self, path):
        return self.root / str(path).lstrip('/')

    def read_bytes(self, path, limit):
        target = self._resolve(path)
        try:
            with open(target, 'rb') as handle:
                raw = handle.read(limit + 1)
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            raise MissingFile(str(path)) from None
        except PermissionError:
            raise PermissionDenied(str(path)) from None
        except OSError as exc:
            raise ReadError(str(path) + ': ' + type(exc).__name__) from None
        if len(raw) > limit:
            raise TooLarge(str(path))
        return raw

    def read_text(self, path, limit):
        raw = self.read_bytes(path, limit)
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            raise FormatError(str(path) + ': not valid utf-8') from None

    def is_dir(self, path):
        return self._resolve(path).is_dir()

    def is_symlink(self, path):
        return self._resolve(path).is_symlink()

    def exists(self, path):
        return self._resolve(path).exists()

    def realpath(self, path):
        """Return the fully resolved on-disk path (follows symlinks)."""
        return os.path.realpath(str(self._resolve(path)))


def _nonneg_int(text, name):
    if not re.fullmatch(r'\d+', text):
        raise FormatError(name + ': expected a non-negative integer')
    return int(text)


# ---------------------------------------------------------------- meminfo ----

def read_meminfo(source):
    """Return {'MemTotal','MemAvailable','SwapTotal','SwapFree'} in bytes."""
    text = source.read_text('/proc/meminfo', MEMINFO_LIMIT)
    fields = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if ':' not in line:
            raise FormatError('meminfo line %d: missing colon' % number)
        key, rest = line.split(':', 1)
        key = key.strip()
        tokens = rest.split()
        if not tokens:
            raise FormatError('meminfo line %d: missing value' % number)
        if key in fields:
            raise FormatError('meminfo duplicate field: ' + key)
        fields[key] = (tokens[0], tokens[1] if len(tokens) > 1 else '')
    out = {}
    for key in MEMINFO_KEYS:
        if key not in fields:
            raise MemInfoError('meminfo missing ' + key)
        value, unit = fields[key]
        if not re.fullmatch(r'\d+', value):
            raise FormatError('meminfo %s: invalid integer' % key)
        if unit != 'kB':
            raise FormatError('meminfo %s: unexpected unit %r' % (key, unit))
        out[key] = int(value) * 1024  # integer math only; never float bytes
    return out


# ----------------------------------------------------------------- cgroup ----

_ESCAPE = re.compile(r'\\([0-7]{3})')


def _unescape(field):
    return _ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), field)


def read_self_cgroup(source):
    """Return the cgroup v2 path of this process from /proc/self/cgroup."""
    text = source.read_text('/proc/self/cgroup', CGROUP_FILE_LIMIT)
    found = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith('0::'):
            if found is not None:
                raise CgroupError('duplicate cgroup v2 entry')
            found = line[3:]
    if found is None:
        raise CgroupUnsupported('no cgroup v2 entry in /proc/self/cgroup')
    if not found:
        found = '/'
    if not found.startswith('/'):
        raise CgroupError('cgroup path is not absolute')
    if '..' in Path(found).parts:
        raise CgroupError('cgroup path escapes the hierarchy root')
    return found


def read_mountinfo(source):
    """Return the cgroup2 mount root and mount point from /proc/self/mountinfo."""
    text = source.read_text('/proc/self/mountinfo', MOUNTINFO_LIMIT)
    matches = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split(' ')
        if '-' not in parts:
            raise FormatError('mountinfo line %d: missing separator' % number)
        sep = parts.index('-')
        if sep < 6 or sep + 2 >= len(parts):
            raise FormatError('mountinfo line %d: short entry' % number)
        if parts[sep + 1] == 'cgroup2':
            matches.append((_unescape(parts[3]), _unescape(parts[4])))
    if not matches:
        raise CgroupUnsupported('no cgroup2 mount in /proc/self/mountinfo')
    if len(matches) > 1:
        raise CgroupError('ambiguous cgroup2 mounts; boundary not provable')
    root, point = matches[0]
    if not point.startswith('/'):
        raise CgroupError('cgroup2 mount point is not absolute')
    return {'mount_root': root.rstrip('/') or '/', 'mount_point': point.rstrip('/') or '/'}


def map_cgroup_dir(rel, mount_root, mount_point):
    """Map a namespace cgroup path to an on-disk directory under the mount."""
    mr = mount_root.rstrip('/')
    if mr and (rel == mr or rel.startswith(mr + '/')):
        trimmed = rel[len(mr):]
    elif not mr:
        trimmed = rel
    else:
        raise CgroupError('cgroup path is outside the mounted hierarchy')
    if '..' in Path(trimmed).parts:
        raise CgroupError('cgroup path escapes the mount')
    joined = mount_point.rstrip('/') + '/' + trimmed.lstrip('/')
    return joined.rstrip('/') or '/'


def _ancestry(start, mount_point):
    """Return [start..mount_point] (deepest first); fail if it cannot be proven."""
    stop = mount_point.rstrip('/') or '/'
    prefix = stop if stop.endswith('/') else stop + '/'
    levels = []
    current = start.rstrip('/') or '/'
    while True:
        if current != stop and not current.startswith(prefix):
            raise CgroupError('cgroup ancestry leaves the mount point')
        levels.append(current)
        if current == stop:
            return levels
        parent = str(Path(current).parent)
        if parent == current:
            raise CgroupError('cgroup ancestry reached filesystem root before mount point')
        if len(levels) > 4096:
            raise CgroupError('cgroup ancestry is unreasonably deep')
        current = parent


def _namespace_path(dir_path, mount_point, mount_root):
    stop = mount_point.rstrip('/') or '/'
    suffix = dir_path[len(stop):] if stop != '/' else dir_path
    if stop == '/':
        suffix = dir_path
    return (mount_root.rstrip('/') + suffix) or '/'


def _assert_within_mount(source, mount_point, target):
    """Refuse a cgroup directory that could leave the mount point.

    Lexical prefix checks are not sufficient: any real symlink component below
    the mount point, or a directory whose resolved location sits outside the
    mount, must abort. Both are enforced here.
    """
    stop = mount_point.rstrip('/') or '/'
    prefix = stop if stop.endswith('/') else stop + '/'
    node = target.rstrip('/') or '/'
    if node != stop and not node.startswith(prefix):
        raise CgroupError('cgroup path lies outside the mount point')
    while node != stop:
        if source.is_symlink(node):
            raise CgroupError('cgroup path component is a symlink: ' + node)
        parent = str(Path(node).parent)
        if parent == node:
            raise CgroupError('cgroup path lies outside the mount point')
        node = parent
    mount_real = source.realpath(stop)
    target_real = source.realpath(target)
    if target_real != mount_real and not target_real.startswith(mount_real + os.sep):
        raise CgroupError('cgroup directory escapes the mount point')


def _parse_events(text):
    counts = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2 or not re.fullmatch(r'\d+', parts[1]):
            raise FormatError('memory.events: malformed line')
        if parts[0] in counts:
            raise FormatError('memory.events: duplicate key ' + parts[0])
        counts[parts[0]] = int(parts[1])
    for key in EVENT_KEYS_REQUIRED:
        if key not in counts:
            raise FormatError('memory.events missing ' + key)
    events = dict((key, counts.get(key, 0)) for key in EVENT_KEYS_REQUIRED + EVENT_KEYS_OPTIONAL)
    return events, ('low' in counts)


def _read_peak(source, base):
    try:
        raw = source.read_text(base + '/memory.peak', CGROUP_FILE_LIMIT)
    except MissingFile:
        return None, 'absent'
    except PermissionDenied:
        return None, 'error'
    except (ReadError, FormatError):
        return None, 'error'
    try:
        return _nonneg_int(raw.strip(), 'memory.peak'), 'ok'
    except FormatError:
        return None, 'error'


def read_cgroup_level(source, dir_path, namespace):
    base = dir_path.rstrip('/')

    def read(name):
        return source.read_text(base + '/' + name, CGROUP_FILE_LIMIT)

    current = _nonneg_int(read('memory.current').strip(), 'memory.current')
    raw_max = read('memory.max').strip()
    if raw_max == 'max':
        maximum = None
    else:
        maximum = _nonneg_int(raw_max, 'memory.max')
    events, low_present = _parse_events(read('memory.events'))
    peak, peak_status = _read_peak(source, base)
    return {'path': namespace, 'dir': dir_path, 'current': current,
            'max': maximum, 'max_unlimited': maximum is None,
            'peak': peak, 'peak_status': peak_status,
            'events': events, 'events_low_present': low_present}


def read_cgroup(source):
    """Return the full visible cgroup v2 boundary chain, deepest level first."""
    rel = read_self_cgroup(source)
    mount = read_mountinfo(source)
    mount_root, mount_point = mount['mount_root'], mount['mount_point']
    start = map_cgroup_dir(rel, mount_root, mount_point)
    if not source.is_dir(start):
        raise CgroupError('mapped cgroup directory does not exist')
    levels = []
    for dir_path in _ancestry(start, mount_point):
        _assert_within_mount(source, mount_point, dir_path)
        namespace = _namespace_path(dir_path, mount_point, mount_root)
        levels.append(read_cgroup_level(source, dir_path, namespace))
    return {'supported': True, 'version': 2,
            'mount_root': mount_root, 'mount_point': mount_point,
            'self_path': rel, 'levels': levels}


# -------------------------------------------------------------------- psi ----

def _parse_psi(text):
    result = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        kind = parts[0]
        if kind not in ('some', 'full'):
            raise FormatError('pressure: unknown section ' + kind)
        if kind in result:
            raise FormatError('pressure: duplicate section ' + kind)
        values = {}
        for token in parts[1:]:
            if '=' not in token:
                raise FormatError('pressure: malformed token')
            key, value = token.split('=', 1)
            if key not in PSI_KEYS:
                raise FormatError('pressure: unknown field ' + key)
            if key in values:
                raise FormatError('pressure: duplicate field ' + key)
            if key == 'total':
                if not re.fullmatch(r'\d+', value):
                    raise FormatError('pressure: malformed total')
                values[key] = int(value)
            else:
                if not re.fullmatch(r'\d+\.\d+', value):
                    raise FormatError('pressure: malformed average')
                values[key] = float(value)
        for key in PSI_KEYS:
            if key not in values:
                raise FormatError('pressure: missing ' + key)
        result[kind] = values
    for kind in ('some', 'full'):
        if kind not in result:
            raise FormatError('pressure: missing ' + kind)
    return result


def read_psi(source):
    """Return a structured PSI result; unsupported is explicit, never assumed pressure-free."""
    try:
        text = source.read_text('/proc/pressure/memory', PSI_LIMIT)
    except MissingFile:
        if source.is_dir('/proc/pressure'):
            return {'supported': False, 'status': 'missing', 'reason': 'psi file absent'}
        return {'supported': False, 'status': 'unsupported', 'reason': 'kernel exposes no psi'}
    except PermissionDenied:
        return {'supported': False, 'status': 'permission', 'reason': 'psi read denied'}
    except FormatError:
        return {'supported': False, 'status': 'malformed', 'reason': 'psi not valid utf-8'}
    except (TooLarge, ReadError):
        return {'supported': False, 'status': 'error', 'reason': 'psi unreadable'}
    try:
        parsed = _parse_psi(text)
    except FormatError as exc:
        return {'supported': False, 'status': 'malformed', 'reason': str(exc)}
    return {'supported': True, 'status': 'ok', 'some': parsed['some'], 'full': parsed['full']}


# ----------------------------------------------------------------- system ----

def read_system():
    """Return static host/runtime facts. Missing values are left as None."""
    try:
        page_size = os.sysconf('SC_PAGE_SIZE')
    except (ValueError, OSError, AttributeError):
        page_size = None
    return {'cpu_count': os.cpu_count(), 'arch': platform.machine(),
            'python_version': platform.python_version(), 'page_size': page_size}


def _utc_now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------- snapshot ----

def _issue(scope, status, detail):
    return {'scope': scope, 'status': status, 'detail': str(detail)}


def snapshot(source=None, system=None, clock=None):
    """Collect one read-only environment snapshot.

    Returns a structured dict. ``valid`` is True only when base memory and the
    full cgroup v2 boundary chain were read and parsed. Optional data (PSI,
    memory.peak) is reported through ``issues`` without invalidating the rest.
    """
    source = source if source is not None else Source('/')
    generated_at = (clock if clock is not None else _utc_now)().isoformat()
    issues = []
    meminfo = None
    try:
        meminfo = read_meminfo(source)
    except ProbeError as exc:
        issues.append(_issue('meminfo', 'required', exc))
    cgroup = None
    try:
        cgroup = read_cgroup(source)
    except CgroupUnsupported as exc:
        cgroup = {'supported': False, 'reason': str(exc)}
        issues.append(_issue('cgroup', 'unsupported', exc))
    except ProbeError as exc:
        cgroup = {'supported': False, 'reason': str(exc)}
        issues.append(_issue('cgroup', 'invalid', exc))
    if cgroup and cgroup.get('supported'):
        for level in cgroup['levels']:
            if level['peak_status'] == 'error':
                issues.append(_issue('cgroup-peak', level['path'], 'memory.peak unreadable'))
    try:
        psi = read_psi(source)
    except ProbeError as exc:
        psi = {'supported': False, 'status': 'error', 'reason': str(exc)}
    if not psi.get('supported'):
        issues.append(_issue('psi', psi.get('status', 'unsupported'), psi.get('reason', '')))
    system = system if system is not None else read_system()
    valid = meminfo is not None and bool(cgroup and cgroup.get('supported'))
    data = {'probe_version': PROBE_VERSION, 'generated_at': generated_at,
            'valid': valid, 'issues': issues, 'system': system,
            'meminfo': meminfo, 'cgroup': cgroup, 'psi': psi}
    data['fingerprint'] = static_fingerprint(data) if valid else None
    return data


def _static_fields(data):
    system = data.get('system') or {}
    cg = data.get('cgroup') or {}
    mem = data.get('meminfo') or {}
    supported = bool(cg.get('supported'))
    return {
        'arch': system.get('arch'),
        'cpu_count': system.get('cpu_count'),
        'page_size': system.get('page_size'),
        'MemTotal': mem.get('MemTotal'),
        'SwapTotal': mem.get('SwapTotal'),
        'cgroup_version': cg.get('version') if supported else None,
        'mount_root': cg.get('mount_root'),
        'mount_point': cg.get('mount_point'),
        'self_path': cg.get('self_path'),
        'limits': [[level.get('path'), level.get('max')] for level in (cg.get('levels') or [])],
    }


def static_fingerprint(data):
    """SHA256 over normalised static fields; dynamic values are excluded."""
    blob = json.dumps(_static_fields(data), sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(blob.encode('ascii')).hexdigest()


def compare_static(first, second):
    """Return {'consistent': bool, 'changed': [field names]}."""
    left, right = _static_fields(first), _static_fields(second)
    changed = sorted(key for key in left if left[key] != right[key])
    changed += sorted(key for key in right if key not in left)
    return {'consistent': not changed, 'changed': changed}


def _levels_by_path(data):
    cg = data.get('cgroup') or {}
    return dict((level.get('path'), level) for level in (cg.get('levels') or []))


def compare_dynamic(first, second):
    """Compare dynamic memory signals between two snapshots (None-safe)."""
    left_mem, right_mem = first.get('meminfo') or {}, second.get('meminfo') or {}
    left_available = left_mem.get('MemAvailable')
    right_available = right_mem.get('MemAvailable')
    available_delta = None
    if left_available is not None and right_available is not None:
        available_delta = right_available - left_available
    left_levels, right_levels = _levels_by_path(first), _levels_by_path(second)
    current_delta = {}
    for path in sorted(set(left_levels) & set(right_levels)):
        current_delta[path] = right_levels[path]['current'] - left_levels[path]['current']
    limit_changed = sorted(path for path in set(left_levels) & set(right_levels)
                           if left_levels[path]['max'] != right_levels[path]['max'])
    new_oom = new_oom_kill = high_increase = max_increase = 0
    counter_reset = []
    for path in sorted(set(left_levels) & set(right_levels)):
        before, after = left_levels[path]['events'], right_levels[path]['events']
        deltas = dict((key, after.get(key, 0) - before.get(key, 0)) for key in after)
        new_oom += deltas.get('oom', 0)
        new_oom_kill += deltas.get('oom_kill', 0)
        high_increase += deltas.get('high', 0)
        max_increase += deltas.get('max', 0)
        if any(value < 0 for value in deltas.values()):
            counter_reset.append(path)
    psi = {'changed': False, 'comparable': False}
    before_psi, after_psi = first.get('psi') or {}, second.get('psi') or {}
    if before_psi.get('supported') and after_psi.get('supported'):
        psi = {'changed': before_psi['some'] != after_psi['some'] or before_psi['full'] != after_psi['full'],
               'comparable': True, 'before': before_psi['some'], 'after': after_psi['some']}
    else:
        psi = {'changed': False, 'comparable': False,
               'status': after_psi.get('status', 'unsupported')}
    return {'available_delta': available_delta,
            'current_delta': current_delta,
            'limit_changed': limit_changed,
            'ancestry_changed': sorted(set(left_levels) ^ set(right_levels)),
            'new_oom': new_oom, 'new_oom_kill': new_oom_kill,
            'high_increase': high_increase, 'max_increase': max_increase,
            'counter_reset': counter_reset, 'psi': psi}
