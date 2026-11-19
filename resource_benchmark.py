"""V1 resource-profile benchmark: measurement, statistics and profile authority.

This module measures the incremental peak memory of every operation that the
ResourceGate admits, converts those measurements into a complete profile and
commits that profile through an atomic, recoverable write.

Design baseline: Resource Profile V1 stage C specification v3.

Hard rules enforced here (never relaxable by ``--force``):

* A formal measurement MUST read the peak from the cgroup v2 ``memory.peak``
  counter inside a dedicated ``systemd-run --scope`` unit. Polling and estimated
  peaks are refused: the operation is reported BLOCKED instead.
* The increment is ``max(final_peak - pre_current, 0)`` measured in one scope,
  which can only overstate, never understate, the real growth because a
  high-water counter dominates every earlier sample.
* A formal profile is produced only when every operation is measurable and the
  measuring scope is cgroup-environment EQUIVALENT to the service cgroup.
  ``NOT_EQUIVALENT`` and ``INDETERMINATE`` both forbid a formal profile.
* The three installer operations are never measured on the running host; they
  require externally supplied measurements that pass schema, integrity and
  local environment-equivalence validation.

The module only reads kernel interfaces; it never writes cgroup files except an
optional, purely optional, ``memory.peak`` reset, and it never stops or starts a
service. Importing it has no side effects.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import math
import os
import queue
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import resource_probe as rp

# ------------------------------------------------------------------ identity --

BENCHMARK_VERSION = 'resource-benchmark-v1'
PROFILE_SCHEMA_VERSION = 1
EXTERNAL_SCHEMA = 'resource-external-measurement-v1'
EXTERNAL_SCHEMA_VERSION = 1
PROBE_VERSION_EXPECTED = 'resource-probe-v1'

# ------------------------------------------------------------- policy values --

N_MIN = 5                 # minimum valid samples per operation
IQR_FENCE = 1.5           # Tukey outlier fence multiplier
RATIO_MAX = 1.0           # rejection ceiling for observed relative dispersion
DISCOUNT_MAX = 0.5        # rejection ceiling for the available lower-tail discount
RAM_LIMIT = 200_000_000   # decimal hardware ceiling already inside Memory.sample()
MAX_PROFILE_BYTES = 1 << 20
DEFAULT_PROFILE_PATH = '/etc/node-panel/resource-profile.json'
UNCERTAIN_SUFFIX = '.uncertain'   # persisted COMMIT_UNCERTAIN marker next to the profile
DEFAULT_SCOPE_SLICE = 'system.slice'
CONTROLLER_REQUIRED = 'memory'
DEFAULT_TIMEOUT = 120

# ------------------------------------------------------------ op registry ----

AUTH_OPS = ('initialize', 'login', 'change-password', 'recovery', 'api-change',
            'export', 'restore', 'key-rotation', 'backup', 'key-cleanup')
PANEL_OPS = ('xray-test', 'qr', 'diagnostics')
INSTALL_OPS = ('install-panel', 'install-xray', 'install-cloudflared')
MODULE_METADATA_OP = 'module-metadata'

# Operations measured locally on the target host.
LOCAL_OPS = AUTH_OPS + PANEL_OPS + (MODULE_METADATA_OP,)
# Operations that may never be measured on the running host.
EXTERNAL_OPS = INSTALL_OPS
# The complete, closed set of gate operations.
ALL_OPS = AUTH_OPS + PANEL_OPS + (MODULE_METADATA_OP,) + INSTALL_OPS

LANE = dict((op, 'login' if op == 'login' else 'normal') for op in ALL_OPS)

# An internal baseline measurement: the scope idles for a short window so the
# peak jitter of an otherwise empty scope can be measured.
IDLE_OP = 'idle'
IDLE_WINDOW = 0.5

# Status vocabulary.
STATUS_OK = 'ok'
STATUS_BLOCKED = 'blocked'
STATUS_ERROR = 'error'
STATUS_COMPLETE = 'complete'
STATUS_INCOMPLETE = 'incomplete'

# cgroup equivalence vocabulary.
EQUIVALENT = 'EQUIVALENT'
NOT_EQUIVALENT = 'NOT_EQUIVALENT'
INDETERMINATE = 'INDETERMINATE'

# Commit outcome vocabulary.
COMMITTED = 'COMMITTED'
ABORTED = 'ABORTED'
COMMIT_UNCERTAIN = 'COMMIT_UNCERTAIN'


class BenchmarkError(Exception):
    """Base class for every benchmark failure."""


class Blocked(BenchmarkError):
    """A trustworthy measurement is impossible; the operation must be BLOCKED."""


class NotEquivalent(BenchmarkError):
    """The measuring scope is not cgroup-equivalent to the service cgroup."""


class Rejected(BenchmarkError):
    """A measured value crossed a safety rejection threshold."""


class UncertainWrite(BenchmarkError):
    """A rename completed but its durability could not be proven (fsync failed)."""


class OperationFailed(BenchmarkError):
    """A recorded operation ran but did not complete successfully.

    A non-zero exit status, a timeout or a launch failure must never be
    accepted as a valid measurement sample, so the fixture that produced it
    raises instead of returning a value the caller could ignore.
    """


# ------------------------------------------------------------- small helpers --

def _utcnow():
    return datetime.now(timezone.utc)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _nonneg_int_field(text, name):
    if not re.fullmatch(r'\d+', text):
        raise BenchmarkError(name + ': expected a non-negative integer')
    return int(text)


# ------------------------------------------------------------- peak reading --

def compute_increment(pre_current, final_peak):
    """Return ``max(final_peak - pre_current, 0)``.

    A cgroup v2 high-water counter dominates every earlier reading, so for a
    freshly created scope ``final_peak >= pre_current`` and the difference can
    only overstate, never understate, the memory grown during the operation.
    """
    if pre_current is None or final_peak is None:
        raise BenchmarkError('increment requires both a prior current and a peak')
    if not isinstance(pre_current, int) or not isinstance(final_peak, int):
        raise BenchmarkError('increment requires integer readings')
    if pre_current < 0 or final_peak < 0:
        raise BenchmarkError('increment requires non-negative readings')
    return max(final_peak - pre_current, 0)


def read_peak_value(source, dir_path):
    """Read ``memory.peak``; return ``(value, status)`` or ``(None, reason)``.

    ``status`` is ``ok``, ``absent``, ``permission``, ``malformed`` or ``error``.
    A missing counter is never interpreted as zero growth.
    """
    base = dir_path.rstrip('/')
    try:
        raw = source.read_text(base + '/memory.peak', rp.CGROUP_FILE_LIMIT)
    except rp.MissingFile:
        return None, 'absent'
    except rp.PermissionDenied:
        return None, 'permission'
    except (rp.ReadError, rp.FormatError):
        return None, 'error'
    text = raw.strip()
    if not re.fullmatch(r'\d+', text):
        return None, 'malformed'
    return int(text), 'ok'


def read_current_value(source, dir_path):
    base = dir_path.rstrip('/')
    raw = source.read_text(base + '/memory.current', rp.CGROUP_FILE_LIMIT)
    return _nonneg_int_field(raw.strip(), 'memory.current')


def read_self_memory(source=None):
    """Sample this process's own ``memory.current`` and ``memory.peak``."""
    src = source if source is not None else rp.Source('/')
    rel = rp.read_self_cgroup(src)
    mount = rp.read_mountinfo(src)
    start = rp.map_cgroup_dir(rel, mount['mount_root'], mount['mount_point'])
    current = read_current_value(src, start)
    peak, status = read_peak_value(src, start)
    return {'dir': start, 'current': current, 'peak': peak, 'peak_status': status}


def try_reset_peak(source, dir_path):
    """Best-effort ``memory.peak`` reset; a precision aid, never required.

    Returns ``applied``, ``unsupported`` (absent or not writable) or ``failed``.
    """
    base = dir_path.rstrip('/')
    try:
        target = source._resolve(base + '/memory.peak')
    except Exception:
        return 'failed'
    try:
        with open(target, 'w') as handle:
            handle.write('0')
        return 'applied'
    except FileNotFoundError:
        return 'unsupported'
    except PermissionError:
        return 'unsupported'
    except OSError:
        return 'failed'


# ------------------------------------------------------ cgroup limit chain ---

def _read_limit(source, dir_path, name):
    """Read a memory limit that may be ``max``; return ``(value, status)``.

    ``status`` is ``ok``, ``unlimited``, ``absent``, ``permission`` or ``error``.
    An unreadable limit is never treated as unlimited.
    """
    base = dir_path.rstrip('/')
    try:
        raw = source.read_text(base + '/' + name, rp.CGROUP_FILE_LIMIT)
    except rp.MissingFile:
        return None, 'absent'
    except rp.PermissionDenied:
        return None, 'permission'
    except (rp.ReadError, rp.FormatError):
        return None, 'error'
    text = raw.strip()
    if text == 'max':
        return None, 'unlimited'
    if not re.fullmatch(r'\d+', text):
        return None, 'error'
    return int(text), 'ok'


def _read_word_list(source, path):
    try:
        raw = source.read_text(path, rp.CGROUP_FILE_LIMIT)
    except rp.MissingFile:
        return [], 'absent'
    except rp.PermissionDenied:
        return [], 'permission'
    except (rp.ReadError, rp.FormatError):
        return [], 'error'
    return raw.split(), 'ok'


def read_controllers(source, mount_point):
    """Read controller availability and which controllers are enabled downward."""
    base = mount_point.rstrip('/')
    controllers, cstat = _read_word_list(source, base + '/cgroup.controllers')
    subtree, sstat = _read_word_list(source, base + '/cgroup.subtree_control')
    return {'controllers': controllers, 'controllers_status': cstat,
            'subtree': subtree, 'subtree_status': sstat}


def _extended_level(source, base_dir, namespace):
    """A frozen-probe level plus the benchmark-only limit interfaces.

    ``resource_probe`` is archived and must not change, so the extra interfaces
    (``memory.high``, ``memory.swap.max`` and the per-level
    ``cgroup.subtree_control``) are read here, directly, without touching it.
    """
    level = dict(rp.read_cgroup_level(source, base_dir, namespace))
    high, high_status = _read_limit(source, base_dir, 'memory.high')
    swap, swap_status = _read_limit(source, base_dir, 'memory.swap.max')
    subtree, subtree_status = _read_word_list(source, base_dir.rstrip('/') + '/cgroup.subtree_control')
    level.update({'high': high, 'high_status': high_status,
                  'swap_max': swap, 'swap_max_status': swap_status,
                  'subtree': subtree, 'subtree_status': subtree_status})
    return level


def _chain_dirs(mount_point, target_dir):
    """Return ``[target..mount_point]`` (deepest first), refusing escapes."""
    stop = mount_point.rstrip('/') or '/'
    prefix = stop if stop.endswith('/') else stop + '/'
    node = target_dir.rstrip('/') or '/'
    if node != stop and not node.startswith(prefix):
        raise rp.CgroupError('cgroup directory lies outside the mount point')
    dirs = []
    while True:
        dirs.append(node)
        if node == stop:
            return dirs
        parent = str(Path(node).parent)
        if parent == node:
            raise rp.CgroupError('cgroup ancestry reached the filesystem root')
        node = parent


def read_envelope(source, rel_path=None):
    """Return the full cgroup v2 limit envelope of a process or cgroup path.

    Extends the frozen probe chain with the interfaces the benchmark must read
    directly: ``memory.high``, ``memory.swap.max``, ``cgroup.controllers`` and
    ``cgroup.subtree_control`` at every level. ``rel_path`` selects an arbitrary
    cgroup (the service cgroup or a measuring scope); ``None`` means this
    process. The frozen probe already proves the mount boundary and rejects any
    symlinked or escaping component for the mapped directory.
    """
    mount = rp.read_mountinfo(source)
    mount_root, mount_point = mount['mount_root'], mount['mount_point']
    if rel_path is None:
        rel = rp.read_self_cgroup(source)
    else:
        rel = str(rel_path)
        if not rel.startswith('/'):
            rel = '/' + rel
    if '..' in Path(rel).parts:
        raise rp.CgroupError('cgroup path escapes the hierarchy root')
    start = rp.map_cgroup_dir(rel, mount_root, mount_point)
    if not source.is_dir(start):
        raise rp.CgroupError('mapped cgroup directory does not exist')
    levels = []
    for dir_path in _chain_dirs(mount_point, start):
        rp._assert_within_mount(source, mount_point, dir_path)
        namespace = rp._namespace_path(dir_path, mount_point, mount_root)
        levels.append(_extended_level(source, dir_path, namespace))
    return {'valid': True, 'version': 2, 'mount_root': mount_root,
            'mount_point': mount_point, 'self_path': rel,
            'controllers': read_controllers(source, mount_point),
            'levels': levels}


def _level_statuses_known(level):
    """True when a level's limit interfaces are readable (or truly unlimited).

    An unreadable ``memory.high``/``memory.swap.max`` can never be interpreted
    as "no limit". Subtree-controller readability is judged separately by
    :func:`_controller_chain`, which knows whether a level is a genuine leaf.
    """
    return (level['high_status'] in ('ok', 'unlimited') and
            level['swap_max_status'] in ('ok', 'unlimited'))


def _controller_chain(env):
    """Return ``(indeterminate, reasons)`` for controller availability/enabling.

    The memory controller must be *available* to the namespace and *enabled*
    (``cgroup.subtree_control``) by every ancestor that has children. Only a
    genuine leaf — and the namespace mount root, whose subtree control is
    governed outside the namespace — may expose no ``cgroup.subtree_control``.
    A middle ancestor that must enable the controller downward but exposes no
    (or an unreadable) interface can never be proven enabled and is therefore
    INDETERMINATE, regardless of whether the numeric limits happen to match.
    """
    ctl = env.get('controllers') or {}
    if ctl.get('controllers_status') != 'ok':
        return True, []
    reasons = []
    if CONTROLLER_REQUIRED not in ctl.get('controllers', []):
        reasons.append('memory controller is not available to the namespace')
    levels = env.get('levels') or []
    last = len(levels) - 1
    for index, level in enumerate(levels):
        is_leaf = index == 0
        is_mount_root = index == last and last != 0
        if is_mount_root:
            continue
        status = level.get('subtree_status')
        if status in ('permission', 'error'):
            return True, []
        if status == 'ok':
            if not is_leaf and CONTROLLER_REQUIRED not in level.get('subtree', []):
                reasons.append('level %s does not enable the memory controller' % level.get('path'))
        elif status == 'absent':
            if not is_leaf:
                # A middle ancestor has children; without its subtree control we
                # cannot prove the memory controller is enabled downward.
                return True, []
        else:
            return True, []
    return False, reasons


def _limit_tuple(level):
    return (level['max'], level['high'], level['swap_max'])


def compare_environments(service, scope):
    """Compare two limit envelopes; return ``{'state', 'reasons'}``.

    ``INDETERMINATE`` dominates: an unreadable limit or enablement interface can
    never be treated as "no limit", so it forbids equivalence. Only an
    ``EQUIVALENT`` result may produce a formal measurement. Levels are aligned
    by position from the mount root, so a renamed scope leaf is fine while a
    differing limit value, a differing depth or a disabled controller is not.
    """
    if not service or not scope or not service.get('valid') or not scope.get('valid'):
        return {'state': INDETERMINATE, 'reasons': ['environment not valid']}
    reasons = []
    for env, label in ((service, 'service'), (scope, 'scope')):
        indeterminate, env_reasons = _controller_chain(env)
        if indeterminate:
            return {'state': INDETERMINATE,
                    'reasons': [label + ' controller chain is unreadable']}
        reasons.extend(label + ': ' + reason for reason in env_reasons)
    for env, label in ((service, 'service'), (scope, 'scope')):
        for index, level in enumerate(env.get('levels', [])):
            if not _level_statuses_known(level):
                return {'state': INDETERMINATE,
                        'reasons': [label + ' level %d has an unreadable limit interface' % index]}
    if service.get('mount_root') != scope.get('mount_root') or service.get('mount_point') != scope.get('mount_point'):
        reasons.append('cgroup mount differs')
    if len(service.get('levels', [])) != len(scope.get('levels', [])):
        reasons.append('ancestry depth differs')
    left = list(reversed(service.get('levels', [])))
    right = list(reversed(scope.get('levels', [])))
    for index, (a, b) in enumerate(zip(left, right)):
        if _limit_tuple(a) != _limit_tuple(b):
            reasons.append('level %d limits differ %r != %r' % (index, _limit_tuple(a), _limit_tuple(b)))
    if reasons:
        return {'state': NOT_EQUIVALENT, 'reasons': reasons}
    return {'state': EQUIVALENT, 'reasons': []}


def overall_equivalence(service_envelope, scope_envelopes):
    """Worst-case equivalence across every captured measuring scope."""
    if service_envelope is None:
        return {'state': INDETERMINATE, 'reasons': ['service cgroup envelope is unavailable']}
    if not scope_envelopes:
        return {'state': INDETERMINATE, 'reasons': ['no measuring scope envelope was captured']}
    state = EQUIVALENT
    reasons = []
    for envelope in scope_envelopes:
        verdict = compare_environments(service_envelope, envelope)
        reasons.extend(verdict['reasons'])
        if verdict['state'] == NOT_EQUIVALENT:
            return {'state': NOT_EQUIVALENT, 'reasons': reasons}
        if verdict['state'] == INDETERMINATE:
            state = INDETERMINATE
    return {'state': state, 'reasons': reasons}


def environment_fingerprint(env):
    """SHA256 over the static, dynamic-value-free part of an environment block."""
    cg = env.get('cgroup') or {}
    system = env.get('system') or {}
    mem = env.get('meminfo') or {}
    levels = cg.get('levels') or []
    static = {
        'arch': system.get('arch'), 'cpu_count': system.get('cpu_count'),
        'page_size': system.get('page_size'),
        'MemTotal': mem.get('MemTotal'), 'SwapTotal': mem.get('SwapTotal'),
        'mount_root': cg.get('mount_root'), 'mount_point': cg.get('mount_point'),
        'controllers': cg.get('controllers'),
        'levels': [[l.get('path'), l.get('max'), l.get('high'), l.get('swap_max')] for l in levels],
    }
    return _sha256(_canonical(static).encode('ascii'))


def capture_environment(source=None, system=None, clock=None):
    """Collect one cgroup/memory environment block for measurement or export."""
    src = source if source is not None else rp.Source('/')
    env = {'benchmark_version': BENCHMARK_VERSION, 'probe_version': rp.PROBE_VERSION,
           'generated_at': (clock if clock is not None else _utcnow)().isoformat(),
           'valid': False, 'issues': [], 'system': system if system is not None else rp.read_system(),
           'meminfo': None, 'cgroup': None}
    try:
        env['meminfo'] = rp.read_meminfo(src)
    except rp.ProbeError as exc:
        env['issues'].append({'scope': 'meminfo', 'status': 'required', 'detail': str(exc)})
    try:
        envelope = read_envelope(src)
        env['cgroup'] = envelope
        env['valid'] = env['meminfo'] is not None
    except rp.CgroupUnsupported as exc:
        env['issues'].append({'scope': 'cgroup', 'status': 'unsupported', 'detail': str(exc)})
        env['cgroup'] = {'valid': False, 'reason': str(exc)}
    except rp.ProbeError as exc:
        env['issues'].append({'scope': 'cgroup', 'status': 'invalid', 'detail': str(exc)})
        env['cgroup'] = {'valid': False, 'reason': str(exc)}
    env['fingerprint'] = environment_fingerprint(env) if env['valid'] else None
    return env


# ------------------------------------------------------------- statistics ----

def _quantile(ordered, fraction):
    """Linear-interpolated quantile of an already-sorted sequence."""
    n = len(ordered)
    if n == 1:
        return float(ordered[0])
    position = (n - 1) * fraction
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return float(ordered[low])
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def tukey_filter(values, fence=IQR_FENCE):
    """Split values with a Tukey IQR fence; return ``(kept, rejected)``."""
    ordered = sorted(values)
    n = len(ordered)
    if n < 4:
        return list(ordered), []
    q1 = _quantile(ordered, 0.25)
    q3 = _quantile(ordered, 0.75)
    iqr = q3 - q1
    low = q1 - fence * iqr
    high = q3 + fence * iqr
    kept = [v for v in ordered if low <= v <= high]
    rejected = [v for v in ordered if v < low or v > high]
    return kept, rejected


def sample_summary(values):
    return {'n': len(values), 'min': min(values) if values else None,
            'max': max(values) if values else None, 'median': _median(values) if values else None}


def comp_ratio(values):
    """Relative dispersion ``(max - min) / median`` for one operation."""
    if not values:
        return 0.0
    low, high = min(values), max(values)
    base = _median(values)
    if base <= 0:
        return 0.0 if high == low else float('inf')
    return (high - low) / base


def drawdown(series):
    """Largest non-negative step decline of an ``available`` series."""
    worst = 0
    for before, after in zip(series, series[1:]):
        worst = max(worst, max(0, before - after))
    return worst


def safety_discount(series):
    """Relative gap between the median and the minimum of an ``available`` series."""
    if not series:
        return 0.0
    base = _median(series)
    if base <= 0:
        return 0.0
    return (base - min(series)) / base


def compute_constants(per_op_samples, idle_peak_samples, available_series):
    """Derive the four compensation constants from measured samples."""
    ratios = dict((op, comp_ratio(values)) for op, values in per_op_samples.items())
    comp_ratio_value = max(ratios.values()) if ratios else 0.0
    comp_margin = (max(idle_peak_samples) - min(idle_peak_samples)) if idle_peak_samples else 0
    decline = drawdown(available_series)
    return {'comp_ratio': comp_ratio_value,
            'comp_margin': comp_margin,
            'comp_reserve_margin': decline + comp_margin,
            'safety_discount': safety_discount(available_series),
            'drawdown': decline,
            'per_op_ratio': ratios}


def op_budget(values, constants):
    """Inflate one operation's filtered peak into a gate budget."""
    kept, _ = tukey_filter(values)
    peak = max(kept) if kept else (max(values) if values else 0)
    return max(math.ceil(peak * (1.0 + constants['comp_ratio'])) + int(constants['comp_margin']), 1)


def available_floor(available_series, discount):
    """Conservative usable memory: the session minimum scaled by the discount."""
    if not available_series:
        raise BenchmarkError('available_floor requires at least one sample')
    return int(min(available_series) * (1.0 - discount))


def feasibility(profiles, reserve, floor):
    """Worst-case concurrent feasibility: reserve + one normal op + one login."""
    login = profiles.get('login')
    others = [profiles[op] for op in ALL_OPS if op != 'login']
    if login is None or not others:
        raise BenchmarkError('a complete profile is required for feasibility')
    required = reserve + max(others) + login
    return {'feasible': required <= floor, 'required': required, 'available_floor': floor}


def rejection_reasons(constants):
    """Safety rejection thresholds; these are gates, not compensation values."""
    reasons = []
    if constants.get('comp_ratio', 0.0) > RATIO_MAX:
        reasons.append('observed relative dispersion exceeds RATIO_MAX')
    if constants.get('safety_discount', 0.0) > DISCOUNT_MAX:
        reasons.append('available lower-tail discount exceeds DISCOUNT_MAX')
    return reasons


# ------------------------------------------------------- measurement scope ----

def _cleanup_fixture(fixture, prepared):
    """Release a fixture after measurement; return an issue string or ``None``.

    Cleanup must never be silent: a failure to remove the temporary auth vault
    is reported so it can be surfaced in the run report. Only the fixture's own
    freshly created state is ever removed, never pre-existing user data.
    """
    if fixture is None or not hasattr(fixture, 'cleanup'):
        return None
    try:
        fixture.cleanup(prepared)
    except Exception as exc:  # noqa: BLE001 - report, never hide
        return 'fixture cleanup failed: ' + _describe_exception(exc)
    return None


def _describe_exception(exc):
    """Render an exception with its notes, so a chained failure is not lost.

    A preparation error can carry an attached cleanup failure as a note; folding
    the notes into the message keeps both visible in the run report rather than
    only in a traceback.
    """
    text = type(exc).__name__ + ': ' + str(exc)
    for note in getattr(exc, '__notes__', ()) or ():
        text += ' [' + str(note) + ']'
    return text


def run_scope_operation(op, source=None, capabilities=None):
    """Execute one operation inside the current (scope) cgroup and measure it.

    This runs in the control process that systemd already placed inside the
    scope unit. The measurement window is deliberately narrow: the fixture is
    created and fully prepared first, then the peak is (best-effort) reset, then
    ``pre_current`` is read, then the operation runs, then ``final_peak`` is
    read. Fixture preparation cost is therefore never charged to the operation,
    and the increment is ``max(final_peak - pre_current, 0)``. Cleanup happens
    only after the final peak has been captured. The scope's peak is only
    trustworthy while this process is alive, which is why it stays up until the
    parent acknowledges the RESULT line.
    """
    src = source if source is not None else rp.Source('/')

    # A trustworthy counter is a precondition, not a measurement step.
    try:
        probe = read_self_memory(src)
    except rp.ProbeError as exc:
        return {'op': op, 'status': STATUS_BLOCKED, 'reason': 'scope cgroup unreadable: ' + str(exc)}
    if probe['peak_status'] != 'ok':
        return {'op': op, 'status': STATUS_BLOCKED, 'reason': 'memory.peak ' + probe['peak_status']}
    try:
        scope_envelope = read_envelope(src)
    except rp.ProbeError as exc:
        scope_envelope = None
        envelope_issue = str(exc)
    else:
        envelope_issue = None

    # Build and fully prepare the fixture *outside* the measurement window so a
    # fresh, isolated initial state is ready before ``pre_current`` is read.
    fixture = None
    prepared = None
    if op != IDLE_OP:
        try:
            fixture = build_fixture(op, capabilities if capabilities is not None else default_capabilities())
            prepared = fixture.prepare()
        except Blocked as exc:
            cleanup_issue = _cleanup_fixture(fixture, prepared)
            return {'op': op, 'status': STATUS_BLOCKED, 'reason': str(exc),
                    'scope_envelope': scope_envelope, 'envelope_issue': envelope_issue,
                    'cleanup_issue': cleanup_issue}
        except Exception as exc:
            cleanup_issue = _cleanup_fixture(fixture, prepared)
            return {'op': op, 'status': STATUS_ERROR, 'reason': _describe_exception(exc),
                    'scope_envelope': scope_envelope, 'envelope_issue': envelope_issue,
                    'cleanup_issue': cleanup_issue}

    # Precision aid only: clear the high-water mark left by preparation.
    reset = try_reset_peak(src, probe['dir'])
    try:
        before = read_self_memory(src)
    except rp.ProbeError as exc:
        cleanup_issue = _cleanup_fixture(fixture, prepared)
        return {'op': op, 'status': STATUS_BLOCKED, 'reason': 'pre-operation cgroup unreadable: ' + str(exc),
                'reset': reset, 'scope_envelope': scope_envelope, 'envelope_issue': envelope_issue,
                'cleanup_issue': cleanup_issue}
    if before['peak_status'] != 'ok':
        cleanup_issue = _cleanup_fixture(fixture, prepared)
        return {'op': op, 'status': STATUS_BLOCKED, 'reason': 'memory.peak ' + before['peak_status'],
                'reset': reset, 'scope_envelope': scope_envelope, 'envelope_issue': envelope_issue,
                'cleanup_issue': cleanup_issue}

    # Run the target operation and judge its real outcome.
    if op == IDLE_OP:
        time.sleep(IDLE_WINDOW)
    else:
        try:
            outcome = fixture.invoke(prepared)
        except Blocked as exc:
            cleanup_issue = _cleanup_fixture(fixture, prepared)
            return {'op': op, 'status': STATUS_BLOCKED, 'reason': str(exc), 'reset': reset,
                    'scope_envelope': scope_envelope, 'envelope_issue': envelope_issue,
                    'cleanup_issue': cleanup_issue}
        except Exception as exc:  # the operation ran and failed: not a valid sample
            cleanup_issue = _cleanup_fixture(fixture, prepared)
            return {'op': op, 'status': STATUS_ERROR, 'reason': _describe_exception(exc),
                    'scope_envelope': scope_envelope, 'envelope_issue': envelope_issue,
                    'cleanup_issue': cleanup_issue}
        if outcome not in (None, 0):  # a fixture that reports a non-zero status
            cleanup_issue = _cleanup_fixture(fixture, prepared)
            return {'op': op, 'status': STATUS_ERROR,
                    'reason': op + ' reported a non-zero outcome: ' + repr(outcome),
                    'scope_envelope': scope_envelope, 'envelope_issue': envelope_issue,
                    'cleanup_issue': cleanup_issue}

    # Capture the final peak *before* cleanup so teardown is never measured.
    try:
        after = read_self_memory(src)
    except rp.ProbeError as exc:
        cleanup_issue = _cleanup_fixture(fixture, prepared)
        return {'op': op, 'status': STATUS_BLOCKED, 'reason': 'post-operation cgroup unreadable: ' + str(exc),
                'scope_envelope': scope_envelope, 'envelope_issue': envelope_issue,
                'cleanup_issue': cleanup_issue}
    if after['peak_status'] != 'ok':
        cleanup_issue = _cleanup_fixture(fixture, prepared)
        return {'op': op, 'status': STATUS_BLOCKED, 'reason': 'memory.peak ' + after['peak_status'],
                'reset': reset, 'scope_envelope': scope_envelope, 'envelope_issue': envelope_issue,
                'cleanup_issue': cleanup_issue}
    result = {'op': op, 'status': STATUS_OK,
              'increment': compute_increment(before['current'], after['peak']),
              'pre_current': before['current'], 'final_peak': after['peak'], 'reset': reset,
              'scope_envelope': scope_envelope, 'envelope_issue': envelope_issue}
    result['cleanup_issue'] = _cleanup_fixture(fixture, prepared)
    return result


_RESULT_PREFIX = 'RESULT '


def encode_result(result):
    return _RESULT_PREFIX + _canonical(result) + '\n'


def decode_result(line):
    if not line.startswith(_RESULT_PREFIX):
        raise BenchmarkError('malformed scope result line')
    return json.loads(line[len(_RESULT_PREFIX):])


def scope_properties(service_envelope):
    """systemd properties that replicate the service leaf's memory limits.

    The measuring scope must reproduce the service cgroup's limit envelope so
    that the comparison can be EQUIVALENT. An unreadable limit is refused here
    rather than silently left unlimited.
    """
    levels = service_envelope.get('levels') or []
    if not levels:
        raise BenchmarkError('service envelope has no cgroup levels')
    leaf = levels[0]
    properties = []
    if leaf.get('max_unlimited'):
        properties.append('MemoryMax=infinity')
    else:
        properties.append('MemoryMax=' + str(leaf['max']))
    for prop, key, status_key in (('MemoryHigh', 'high', 'high_status'),
                                  ('MemorySwapMax', 'swap_max', 'swap_max_status')):
        status = leaf.get(status_key)
        if status == 'unlimited':
            properties.append(prop + '=infinity')
        elif status == 'ok' and leaf.get(key) is not None:
            properties.append(prop + '=' + str(leaf[key]))
        else:
            raise BenchmarkError('cannot replicate ' + prop + ' from an unreadable interface')
    return properties


def build_scope_argv(systemd_run, op, slice_name=DEFAULT_SCOPE_SLICE, unit=None, python=None,
                     properties=None, capabilities_path=None):
    """Build the ``systemd-run`` argv that runs one measured operation.

    The control process stays alive until the parent acknowledges the result, so
    the scope unit is never destroyed before its peak has been captured. The
    scope replicates the service leaf's memory limits through ``properties`` and
    receives an optional capability specification file so panel/module
    operations can be measured against a genuinely deployed capability.
    """
    unit = unit or ('vps-bench-' +
                    hashlib.sha256(('%f-%d' % (time.time(), os.getpid())).encode()).hexdigest()[:12] +
                    '.scope')
    runner = python or sys.executable
    argv = [systemd_run, '--quiet', '--wait', '--pipe', '--collect',
            '--unit=' + unit, '--slice=' + slice_name]
    for prop in (properties or []):
        argv.append('--property=' + prop)
    argv.extend(['--property=KillMode=control-group', '--property=RuntimeMaxSec=300', '--',
                 runner, os.path.abspath(__file__), '_scope-control', op])
    if capabilities_path:
        argv.extend(['--capabilities', capabilities_path])
    return argv


class RealScopeMeter:
    """Measure one operation inside a dedicated ``systemd-run --scope`` unit.

    The parent reads exactly one RESULT line, then writes an ACK that releases
    the control process. The unit therefore cannot be torn down before the peak
    was read and handed back.
    """

    def __init__(self, systemd_run=None, slice_name=DEFAULT_SCOPE_SLICE, timeout=DEFAULT_TIMEOUT,
                 popen=None, service_envelope=None, capabilities_path=None):
        self.systemd_run = systemd_run
        self.slice_name = slice_name
        self.timeout = timeout
        self.popen = popen
        self.service_envelope = service_envelope
        self.capabilities_path = capabilities_path

    def _binary(self):
        if self.systemd_run:
            return self.systemd_run
        import shutil
        found = shutil.which('systemd-run')
        if found is None:
            raise Blocked('systemd-run unavailable; no scope measurement')
        return found

    @staticmethod
    def _read_line_with_timeout(stream, timeout):
        """Read one line with a hard timeout; return ``(line, failure)``.

        ``stdout.readline()`` blocks indefinitely, so the deadline can never be
        enforced around it. A helper thread performs the read and hands the
        result back through a queue; on timeout the caller terminates the child.
        """
        outcome = queue.Queue(maxsize=1)

        def reader():
            try:
                outcome.put((stream.readline(), None))
            except Exception as exc:  # pragma: no cover - defensive
                outcome.put((None, type(exc).__name__))

        worker = threading.Thread(target=reader, daemon=True)
        worker.start()
        try:
            return outcome.get(timeout=timeout)
        except queue.Empty:
            return None, 'timeout'

    @staticmethod
    def _terminate(proc):
        """Best-effort terminate, wait for and reap a scope client process."""
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:  # noqa: BLE001 - teardown must never mask the real error
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    def measure(self, op, fixture=None):
        if op != IDLE_OP and op not in ALL_OPS:
            raise BenchmarkError('unknown operation')
        if op in EXTERNAL_OPS:
            raise Blocked('installer operations require external measurements')
        popen = self.popen or subprocess.Popen
        properties = None
        if self.service_envelope is not None and op != IDLE_OP:
            properties = scope_properties(self.service_envelope)
        argv = build_scope_argv(self._binary(), op, self.slice_name, properties=properties,
                                capabilities_path=self.capabilities_path)
        try:
            proc = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
        except OSError as exc:
            raise Blocked('scope launch failed: ' + type(exc).__name__) from None
        deadline = time.monotonic() + self.timeout
        line, failure = self._read_line_with_timeout(proc.stdout, self.timeout)
        if failure == 'timeout':
            self._terminate(proc)
            raise Blocked('scope measurement timed out')
        if failure is not None:
            self._terminate(proc)
            raise Blocked('scope produced an unreadable measurement: ' + failure)
        if not line:
            self._terminate(proc)
            raise Blocked('scope exited without a measurement')
        try:
            result = decode_result(line)
        except BenchmarkError:
            self._terminate(proc)
            raise Blocked('scope produced an invalid RESULT line') from None
        # Acknowledge only after the peak was received; the scope unit stays
        # alive until the control process consumes this line.
        try:
            proc.stdin.write('ACK\n')
            proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass
        remaining = deadline - time.monotonic()
        try:
            proc.wait(timeout=max(1.0, remaining))
        except subprocess.TimeoutExpired:
            self._terminate(proc)
            raise Blocked('scope did not exit after acknowledgement') from None
        return result


class FakeMeter:
    """Deterministic meter used only to exercise the harness contract in tests.

    When a fixture is supplied it is prepared, invoked and cleaned for real, so
    tests can prove sample-to-sample fixture isolation without a systemd scope.
    """

    def __init__(self, results):
        self.results = results
        self.calls = []

    def measure(self, op, fixture=None):
        self.calls.append(op)
        if fixture is not None:
            prepared = None
            try:
                prepared = fixture.prepare()
                fixture.invoke(prepared)
            finally:
                if prepared is not None and hasattr(fixture, 'cleanup'):
                    fixture.cleanup(prepared)
        value = self.results.get(op)
        if callable(value):
            value = value(op)
        if value is None:
            return {'op': op, 'status': STATUS_BLOCKED, 'reason': 'no fake result'}
        return dict(value)


# -------------------------------------------------------------- fixtures -----

PW = 'benchmark-password-000000000000'
PW2 = 'benchmark-password-111111111111'
INDEP_PW = 'independent-backup-password-00000'
TOKEN_OPS = ('change-password', 'api-change', 'export', 'restore', 'key-rotation', 'backup', 'key-cleanup')


class _NullGate:
    """Permissive gate used only for measurement fixtures, never in production."""

    def __call__(self, kind):
        return self._cm()

    @contextmanager
    def _cm(self):
        yield

    def checkpoint(self, extra=0):
        return None


class AuthFixture:
    """Fresh, isolated auth vault for one operation sample.

    ``prepare`` builds a brand new vault state for every sample so a state
    changing operation can never inherit the previous sample's mutations. If
    any prerequisite step (initialize/login/export) fails, the vault and its
    temporary root are torn down before the error propagates, so a failed
    preparation never leaks a directory or a half-built credential store.
    """

    def __init__(self, op):
        self.op = op

    def prepare(self):
        import auth as auth_module
        root = Path(tempfile.mkdtemp(prefix='rpb-auth-'))
        vault = auth_module.Auth(str(root), gate=_NullGate())
        prepared = {'auth': vault, 'root': root, 'token': None, 'recovery': None, 'package': None}
        try:
            if self.op != 'initialize':
                prepared['recovery'] = vault.initialize(PW, PW, local_authority=True)
            if self.op in TOKEN_OPS:
                prepared['token'] = vault.login(PW, source='127.0.0.1')
            if self.op == 'restore':
                prepared['package'] = vault.export_backup(prepared['token'], INDEP_PW)
        except BaseException as prepare_error:
            # A failed preparation must still tear down its temporary vault, but
            # the cleanup failure must never be swallowed and must never replace
            # the preparation error: the preparation error stays the raised
            # exception, and the cleanup failure is preserved on it (as a note,
            # which the traceback renders) so both are traceable.
            try:
                self.cleanup(prepared)
            except Exception as cleanup_error:  # noqa: BLE001 - reported, never hidden
                prepare_error.add_note('fixture cleanup also failed: ' +
                                       type(cleanup_error).__name__ + ': ' + str(cleanup_error))
            raise
        return prepared

    def cleanup(self, prepared=None):
        """Remove the fixture's own temporary vault; never hide a failure.

        Only the directory this fixture created with :func:`tempfile.mkdtemp`
        (recognised by its ``rpb-auth-`` prefix) is ever removed, never a path
        that merely happens to be recorded in ``prepared``. A removal failure is
        raised so the caller reports it rather than assuming the vault is gone;
        an already-absent directory satisfies the goal and is not an error. The
        original measurement error is never masked — :func:`run_scope_operation`
        collects this exception through :func:`_cleanup_fixture`, which reports
        it alongside, not instead of, the measurement outcome.
        """
        import shutil
        if not prepared or not prepared.get('root'):
            return
        root = Path(prepared['root'])
        close_error = None
        try:
            prepared['auth'].close()
        except Exception as exc:  # noqa: BLE001 - still attempt the directory removal
            close_error = exc
        remove_error = None
        if root.name.startswith('rpb-auth-'):
            try:
                shutil.rmtree(str(root))
            except FileNotFoundError:
                pass  # already gone: the goal (absence) is met
            except OSError as exc:
                remove_error = exc
        if remove_error is not None:
            raise OSError('cannot remove temporary vault ' + str(root) + ': ' +
                          type(remove_error).__name__ + ': ' + str(remove_error)) from None
        if close_error is not None:
            raise close_error

    def invoke(self, prepared):
        op = self.op
        vault = prepared['auth']
        token = prepared['token']
        if op == 'initialize':
            vault.initialize(PW, PW, local_authority=True)
        elif op == 'login':
            vault.login(PW, source='127.0.0.1')
        elif op == 'change-password':
            vault.change_password(token, PW, PW2, PW2, source='127.0.0.1')
        elif op == 'recovery':
            vault.recover(prepared['recovery'], PW2, PW2, source='127.0.0.1')
        elif op == 'api-change':
            record = vault.create_api(token, 'benchmark')
            vault.delete_api(token, record['id'], 'benchmark')
        elif op == 'export':
            vault.export_backup(token, INDEP_PW)
        elif op == 'restore':
            vault.restore_backup(token, prepared['package'], INDEP_PW)
        elif op == 'key-rotation':
            vault.rotate_keys(token)
        elif op == 'backup':
            vault.local_backup(token)
        elif op == 'key-cleanup':
            vault.collect_objects(token)
        else:
            raise Blocked('no auth fixture for ' + op)


# ------------------------------------------------- capability binding (P0-5) --
#
# The benchmark -- never the operator -- binds every capability-driven operation
# to its real business entry point. The operator's capability file may only
# *enable* an operation; it can never name an executable, an interpreter, an
# argv, a printed result or a timeout, because a subprocess that merely exits
# zero (or prints a convincing JSON blob) is no proof that a business operation
# ran. The benchmark constructs the real object graph from function.py's own
# classes and invokes the real method in-process inside the measuring scope, and
# it is the *real return value* -- not anything a process printed -- that is
# verified.

# Panel/module operations that require a genuinely deployed capability.
CAPABILITY_OPS = PANEL_OPS + (MODULE_METADATA_OP,)

# The canonical real business entry point behind every capability-driven
# operation. These names are the benchmark's own binding; they are never taken
# from operator input and are used only to document and construct the call.
CAPABILITY_ENTRYPOINTS = {
    'qr': 'function.PanelBackend.qr',
    'diagnostics': 'function.PanelBackend.detail',
    'xray-test': 'function.XraySelfTest.__call__',
    'module-metadata': 'function.PanelBackend.metadata',
}

# Deployed locations the benchmark uses to construct the real object graph.
PANEL_CONFIG_PATH = '/etc/node-panel/panel-config.json'
XRAY_CONFIG_PATH = '/usr/local/etc/xray/config.json'

# Fields a capability specification must never carry: each names something the
# benchmark would have to *execute* or print, so they are refused outright.
CAPABILITY_COMMAND_FIELDS = ('entrypoint', 'argv', 'command', 'exec', 'program',
                             'shell', 'result', 'timeout', 'interpreter')


def _capability_spec_problem(op, spec):
    """Return a reason when an operator capability spec tries to name a command.

    A valid specification is the boolean ``true`` (or an empty object), which
    only *enables* the operation. Any object that carries a command-bearing field
    is refused, so a capability file can never smuggle in an arbitrary command,
    an interpreter or a result to print.
    """
    if spec is True:
        return None
    if isinstance(spec, dict):
        named = sorted(k for k in CAPABILITY_COMMAND_FIELDS if k in spec)
        if named:
            return ('names ' + ', '.join(named) + '; the benchmark binds the ' + op +
                    ' business entry itself and refuses operator-supplied commands')
        if spec:
            return 'carries unexpected fields: ' + ', '.join(sorted(spec))
        return None
    if spec is False or spec is None:
        return 'is not enabled'
    return 'is malformed'


def _verify_module_metadata(panel_config, result):
    """Return a reason when the real ``metadata`` result is not the real config."""
    if not isinstance(result, dict):
        return 'expected a metadata mapping'
    try:
        expected = json.loads(Path(panel_config).read_bytes())
    except FileNotFoundError:
        expected = {}
    except (OSError, ValueError):
        expected = None
    if expected is not None and result != expected:
        return 'metadata does not match the configured panel metadata'
    return None


def _verify_qr(result):
    """Return a reason when ``qr`` did not return a real SVG payload."""
    import base64
    import binascii
    if not isinstance(result, str) or not result:
        return 'expected a base64 SVG payload'
    try:
        raw = base64.b64decode(result, validate=True)
    except (ValueError, binascii.Error):
        return 'payload is not valid base64'
    if b'<svg' not in raw:
        return 'payload is not an SVG image'
    return None


def _verify_diagnostics(result):
    """Return a reason when ``detail`` did not return a diagnostics mapping."""
    if not isinstance(result, dict):
        return 'expected a diagnostics mapping'
    for key in ('ip', 'country', 'note'):
        if key not in result:
            return 'diagnostics mapping is missing ' + key
    return None


# Builder that constructs the real object graph for one capability operation.
# The benchmark owns this registry: an operation's entry point is never taken
# from operator input.
def _capability_build_qr(function, vault, gate, panel_config, xray_config, prepared):
    if not Path(xray_config).is_file():
        raise Blocked('qr requires a readable Xray configuration at ' + str(xray_config))
    xray = function.XrayTransaction(vault.store, xray_config, gate, None, None, vault.validate)
    backend = _capability_backend(function, vault, gate, panel_config, xray)
    nodes = backend.nodes(prepared['token'], False)
    if not nodes:
        raise Blocked('qr requires a configured Xray node; none is available')
    prepared['ident'] = nodes[0]['id']
    return backend


def _capability_backend(function, vault, gate, panel_config, xray=None):
    import active as active_module
    coordinator = function.Coordinator(vault, active_module.Active(), gate, xray=xray)
    return function.PanelBackend(coordinator, panel_config=panel_config)


class CapabilityRunner:
    """Benchmark-owned binding from an operation to its real business call.

    The benchmark constructs the real object graph with function.py's real
    classes and calls the real business method in-process, inside the measuring
    scope, so the operation's memory is charged to that scope. The operator
    supplies no command of any kind: :meth:`prepare` refuses any specification
    that names one, and refuses any operation whose real dependencies cannot be
    satisfied. A real return value is always required; a printed blob or a zero
    exit status can never stand in for it.
    """

    def __init__(self, op, capability=True, *, panel_config=None, xray_config=None,
                 gate=None, workspace=None):
        self.op = op
        self.capability = capability
        self.panel_config = Path(panel_config) if panel_config is not None else Path(PANEL_CONFIG_PATH)
        self.xray_config = Path(xray_config) if xray_config is not None else Path(XRAY_CONFIG_PATH)
        self.gate = gate if gate is not None else _NullGate()
        self.workspace = Path(workspace) if workspace is not None else None
        self._owned_root = None

    def prepare(self):
        import function
        if self.op not in CAPABILITY_OPS:
            raise Blocked(self.op + ' has no capability-driven business entry point')
        problem = _capability_spec_problem(self.op, self.capability)
        if problem is not None:
            raise Blocked(self.op + ' capability ' + problem)
        if self.op == 'xray-test':
            raise Blocked('xray-test requires a real Xray executable and a measured task '
                          'launcher (function.BoundedCommand with isolated execution); neither '
                          'can be safely synthesised, so the operation is BLOCKED')
        root = self.workspace
        if root is None:
            root = Path(tempfile.mkdtemp(prefix='rpb-cap-'))
            self._owned_root = root
        prepared = {'root': root}
        try:
            import auth as auth_module
            vault = auth_module.Auth(str(root), gate=self.gate)
            prepared['auth'] = vault
            if self.op in ('qr', 'diagnostics'):
                vault.initialize(PW, PW, local_authority=True)
                prepared['token'] = vault.login(PW, source='127.0.0.1')
            if self.op == 'module-metadata':
                prepared['backend'] = _capability_backend(function, vault, self.gate, self.panel_config)
            elif self.op == 'qr':
                prepared['backend'] = _capability_build_qr(function, vault, self.gate, self.panel_config,
                                                           self.xray_config, prepared)
            elif self.op == 'diagnostics':
                prepared['backend'] = _capability_backend(function, vault, self.gate, self.panel_config)
                prepared['headers'] = {}
            return prepared
        except BaseException:
            self._teardown(prepared)
            raise

    def invoke(self, prepared):
        op = self.op
        backend = prepared['backend']
        if op == 'module-metadata':
            result = backend.metadata()
            problem = _verify_module_metadata(self.panel_config, result)
        elif op == 'qr':
            result = backend.qr(prepared['token'], prepared['ident'])
            problem = _verify_qr(result)
        elif op == 'diagnostics':
            result = backend.detail(prepared['token'], 'visitor', prepared['headers'])
            problem = _verify_diagnostics(result)
        else:
            raise Blocked(op + ' has no capability-driven business entry point')
        if problem is not None:
            raise OperationFailed(op + ' business operation did not produce the expected result: ' +
                                  problem)
        return 0

    def cleanup(self, prepared=None):
        """Release the fixture; report a cleanup failure instead of hiding it."""
        self._teardown(prepared)

    def _teardown(self, prepared):
        if not prepared:
            return
        error = None
        auth_obj = prepared.get('auth')
        if auth_obj is not None and hasattr(auth_obj, 'close'):
            try:
                auth_obj.close()
            except Exception as exc:  # noqa: BLE001 - still remove our own directory
                error = exc
        root = prepared.get('root')
        if root is not None and root == self._owned_root:
            import shutil
            try:
                shutil.rmtree(str(root))
            except FileNotFoundError:
                pass
            except OSError as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error


def build_fixture(op, capabilities=None):
    if op in AUTH_OPS:
        return AuthFixture(op)
    if op in CAPABILITY_OPS:
        caps = capabilities if capabilities is not None else default_capabilities()
        return CapabilityRunner(op, caps.get(op, False))
    if op in EXTERNAL_OPS:
        raise Blocked(op + ' must be measured externally')
    raise BenchmarkError('unknown operation ' + str(op))


def default_capabilities():
    """No capability is assumed.

    Panel and module operations stay BLOCKED until a deployment *enables* them;
    enabling only permits the benchmark to attempt its own real binding, which
    still BLOCKS when the operation's real dependencies are unavailable.
    """
    return {}


def load_capabilities(path):
    """Load a capability enablement file, refusing any operator-supplied command.

    ``None`` yields the fail-closed default. The file must be a JSON object that
    maps each operation to ``true`` (or an empty object). Any entry that names an
    executable, an argv, an interpreter or a result to print is refused, so the
    file can never smuggle in an arbitrary command.
    """
    if not path:
        return default_capabilities()
    raw = Path(path).read_bytes()
    if len(raw) > MAX_PROFILE_BYTES:
        raise BenchmarkError('capability specification exceeds size limit')
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeError):
        raise BenchmarkError('capability specification is not valid JSON') from None
    if not isinstance(doc, dict):
        raise BenchmarkError('capability specification must be an object')
    for op, spec in doc.items():
        problem = _capability_spec_problem(op, spec)
        if problem is not None:
            raise BenchmarkError('capability ' + str(op) + ' ' + problem)
    return doc


# ---------------------------------------------------------------- measurer ---

def measure_operation(meter, op, fixture, samples=N_MIN):
    """Collect ``samples`` measurements for one operation.

    The meter owns fixture handling: the real scope meter rebuilds a fresh,
    isolated fixture inside the scope for every sample, while the fake meter
    prepares/invokes/cleans the supplied fixture so isolation can be asserted.
    A meter that cannot obtain a trustworthy measurement reports it as a
    BLOCKED sample instead of aborting the whole run.
    """
    results = []
    for _ in range(samples):
        try:
            result = dict(meter.measure(op, fixture))
        except Blocked as exc:
            result = {'op': op, 'status': STATUS_BLOCKED, 'reason': str(exc)}
        except BenchmarkError as exc:
            result = {'op': op, 'status': STATUS_ERROR,
                      'reason': type(exc).__name__ + ': ' + str(exc)}
        result['op'] = op
        results.append(result)
    return results


def summarize_operation(results):
    """Reduce per-sample results into one operation record."""
    increments = [r['increment'] for r in results if r.get('status') == STATUS_OK]
    blocked = [r.get('reason', 'blocked') for r in results if r.get('status') == STATUS_BLOCKED]
    errors = [r.get('reason', 'error') for r in results if r.get('status') == STATUS_ERROR]
    if blocked:
        return {'status': STATUS_BLOCKED, 'samples': increments, 'reasons': blocked}
    if errors:
        return {'status': STATUS_ERROR, 'samples': increments, 'reasons': errors}
    if len(increments) < N_MIN:
        return {'status': STATUS_BLOCKED, 'samples': increments,
                'reasons': ['fewer than N_MIN valid samples']}
    kept, rejected = tukey_filter(increments)
    return {'status': STATUS_OK, 'samples': increments, 'filtered': kept,
            'rejected': rejected, 'summary': sample_summary(increments)}


# --------------------------------------------------------- external import ---

def external_digest(doc):
    """Integrity digest over the document with its ``integrity`` field removed."""
    body = dict(doc)
    body.pop('integrity', None)
    return _sha256(_canonical(body).encode('ascii'))


def validate_external_document(doc, local_envelope, expected_op=None):
    """Validate one external measurement document.

    ``verified`` requires the schema, the operator block, integrity, at least
    N_MIN samples and a *locally recomputed* EQUIVALENT cgroup environment. A
    missing local service envelope is **not** a pass: without it the local
    environment-equivalence check cannot run, so the document is refused rather
    than reported as verified. A SHA256 digest proves integrity only; it cannot
    prove the operator identity or the measuring environment is genuine.
    """
    issues = []
    if not isinstance(doc, dict):
        return {'verified': False, 'issues': ['document is not an object'], 'operation': None, 'increments': []}
    if doc.get('schema') != EXTERNAL_SCHEMA:
        issues.append('unexpected schema')
    if doc.get('schema_version') != EXTERNAL_SCHEMA_VERSION:
        issues.append('unexpected schema_version')
    op = doc.get('operation')
    if op not in EXTERNAL_OPS:
        issues.append('operation is not an external installer operation')
    if expected_op is not None and op != expected_op:
        issues.append('operation does not match the slot being filled')
    if doc.get('benchmark_version') != BENCHMARK_VERSION:
        issues.append('unexpected benchmark_version')
    if doc.get('probe_version') != PROBE_VERSION_EXPECTED:
        issues.append('unexpected probe_version')
    n = doc.get('n')
    samples = doc.get('samples')
    if type(n) is not int or n < N_MIN:
        issues.append('n below N_MIN')
    if not isinstance(samples, list) or len(samples) < N_MIN:
        issues.append('samples below N_MIN')
    if isinstance(samples, list) and any(type(s) is not int or s < 0 or s >= RAM_LIMIT for s in samples):
        issues.append('samples must be byte counts below the hardware ceiling')
    if type(n) is int and isinstance(samples, list) and n != len(samples):
        issues.append('n does not match sample count')
    operator = doc.get('operator')
    if not isinstance(operator, dict) or not all(isinstance(operator.get(k), str) and operator.get(k).strip()
                                                for k in ('name', 'contact', 'collected_at')):
        issues.append('operator block incomplete')
    environment = doc.get('environment')
    if not isinstance(environment, dict):
        issues.append('environment block missing')
    else:
        if environment.get('probe_version') != PROBE_VERSION_EXPECTED:
            issues.append('environment probe_version mismatch')
        computed = environment_fingerprint(environment)
        if doc.get('fingerprint') != computed:
            issues.append('environment fingerprint mismatch')
    # Local environment equivalence is mandatory: an external document may not
    # be accepted just because the local side is unavailable.
    if local_envelope is None:
        issues.append('local service cgroup envelope is unavailable; environment equivalence cannot be verified')
    else:
        candidate = environment.get('cgroup') if isinstance(environment, dict) else None
        equivalence = (compare_environments(local_envelope, candidate) if isinstance(candidate, dict)
                       else {'state': INDETERMINATE, 'reasons': ['no cgroup block']})
        if equivalence['state'] != EQUIVALENT:
            issues.append('external cgroup environment is not EQUIVALENT: ' + equivalence['state'])
    integrity = doc.get('integrity')
    if not isinstance(integrity, dict) or integrity.get('algorithm') != 'sha256':
        issues.append('integrity block missing')
    elif integrity.get('digest') != external_digest(doc):
        issues.append('integrity digest mismatch')
    verified = not issues
    return {'verified': verified, 'issues': issues, 'operation': op,
            'increments': list(samples) if verified else []}


def load_external_document(raw):
    if len(raw) > MAX_PROFILE_BYTES:
        raise BenchmarkError('external document exceeds size limit')
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise BenchmarkError('external document is not valid JSON') from None


# ------------------------------------------------------------- profile I/O ---

# Every profile field that must be integrity-protected. The fingerprint is
# computed over exactly these fields so tampering with operations, environment,
# generated_at, budgets, status or equivalence is always detected.
PROFILE_PROTECTED_FIELDS = (
    'schema_version', 'benchmark_version', 'probe_version', 'status',
    'generated_at', 'cgroup_equivalence', 'profiles', 'reserve',
    'available_floor', 'constants', 'operations', 'environment_fingerprint',
    'environment', 'issues',
)


def profile_fingerprint(profile):
    """SHA256 over every integrity-protected field of a profile.

    The fingerprint must cover the complete measurement evidence, not merely the
    top-level summary, so that any change to a protected field is caught at load
    time instead of silently accepted.
    """
    protected = dict((key, profile.get(key)) for key in PROFILE_PROTECTED_FIELDS)
    return _sha256(_canonical(protected).encode('ascii'))


def build_profile(operations, constants, profiles, reserve, floor, env, equivalence, *, generated_at=None, issues=None):
    """Assemble a profile document from measurement results.

    ``status`` is ``complete`` only when every one of the 17 operations is
    measured or externally verified, equivalence is EQUIVALENT, no rejection
    threshold was crossed, and the worst-case concurrency check passes. The
    feasibility gate uses the v3 formula
    ``required = reserve + max(profiles[op] for op != 'login') + profiles['login']``
    and refuses to report a formal profile complete when ``required`` exceeds
    the available floor.
    """
    all_ok = all(operations.get(op, {}).get('status') == STATUS_OK for op in ALL_OPS)
    reasons = list(rejection_reasons(constants))
    if equivalence != EQUIVALENT:
        reasons.append('cgroup environment is ' + equivalence)
    if not all_ok:
        reasons.append('not every operation has a trustworthy measurement')
    reasons.extend(issues or [])
    status = STATUS_COMPLETE if not reasons else STATUS_INCOMPLETE
    # A formal profile must additionally be feasible under worst-case
    # concurrency; otherwise it may only be retained as an incomplete candidate.
    if status == STATUS_COMPLETE:
        if set(profiles) != set(ALL_OPS):
            reasons.append('a complete profile must budget every operation')
            status = STATUS_INCOMPLETE
        else:
            records_problem = _complete_records_problem(operations)
            if records_problem is not None:
                reasons.append(records_problem)
                status = STATUS_INCOMPLETE
            else:
                verdict = feasibility(profiles, reserve, floor)
                if not verdict['feasible']:
                    reasons.append('worst-case feasibility fails: reserve + max(normal) + login = %d '
                                   'exceeds available_floor %d' % (verdict['required'], floor))
                    status = STATUS_INCOMPLETE
    profile = {
        'schema_version': PROFILE_SCHEMA_VERSION,
        'benchmark_version': BENCHMARK_VERSION,
        'probe_version': PROBE_VERSION_EXPECTED,
        'status': status,
        'generated_at': (generated_at or _utcnow()).isoformat(),
        'cgroup_equivalence': equivalence,
        'profiles': dict(profiles),
        'reserve': int(reserve),
        'available_floor': int(floor),
        'constants': dict(constants),
        'operations': dict((op, {'status': d.get('status'), 'samples': d.get('samples', [])})
                           for op, d in operations.items()),
        'environment_fingerprint': environment_fingerprint(env) if env else None,
        'environment': env,
        'issues': reasons,
    }
    profile['fingerprint'] = profile_fingerprint(profile)
    return profile


def _sample_values_problem(samples):
    """Return a reason when ``samples`` is not a list of non-negative integers.

    ``bool`` is deliberately rejected: ``type(True) is bool``, never ``int``, so
    a boolean can never masquerade as a byte-count sample.
    """
    if not isinstance(samples, list):
        return 'must be a list'
    for value in samples:
        if type(value) is not int or value < 0:
            return 'must contain only non-negative integers'
    return None


def _complete_records_problem(operations):
    """Return a reason when records cannot support a formal (complete) profile.

    A formal profile requires every operation to be measured (``ok``) with at
    least :data:`N_MIN` valid samples. This is enforced at generation time as
    well as at load/write time, so no entry point can emit a formal profile that
    carries a single sample per operation.
    """
    for op in ALL_OPS:
        record = operations.get(op)
        if not isinstance(record, dict) or record.get('status') != STATUS_OK:
            return 'operation ' + op + ' has no trustworthy measurement'
        samples = record.get('samples')
        problem = _sample_values_problem(samples)
        if problem is not None:
            return 'operation ' + op + ' samples ' + problem
        if len(samples) < N_MIN:
            return ('operation ' + op + ' has fewer than %d valid samples, so the profile '
                    'cannot be complete' % N_MIN)
    return None


def validate_profile(profile):
    """Full structural and integrity validation; raises ``BenchmarkError``.

    ``loads_profile`` only turns bytes into a dict and then defers entirely to
    this function, so there is no weaker entry point that bypasses the checks.
    The required-field set, field types, operation set, status, version and the
    fingerprint value are all verified here. Deliberately never uses ``assert``,
    so the checks stay effective under ``python -O``.
    """
    if not isinstance(profile, dict):
        raise BenchmarkError('profile must be an object')
    for key in PROFILE_PROTECTED_FIELDS + ('fingerprint',):
        if key not in profile:
            raise BenchmarkError('profile missing ' + key)
    if profile['schema_version'] != PROFILE_SCHEMA_VERSION:
        raise BenchmarkError('unsupported profile schema_version')
    if profile['benchmark_version'] != BENCHMARK_VERSION:
        raise BenchmarkError('unsupported benchmark_version')
    if profile['probe_version'] != PROBE_VERSION_EXPECTED:
        raise BenchmarkError('unsupported probe_version')
    status = profile['status']
    if status not in (STATUS_COMPLETE, STATUS_INCOMPLETE):
        raise BenchmarkError('invalid profile status')
    generated_at = profile['generated_at']
    if not isinstance(generated_at, str) or not generated_at.strip():
        raise BenchmarkError('generated_at must be a non-empty timestamp string')
    equivalence = profile['cgroup_equivalence']
    if equivalence not in (EQUIVALENT, NOT_EQUIVALENT, INDETERMINATE):
        raise BenchmarkError('invalid cgroup_equivalence')
    profiles = profile['profiles']
    if not isinstance(profiles, dict):
        raise BenchmarkError('profiles must be an object')
    if 'normal' in profiles:
        raise BenchmarkError('profiles must not define a normal lane budget')
    if not set(profiles) <= set(ALL_OPS):
        raise BenchmarkError('profiles contains an unknown operation')
    for op, value in profiles.items():
        if type(value) is not int or not 0 < value < RAM_LIMIT:
            raise BenchmarkError('invalid budget for ' + op)
    if status == STATUS_COMPLETE and set(profiles) != set(ALL_OPS):
        raise BenchmarkError('a complete profile must budget exactly the 17 operations')
    reserve = profile['reserve']
    floor = profile['available_floor']
    # An INCOMPLETE candidate may legitimately carry no reserve or floor yet
    # (no usable session), so the nonzero bounds are only required for a profile
    # that claims to be complete and therefore authoritative.
    if type(reserve) is not int or not 0 <= reserve < RAM_LIMIT:
        raise BenchmarkError('invalid reserve')
    if type(floor) is not int or not 0 <= floor <= RAM_LIMIT:
        raise BenchmarkError('invalid available_floor')
    if status == STATUS_COMPLETE and reserve <= 0:
        raise BenchmarkError('a complete profile requires a positive reserve')
    if status == STATUS_COMPLETE and floor <= 0:
        raise BenchmarkError('a complete profile requires a positive available_floor')
    constants = profile['constants']
    if not isinstance(constants, dict):
        raise BenchmarkError('constants must be an object')
    for key in ('comp_ratio', 'comp_margin', 'comp_reserve_margin', 'safety_discount'):
        if key not in constants:
            raise BenchmarkError('constants missing ' + key)
        if type(constants[key]) not in (int, float):
            raise BenchmarkError('constant ' + key + ' must be numeric')
    operations = profile['operations']
    if not isinstance(operations, dict):
        raise BenchmarkError('operations must be an object')
    if not set(operations) <= set(ALL_OPS):
        raise BenchmarkError('operations contains an unknown operation')
    for op, record in operations.items():
        if not isinstance(record, dict):
            raise BenchmarkError('operation record for ' + op + ' must be an object')
        if record.get('status') not in (STATUS_OK, STATUS_BLOCKED, STATUS_ERROR):
            raise BenchmarkError('operation ' + op + ' has an invalid status')
        problem = _sample_values_problem(record.get('samples'))
        if problem is not None:
            raise BenchmarkError('operation ' + op + ' samples ' + problem)
    if status == STATUS_COMPLETE:
        if set(operations) != set(ALL_OPS):
            raise BenchmarkError('a complete profile must record every operation')
        if any(operations[op].get('status') != STATUS_OK for op in ALL_OPS):
            raise BenchmarkError('a complete profile must have a measurement for every operation')
        # A formal profile needs at least N_MIN valid samples per operation; a
        # single sample can never be promoted to complete.
        for op in ALL_OPS:
            if len(operations[op]['samples']) < N_MIN:
                raise BenchmarkError('a complete profile requires at least %d valid samples for %s'
                                     % (N_MIN, op))
        if equivalence != EQUIVALENT:
            raise BenchmarkError('a complete profile requires EQUIVALENT cgroup environment')
        verdict = feasibility(profiles, reserve, floor)
        if not verdict['feasible']:
            raise BenchmarkError('profile is infeasible: required %d exceeds available_floor %d'
                                 % (verdict['required'], floor))
    env_fp = profile['environment_fingerprint']
    if env_fp is not None and not re.fullmatch(r'[0-9a-f]{64}', str(env_fp)):
        raise BenchmarkError('invalid environment_fingerprint')
    environment = profile['environment']
    if environment is not None and not isinstance(environment, dict):
        raise BenchmarkError('environment must be an object or null')
    if not isinstance(profile['issues'], list):
        raise BenchmarkError('issues must be a list')
    fingerprint = profile['fingerprint']
    if not re.fullmatch(r'[0-9a-f]{64}', str(fingerprint)):
        raise BenchmarkError('invalid fingerprint')
    if fingerprint != profile_fingerprint(profile):
        raise BenchmarkError('profile fingerprint mismatch')
    return True


def dumps_profile(profile):
    raw = _canonical(profile).encode('ascii')
    if len(raw) > MAX_PROFILE_BYTES:
        raise BenchmarkError('profile exceeds size limit')
    return raw


def loads_profile(raw):
    """Parse bytes into a profile and fully validate it.

    Responsibilities are split cleanly: this function only decodes the bytes,
    while every semantic and integrity rule lives in :func:`validate_profile`.
    """
    if len(raw) > MAX_PROFILE_BYTES:
        raise BenchmarkError('profile exceeds size limit')
    try:
        profile = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise BenchmarkError('profile is not valid JSON') from None
    validate_profile(profile)
    return profile


# ---------------------------------------------------- atomic profile writer --

def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_fd(fd):
    """Flush one open descriptor's bytes and metadata to stable storage.

    Kept as its own seam so the write-ahead journal's *file* fsync is distinct
    from the *directory* fsync: both must complete before a formal replacement
    is allowed, and a test can fail exactly one of them.
    """
    os.fsync(fd)


def _atomic_write(path, raw, mode=0o600):
    """Write ``raw`` to ``path`` through a same-directory rename, then fsync.

    Raises :class:`UncertainWrite` when the rename has already happened but the
    directory fsync failed: at that point the new bytes are visible and the
    durability of the rename cannot be proven.
    """
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix='.rpb-', dir=directory)
    replaced = False
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb', closefd=False) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(fd)
        os.replace(tmp, path)
        replaced = True
    finally:
        os.close(fd)
        if not replaced:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
    try:
        _fsync_dir(directory)
    except OSError as exc:
        raise UncertainWrite('rename completed but directory fsync failed: ' + type(exc).__name__) from None


# ------------------------------------------------ protected-file safety ------

def _canonical_target(path):
    """Absolute, symlink-resolved form of a path, used only for aliasing checks."""
    return os.path.realpath(os.path.abspath(path))


def _read_fd(fd, limit):
    """Read at most ``limit`` bytes from an already-verified descriptor."""
    chunks = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 1 << 16))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def _file_trust_problem(st):
    """Reject an existing profile file whose ownership or mode is unsafe."""
    if st.st_uid not in (0, os.geteuid()):
        return 'file is owned by uid ' + str(st.st_uid)
    if st.st_mode & 0o022:
        return 'file is writable by group or others'
    return None


def _symlink_problem(path):
    """Return a reason when ``path`` exists but is a symlink or non-regular file."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return 'cannot inspect: ' + type(exc).__name__
    if stat.S_ISLNK(st.st_mode):
        return 'path is a symbolic link'
    if not stat.S_ISREG(st.st_mode):
        return 'path is not a regular file'
    return None


def _parent_directory_problem(path):
    """Reject a parent directory where an atomic replace would be unsafe.

    The directory's type, owner and mode are all verified: a non-directory, a
    directory owned by neither root nor this process, or one that is group- or
    world-writable (without the sticky bit) can never host a protected file.
    """
    parent = os.path.dirname(os.path.abspath(path)) or '/'
    try:
        st = os.stat(parent)
    except OSError as exc:
        return 'parent directory unreadable: ' + type(exc).__name__
    if not stat.S_ISDIR(st.st_mode):
        return 'parent is not a directory'
    if st.st_uid not in (0, os.geteuid()):
        return 'parent directory is owned by uid ' + str(st.st_uid)
    if st.st_mode & 0o002 and not st.st_mode & stat.S_ISVTX:
        return 'parent directory is world-writable without the sticky bit'
    if st.st_mode & 0o020:
        return 'parent directory is group-writable'
    return None


class ProfileWriter:
    """Atomic, recoverable commit of a formal profile plus a same-directory backup.

    The two files are replaced independently; they are deliberately *not*
    presented as a single atomic transaction. A formal replace that succeeded
    but whose directory fsync failed (or whose re-read did not match) puts the
    writer into COMMIT_UNCERTAIN and persists a marker file next to the profile,
    so the uncertain state survives the process and blocks every later write
    until :meth:`recheck` confirms the on-disk state. ``--force`` can never skip
    that recovery check.
    """

    def __init__(self, path=DEFAULT_PROFILE_PATH, backup_suffix='.bak'):
        self.path = str(path)
        self.backup_path = self.path + backup_suffix
        self.journal_path = self.path + UNCERTAIN_SUFFIX
        self.disabled = False
        self.uncertain = self._journal_present()

    def candidate_path(self):
        base, ext = os.path.splitext(self.path)
        return base + '.candidate' + ext

    def candidate_conflict(self, candidate_path):
        """Return a reason when ``candidate_path`` aliases a protected file.

        A candidate is written before any formal commit, so it must be proven
        distinct from both the formal profile and its backup *before* a single
        byte is written. Paths are canonicalised (absolute with symlinks
        resolved), so ``./`` prefixes, aliases and symlinked parents cannot
        smuggle the candidate onto a protected path. The check never depends on
        a later :meth:`install` call noticing the problem.
        """
        candidate = _canonical_target(candidate_path)
        if candidate == _canonical_target(self.path):
            return 'candidate path resolves onto the formal profile path'
        if candidate == _canonical_target(self.backup_path):
            return 'candidate path resolves onto the backup profile path'
        return None

    def _journal_status(self):
        """Classify the write-ahead journal as ``'absent'``, ``'ok'`` or a reason.

        The journal is read through the same descriptor-level chain used for the
        protected files (``O_NOFOLLOW``, ``fstat`` type/owner/mode, device/inode
        re-check), so a symlink, a non-regular file, an untrusted owner or a file
        swapped mid-read is never mistaken for a valid journal. Fail-closed: a
        journal that exists but cannot be read, cannot be trusted or cannot be
        parsed is never treated as absent; only an ``'ok'`` journal may ever
        release the write block (see :meth:`recheck`). Read/write are checked
        independently of the formal file's parent directory, so a presence probe
        cannot be turned into a spurious failure.
        """
        raw, reason = self._read_file(self.journal_path, check_parent=False)
        if reason == 'absent':
            return 'absent'
        if reason is not None:
            return reason
        try:
            doc = json.loads(raw)
        except (ValueError, UnicodeError):
            return 'corrupt'
        if not isinstance(doc, dict) or 'reason' not in doc:
            return 'corrupt'
        return 'ok'

    def _journal_present(self):
        """True when any marker exists; an unreadable/corrupt marker still counts."""
        return self._journal_status() != 'absent'

    @staticmethod
    def _read_file(path, *, check_parent=True):
        """Read a protected file through a verified descriptor; ``(raw, reason)``.

        The file is opened with ``O_NOFOLLOW`` and then judged by its *descriptor*
        (``os.fstat``), not merely by a path-based ``lstat``: the device and inode
        of the descriptor are confirmed to match the path, the descriptor must be
        a regular file, and its ownership and mode must be trustworthy. The bytes
        are read from that same verified descriptor, and the path is inspected
        once more afterwards so a replacement during the read is detected. The
        parent directory's type, owner and mode are verified first, unless the
        caller passes ``check_parent=False`` (the journal reader does, because a
        mere presence probe must not be turned into a failure by an unrelated,
        separately-checked parent problem). ``reason`` is ``None`` on success,
        otherwise a short description of why the bytes cannot be trusted. A
        symlink is never followed, and the descriptor is always closed, including
        on every error path.
        """
        if check_parent:
            parent_problem = _parent_directory_problem(path)
            if parent_problem is not None:
                return None, parent_problem
        try:
            pre = os.lstat(path)
        except FileNotFoundError:
            return None, 'absent'
        except OSError as exc:
            return None, 'unreadable: ' + type(exc).__name__
        if stat.S_ISLNK(pre.st_mode):
            return None, 'symbolic link'
        if not stat.S_ISREG(pre.st_mode):
            return None, 'not a regular file'
        flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None, 'absent'
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return None, 'symbolic link'
            return None, 'unreadable: ' + type(exc).__name__
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return None, 'not a regular file'
            problem = _file_trust_problem(st)
            if problem is not None:
                return None, problem
            # The opened descriptor must be the very file the path names; a swap
            # between the lstat and the open shows up as a differing inode/device.
            if (st.st_dev, st.st_ino) != (pre.st_dev, pre.st_ino):
                return None, 'changed during open'
            try:
                raw = _read_fd(fd, MAX_PROFILE_BYTES + 1)
            except OSError as exc:
                return None, 'unreadable: ' + type(exc).__name__
        finally:
            os.close(fd)
        # Re-inspect the path after reading from the verified descriptor so a
        # replacement during the read is caught.
        try:
            post = os.lstat(path)
        except FileNotFoundError:
            return None, 'changed during read (absent)'
        except OSError as exc:
            return None, 'changed during read (' + type(exc).__name__ + ')'
        if (post.st_dev, post.st_ino) != (st.st_dev, st.st_ino):
            return None, 'changed during read'
        return raw, None

    @staticmethod
    def _verify(raw):
        if raw is None:
            return False, None, ['file absent']
        try:
            profile = loads_profile(raw)
        except BenchmarkError as exc:
            return False, None, [str(exc)]
        return True, profile, []

    def _write_file(self, path, raw):
        _atomic_write(path, raw)

    def _journal_payload(self, reason, transaction=None):
        """Build the journal document: transaction identity plus recovery info."""
        doc = {'reason': reason,
               'generated_at': _utcnow().isoformat(),
               'formal': os.path.abspath(self.path),
               'backup': os.path.abspath(self.backup_path)}
        if transaction is not None:
            doc['transaction'] = transaction
        return doc

    def _persist_journal(self, payload, *, exclusive):
        """Make the journal durable; returns ``(persisted, detail)``.

        Crash-safety is what matters here, not visibility. The bytes are written
        to a descriptor opened with ``O_NOFOLLOW`` and, when ``exclusive`` is set,
        ``O_CREAT|O_EXCL`` (so a symlink or a pre-existing file is refused), then
        flushed with a *file* fsync **and** a *directory* fsync before the call
        reports success. Only when both fsyncs succeeded is the journal re-read
        through the verified-descriptor chain and confirmed to carry exactly the
        bytes written. Any failure — an unsafe or pre-existing path, a write
        error, either fsync, or a read-back mismatch — returns ``(False, reason)``
        so the caller stops *before* touching the formal profile. A file that is
        merely visible is never reported as durable.
        """
        raw = _canonical(payload).encode('ascii')
        parent_problem = _parent_directory_problem(self.journal_path)
        if parent_problem is not None:
            return False, 'journal parent directory is unsafe: ' + parent_problem
        existing_problem = _symlink_problem(self.journal_path)
        if existing_problem is not None:
            return False, 'existing journal is unsafe: ' + existing_problem
        if exclusive:
            try:
                os.lstat(self.journal_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                return False, 'cannot inspect the journal: ' + type(exc).__name__
            else:
                return False, 'a journal already exists, so the transaction is unfinished'
        directory = os.path.dirname(os.path.abspath(self.journal_path))
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
        flags |= os.O_EXCL if exclusive else os.O_TRUNC
        try:
            fd = os.open(self.journal_path, flags, 0o600)
        except OSError as exc:
            return False, 'cannot create the journal: ' + type(exc).__name__
        try:
            os.fchmod(fd, 0o600)
            written = 0
            while written < len(raw):
                written += os.write(fd, raw[written:])
            try:
                _fsync_fd(fd)
            except OSError as exc:
                return False, 'journal file fsync failed: ' + type(exc).__name__
        except OSError as exc:
            return False, 'journal write failed: ' + type(exc).__name__
        finally:
            os.close(fd)
        try:
            _fsync_dir(directory)
        except OSError as exc:
            return False, 'journal directory fsync failed: ' + type(exc).__name__
        back, reason = self._read_file(self.journal_path, check_parent=False)
        if reason is not None:
            return False, 'journal is not durable: ' + reason
        if back != raw:
            return False, 'journal read-back did not match the bytes written'
        return True, None

    def _clear_journal(self):
        """Durably remove the journal; ``True`` only when removal is proven.

        The journal is unlinked and its directory fsynced. A missing file counts
        as already cleared, but an unlink failure or a directory fsync failure
        returns ``False``: the removal is then not proven to survive a crash, so
        the caller must keep the transaction blocked rather than assume safety.
        """
        try:
            os.unlink(self.journal_path)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        try:
            _fsync_dir(os.path.dirname(os.path.abspath(self.journal_path)))
        except OSError:
            return False
        return True

    def _scan(self):
        """Read-only view of the recoverable state of both protected files."""
        formal_raw, formal_reason = self._read_file(self.path)
        backup_raw, backup_reason = self._read_file(self.backup_path)
        formal_valid, formal, formal_issues = self._verify(formal_raw)
        backup_valid, backup, backup_issues = self._verify(backup_raw)
        if formal_raw is None and formal_reason not in (None, 'absent'):
            formal_issues = [formal_reason]
        if backup_raw is None and backup_reason not in (None, 'absent'):
            backup_issues = [backup_reason]
        return {'formal_valid': formal_valid, 'formal_issues': formal_issues,
                'backup_valid': backup_valid, 'backup_issues': backup_issues,
                'formal': formal, 'backup': backup,
                'recoverable': bool(formal_valid or backup_valid),
                'uncertain': bool(self.uncertain or self._journal_present())}

    def recheck(self):
        """Verify both protected files and release a persisted block when safe.

        This is the explicit recovery checkpoint. It re-reads the formal profile
        and its backup independently; only when it can confirm a trustworthy
        on-disk state *and* the journal is well-formed does it clear the journal
        *durably* (unlink plus directory fsync) and release the write block. A
        missing, unreadable, unparseable or unsafe journal, a formal/backup pair
        that cannot be confirmed, or a journal whose removal is not proven to
        survive a crash, all keep the block (fail-closed). The two files are
        never described as a single atomic transaction.
        """
        state = self._scan()
        status = self._journal_status()
        released = False
        if status == 'ok' and state['recoverable']:
            released = self._clear_journal()
        if released:
            self.disabled = False
            self.uncertain = False
        else:
            self.uncertain = self._journal_present()
        state['released'] = released
        state['uncertain'] = self.uncertain
        state['marker'] = status
        return state

    def install(self, candidate_raw, *, force=False, confirmed=False):
        """Commit a candidate profile to the formal path through a write-ahead log.

        The protocol is: (1) the recovery gate below runs first and ``--force``
        can never skip it; (2) a write-ahead journal, carrying the transaction
        identity and recovery information, is made durable (file fsync plus
        directory fsync) *before* any change to the formal profile — if it cannot
        be made durable the commit stops with the formal profile untouched; (3)
        the backup and then the formal file are replaced; (4) the formal file is
        re-read and must match the candidate bytes; (5) only once the durable
        formal state is proven is the journal durably removed, which releases the
        block. A replace that completed without a proven directory fsync, a
        re-read mismatch, or a journal that cannot be cleared, all enter
        COMMIT_UNCERTAIN and poison the writer across processes until
        :meth:`recheck` confirms the on-disk state. ``force`` only permits
        replacing an existing *valid* formal profile; it never bypasses
        completeness, equivalence, feasibility, confirmation or the recovery.
        """
        # Cross-process recovery first: a persisted journal (or an in-memory
        # block) refuses every write until an explicit recheck confirms the
        # state. This runs before ``force`` is consulted, so --force can never
        # skip it.
        if self.disabled or self.uncertain or self._journal_present():
            self.disabled = True
            self.uncertain = True
            return {'state': COMMIT_UNCERTAIN,
                    'reasons': ['a previous commit is uncertain; run recheck to confirm the '
                                'formal and backup files before writing again'],
                    'recheck': self._scan()}
        if confirmed is not True:
            return {'state': ABORTED, 'reasons': ['interactive confirmation not provided']}
        try:
            candidate_obj = json.loads(candidate_raw)
        except (ValueError, UnicodeError):
            return {'state': ABORTED, 'reasons': ['candidate is not valid JSON']}
        if not isinstance(candidate_obj, dict) or candidate_obj.get('status') != STATUS_COMPLETE:
            return {'state': ABORTED,
                    'reasons': ['only a complete profile may become formal; write the candidate path instead']}
        valid, profile, issues = self._verify(candidate_raw)
        if not valid:
            return {'state': ABORTED, 'reasons': ['invalid candidate: ' + '; '.join(issues)]}
        # Filesystem safety before any write: refuse to read or replace a
        # symlinked or non-regular formal/backup file, or to write inside an
        # unsafe parent directory.
        for label, target in (('formal', self.path), ('backup', self.backup_path)):
            problem = _symlink_problem(target)
            if problem is not None:
                return {'state': ABORTED, 'reasons': [label + ' path is unsafe: ' + problem]}
        parent_problem = _parent_directory_problem(self.path)
        if parent_problem is not None:
            return {'state': ABORTED, 'reasons': ['formal parent directory is unsafe: ' + parent_problem]}
        existing_raw, _existing_reason = self._read_file(self.path)
        existing_valid, _existing, _existing_issues = self._verify(existing_raw)
        if existing_valid and not force:
            return {'state': ABORTED, 'reasons': ['a valid formal profile exists; --force required']}
        # --- write-ahead journal: record the intent durably BEFORE any change ---
        transaction = hashlib.sha256(candidate_raw).hexdigest()
        persisted, detail = self._persist_journal(
            self._journal_payload('commit in progress', transaction=transaction), exclusive=True)
        if not persisted:
            reason = ('the write-ahead journal could not be made durable (' + str(detail) +
                      '); refusing to change the formal profile')
            if not self._clear_journal():
                self.disabled = True
                self.uncertain = True
                reason += ('; and the incomplete journal could not be cleared, so further '
                           'writes stay blocked until recheck')
            return {'state': ABORTED, 'reasons': [reason]}
        # Back up only a *verified* existing formal; an invalid file is no backup.
        if existing_valid:
            try:
                self._write_file(self.backup_path, existing_raw)
            except (OSError, UncertainWrite) as exc:
                reasons = ['backup write failed: ' + type(exc).__name__]
                if not self._clear_journal():
                    self.disabled = True
                    self.uncertain = True
                    reasons.append('the write-ahead journal could not be cleared; further '
                                   'writes stay blocked until recheck')
                return {'state': ABORTED, 'reasons': reasons}
        try:
            _atomic_write(self.path, candidate_raw)
        except UncertainWrite:
            return self.mark_uncertain('formal rename completed but its directory fsync failed')
        except OSError as exc:
            reasons = ['formal write failed: ' + type(exc).__name__]
            if not self._clear_journal():
                self.disabled = True
                self.uncertain = True
                reasons.append('the write-ahead journal could not be cleared; further writes '
                               'stay blocked until recheck')
            return {'state': ABORTED, 'reasons': reasons}
        reread, _reread_reason = self._read_file(self.path)
        if reread != candidate_raw:
            return self.mark_uncertain('formal re-read did not match the candidate bytes')
        # --- confirm: the durable formal state is proven; clear the journal ---
        if not self._clear_journal():
            return self.mark_uncertain('the formal profile is durable but its write-ahead '
                                       'journal could not be durably cleared')
        return {'state': COMMITTED, 'reasons': [], 'fingerprint': profile['fingerprint'],
                'backup_written': existing_valid}

    def mark_uncertain(self, reason):
        """Enter COMMIT_UNCERTAIN after a post-replace durability failure.

        The journal is (re)written durably so the uncertainty survives the
        process and blocks every later write until :meth:`recheck` confirms the
        files. If the journal itself cannot be made durable the outcome stays
        COMMIT_UNCERTAIN (never a success) and the failure is reported, so a
        broken journal is never mistaken for a safe state.
        """
        self.disabled = True
        self.uncertain = True
        persisted, detail = self._persist_journal(self._journal_payload(reason), exclusive=False)
        result = {'state': COMMIT_UNCERTAIN, 'reasons': [reason],
                  'recheck': self._scan(), 'marker_persisted': persisted}
        if not persisted:
            result['reasons'].append('the COMMIT_UNCERTAIN journal could not be made durable (' +
                                     str(detail) + '); this process still refuses every further write')
        return result


# --------------------------------------------------------- orchestration -----

def available_sampler(memory=None):
    """Return a callable that samples ``function.Memory().sample()['available']``.

    ``available`` is consumed exactly as the production gate computes it, so the
    ``RAM_LIMIT`` ceiling that is already inside it is never applied twice.
    """
    def sample():
        import function
        engine = memory if memory is not None else function.Memory()
        try:
            return int(engine.sample()['available'])
        except Exception:
            return None
    return sample


class MeasurementSession:
    """Run the idle baseline and every local operation through one meter."""

    def __init__(self, meter, sampler, capabilities=None, samples=N_MIN,
                 service_envelope=None, idle_samples=None):
        self.meter = meter
        self.sampler = sampler
        self.capabilities = capabilities if capabilities is not None else default_capabilities()
        self.samples = samples
        self.service_envelope = service_envelope
        self.idle_samples = idle_samples if idle_samples is not None else samples

    def _sample_available(self, sink):
        if self.sampler is None:
            return
        value = self.sampler()
        if value is not None:
            sink.append(int(value))

    def _measure_idle(self):
        increments = []
        for _ in range(self.idle_samples):
            try:
                result = self.meter.measure(IDLE_OP, None)
            except BenchmarkError:
                break  # no idle baseline is possible; margin stays conservative
            if result.get('status') == STATUS_OK:
                increments.append(int(result['increment']))
        return increments

    def _measure_local(self, op):
        fixture = build_fixture(op, self.capabilities)
        return measure_operation(self.meter, op, fixture, self.samples)

    def run(self, local_ops=None):
        ops = list(local_ops if local_ops is not None else LOCAL_OPS)
        available, envelopes = [], []
        self._sample_available(available)
        idle = self._measure_idle()
        self._sample_available(available)
        operations, per_op = {}, {}
        for op in ops:
            results = self._measure_local(op)
            record = summarize_operation(results)
            operations[op] = record
            if record['status'] == STATUS_OK:
                per_op[op] = record['filtered']
            for result in results:
                envelope = result.get('scope_envelope')
                if envelope:
                    envelopes.append(envelope)
            self._sample_available(available)
        return {'operations': operations, 'per_op': per_op, 'idle': idle,
                'available': available, 'scope_envelopes': envelopes}


def parse_external_specs(items):
    """Parse ``op=path`` CLI items into an ordered mapping."""
    specs = {}
    for item in items or []:
        if '=' not in item:
            raise BenchmarkError('external specification must be op=path: ' + item)
        op, path = item.split('=', 1)
        if op not in EXTERNAL_OPS:
            raise BenchmarkError('external specification names a non-installer operation: ' + op)
        specs[op] = path
    return specs


def load_external_operations(specs, local_envelope):
    """Validate every externally supplied installer measurement.

    A missing, unverifiable or non-equivalent document leaves the operation
    BLOCKED. No measured value is ever fabricated to complete the profile.
    """
    records = {}
    for op in EXTERNAL_OPS:
        path = specs.get(op)
        if not path:
            records[op] = {'status': STATUS_BLOCKED, 'samples': [], 'source': 'external',
                           'reasons': ['no external measurement supplied']}
            continue
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            records[op] = {'status': STATUS_BLOCKED, 'samples': [], 'source': 'external',
                           'reasons': ['external document unreadable: ' + type(exc).__name__]}
            continue
        try:
            doc = load_external_document(raw)
        except BenchmarkError as exc:
            records[op] = {'status': STATUS_BLOCKED, 'samples': [], 'source': 'external',
                           'reasons': [str(exc)]}
            continue
        verdict = validate_external_document(doc, local_envelope, expected_op=op)
        if verdict['verified']:
            kept, rejected = tukey_filter(verdict['increments'])
            records[op] = {'status': STATUS_OK, 'samples': verdict['increments'],
                           'filtered': kept, 'rejected': rejected, 'source': 'external'}
        else:
            records[op] = {'status': STATUS_BLOCKED, 'samples': [], 'source': 'external',
                           'reasons': verdict['issues']}
    return records


def compose_profile(outcome, externals, constants, env, equivalence, *, generated_at=None):
    """Merge local and external results into one profile document."""
    operations = dict(outcome['operations'])
    per_op = dict(outcome['per_op'])
    for op, record in externals.items():
        operations[op] = record
        if record['status'] == STATUS_OK:
            per_op[op] = record['filtered']
    available = outcome['available']
    issues = list(equivalence.get('reasons') or [])
    reserve = int(constants['comp_reserve_margin'])
    if not 0 <= reserve < RAM_LIMIT:
        issues.append('derived reserve is outside the hardware ceiling')
        reserve = 0
    if available:
        floor = available_floor(available, constants['safety_discount'])
    else:
        floor = 0
        issues.append('no usable available sample was collected; reserve and floor are unavailable')
    if not 0 <= floor <= RAM_LIMIT:
        issues.append('derived available_floor is outside the hardware ceiling')
        floor = 0
    profiles = {}
    for op in ALL_OPS:
        record = operations.get(op)
        if record and record.get('status') == STATUS_OK:
            values = per_op.get(op) or record.get('filtered') or record.get('samples') or []
            if values:
                budget = op_budget(values, constants)
                if 0 < budget < RAM_LIMIT:
                    profiles[op] = budget
                else:
                    issues.append(op + ' budget is outside the hardware ceiling')
    return build_profile(operations, constants, profiles, reserve, floor, env,
                         equivalence['state'], generated_at=generated_at, issues=issues)


# ------------------------------------------------------------------- CLI -----

def _cmd_env(args):
    env = capture_environment()
    sys.stdout.write(json.dumps(env, indent=2, ensure_ascii=False) + '\n')
    return 0 if env['valid'] else 3


def _cmd_scope_control(args):
    capabilities = None
    if args.capabilities:
        try:
            capabilities = load_capabilities(args.capabilities)
        except BenchmarkError:
            capabilities = {}
    result = run_scope_operation(args.op, capabilities=capabilities)
    sys.stdout.write(encode_result(result))
    sys.stdout.flush()
    # Stay alive until the parent acknowledges, so the scope unit survives
    # until the peak has been handed back.
    sys.stdin.readline()
    return 0


def _cmd_verify(args):
    raw = Path(args.profile).read_bytes()
    try:
        profile = loads_profile(raw)
    except BenchmarkError as exc:
        sys.stderr.write('INVALID: ' + str(exc) + '\n')
        return 1
    sys.stdout.write(json.dumps({'status': profile['status'], 'fingerprint': profile['fingerprint']}) + '\n')
    return 0


def _cmd_recheck(args):
    writer = ProfileWriter(args.profile)
    sys.stdout.write(json.dumps(writer.recheck(), indent=2, ensure_ascii=False) + '\n')
    return 0


def _cmd_install(args):
    raw = Path(args.candidate).read_bytes()
    writer = ProfileWriter(args.profile)
    result = writer.install(raw, force=args.force, confirmed=args.yes)
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    return 0 if result['state'] == COMMITTED else 2


def _cmd_external(args):
    """Validate one external installer measurement document, nothing more."""
    source = rp.Source(args.root) if args.root else rp.Source('/')
    local_envelope = None
    if args.service_cgroup:
        try:
            local_envelope = read_envelope(source, args.service_cgroup)
        except rp.ProbeError:
            local_envelope = None
    try:
        raw = Path(args.document).read_bytes()
        doc = load_external_document(raw)
    except (OSError, BenchmarkError) as exc:
        sys.stderr.write('INVALID: ' + str(exc) + '\n')
        return 1
    verdict = validate_external_document(doc, local_envelope)
    verdict['local_envelope_present'] = local_envelope is not None
    sys.stdout.write(json.dumps(verdict, indent=2, ensure_ascii=False) + '\n')
    return 0 if verdict['verified'] else 2


def _cmd_benchmark(args):
    """Measure every local operation and assemble the profile document.

    The three installer operations are only ever taken from validated external
    documents. A blocking condition (no ``memory.peak``, no scope, no service
    cgroup, a non-equivalent environment, an unprovisioned capability) yields an
    INCOMPLETE profile that may only be written to the candidate path. The
    candidate path is proven distinct from the formal profile and its backup
    before any write, so an incomplete candidate can never overwrite them.
    """
    writer = ProfileWriter(args.profile)
    candidate_path = args.candidate or writer.candidate_path()
    conflict = writer.candidate_conflict(candidate_path)
    if conflict is not None:
        sys.stderr.write('candidate path refused: ' + conflict + '\n')
        return 1
    source = rp.Source(args.root) if args.root else rp.Source('/')
    service_envelope, service_issue = None, None
    try:
        service_envelope = read_envelope(source, args.service_cgroup)
    except rp.ProbeError as exc:
        service_issue = str(exc)
    env = capture_environment(source=source)
    try:
        capabilities = load_capabilities(args.capabilities)
    except BenchmarkError as exc:
        sys.stderr.write('capability specification invalid: ' + str(exc) + '\n')
        return 1
    try:
        specs = parse_external_specs(args.external)
    except BenchmarkError as exc:
        sys.stderr.write(str(exc) + '\n')
        return 1
    meter = RealScopeMeter(systemd_run=args.systemd_run, slice_name=args.slice,
                           timeout=args.timeout, service_envelope=service_envelope,
                           capabilities_path=args.capabilities)
    session = MeasurementSession(meter, available_sampler(), capabilities,
                                 samples=args.samples, service_envelope=service_envelope,
                                 idle_samples=args.samples)
    outcome = session.run()
    externals = load_external_operations(specs, service_envelope)
    equivalence = overall_equivalence(service_envelope, outcome['scope_envelopes'])
    if service_issue is not None:
        equivalence['reasons'].append('service envelope: ' + service_issue)
        if equivalence['state'] == EQUIVALENT:
            equivalence['state'] = INDETERMINATE
    constants = compute_constants(outcome['per_op'], outcome['idle'], outcome['available'])
    profile = compose_profile(outcome, externals, constants, env, equivalence)
    candidate_raw = dumps_profile(profile)
    candidate_written, candidate_issue = False, None
    try:
        _atomic_write(candidate_path, candidate_raw)
        candidate_written = True
    except UncertainWrite:
        candidate_issue = 'candidate rename completed but its directory fsync failed'
    except OSError as exc:
        candidate_issue = 'candidate write failed: ' + type(exc).__name__
    report = {
        'status': profile['status'],
        'cgroup_equivalence': equivalence['state'],
        'operations': dict((op, {'status': record.get('status'), 'source': record.get('source', 'local')})
                           for op, record in profile['operations'].items()),
        'profiles': profile['profiles'],
        'reserve': profile['reserve'],
        'available_floor': profile['available_floor'],
        'rejection_reasons': rejection_reasons(constants),
        'issues': list(profile['issues']) + ([candidate_issue] if candidate_issue else []),
        'candidate_path': candidate_path,
        'candidate_written': candidate_written,
    }
    if args.install:
        report['install'] = writer.install(candidate_raw, force=args.force, confirmed=args.yes)
    sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    return 0 if profile['status'] == STATUS_COMPLETE else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description='V1 resource-profile benchmark')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('env', help='print the local cgroup environment block')
    p_ctrl = sub.add_parser('_scope-control', help=argparse.SUPPRESS)
    p_ctrl.add_argument('op')
    p_ctrl.add_argument('--capabilities')
    p_bench = sub.add_parser('benchmark', help='measure local operations and assemble a profile')
    p_bench.add_argument('--profile', default=DEFAULT_PROFILE_PATH)
    p_bench.add_argument('--candidate', help='candidate path (defaults to <profile>.candidate.json)')
    p_bench.add_argument('--service-cgroup', default='/system.slice/node-panel.service')
    p_bench.add_argument('--slice', default=DEFAULT_SCOPE_SLICE)
    p_bench.add_argument('--systemd-run')
    p_bench.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT)
    p_bench.add_argument('--samples', type=int, default=N_MIN)
    p_bench.add_argument('--capabilities', help='capability specification file (op -> command)')
    p_bench.add_argument('--external', action='append', default=[],
                         help='installer measurement as op=path (repeatable)')
    p_bench.add_argument('--root', help='alternate filesystem root (testing only)')
    p_bench.add_argument('--install', action='store_true')
    p_bench.add_argument('--force', action='store_true')
    p_bench.add_argument('--yes', action='store_true')
    p_external = sub.add_parser('external', help='validate an external installer measurement document')
    p_external.add_argument('--document', required=True)
    p_external.add_argument('--service-cgroup', default=None)
    p_external.add_argument('--root')
    p_verify = sub.add_parser('verify', help='validate a profile document')
    p_verify.add_argument('--profile', required=True)
    p_recheck = sub.add_parser('recheck', help='describe the formal/backup recovery state')
    p_recheck.add_argument('--profile', default=DEFAULT_PROFILE_PATH)
    p_install = sub.add_parser('install', help='atomically commit a complete candidate profile')
    p_install.add_argument('--candidate', required=True)
    p_install.add_argument('--profile', default=DEFAULT_PROFILE_PATH)
    p_install.add_argument('--force', action='store_true')
    p_install.add_argument('--yes', action='store_true')
    args = parser.parse_args(argv)
    handlers = {'env': _cmd_env, '_scope-control': _cmd_scope_control, 'benchmark': _cmd_benchmark,
                'external': _cmd_external, 'verify': _cmd_verify,
                'recheck': _cmd_recheck, 'install': _cmd_install}
    return handlers[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
