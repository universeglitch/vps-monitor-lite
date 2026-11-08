"""Coordination only. No import-time services or guessed Xray protocol settings."""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from auth import Auth, AuthError, Busy

RAM_LIMIT = 200_000_000


class Memory:
    """Measure MemAvailable and every visible cgroup-v2 ancestor.

    A hidden ancestor/unsupported layout cannot be proven safe: deny admission.
    Measurements are snapshots, not kernel reservations or OOM guarantees.
    """
    def __init__(self, proc=Path('/proc'), cgroup=Path('/sys/fs/cgroup')):
        self.proc, self.cgroup = Path(proc), Path(cgroup)
        self.previous = None
        self.lock = threading.Lock()

    def sample(self):
        with self.lock:
            available = None
            for line in (self.proc / 'meminfo').read_text().splitlines():
                if line.startswith('MemAvailable:'):
                    available = int(line.split()[1]) * 1024
            if available is None:
                raise Busy('MemAvailable unavailable')
            rel = None
            for line in (self.proc / 'self/cgroup').read_text().splitlines():
                if line.startswith('0::'):
                    rel = line[3:]
            if rel is None or '..' in Path(rel).parts:
                raise Busy('cgroup-v2 memory boundary unavailable')
            cur = self.cgroup / rel.lstrip('/')
            if not cur.is_dir():
                raise Busy('cgroup namespace mapping unavailable')
            margins, readings, events = [available], [], []
            while True:
                current = int((cur / 'memory.current').read_text())
                maximum = (cur / 'memory.max').read_text().strip()
                peak = int((cur / 'memory.peak').read_text())
                ev = dict(line.split() for line in (cur / 'memory.events').read_text().splitlines())
                events.append((str(cur), tuple((k, int(ev.get(k, 0))) for k in ('high','max','oom','oom_kill'))))
                readings.append((str(cur), current, peak))
                if maximum != 'max':
                    margins.append(max(0, int(maximum) - current))
                if cur == self.cgroup:
                    # Apply decimal hardware ceiling to the top visible accounting domain too.
                    margins.append(max(0, RAM_LIMIT - current))
                    break
                cur = cur.parent
                if self.cgroup not in (cur, *cur.parents):
                    raise Busy('invalid cgroup ancestry')
            now = time.monotonic()
            pressure = False
            if self.previous:
                at, previous, old_events = self.previous
                old = dict((p, c) for p, c, _ in previous)
                pressure = any(c > old.get(p, c) for p, c, _ in readings) and now - at < 10
                pressure |= events != old_events
            self.previous = (now, readings, events)
            return {'available': min(margins), 'pressure': pressure,
                    'cgroups': readings, 'events': events, 'at': now}


class ResourceGate:
    """One login lane and one ordinary lane, one waiter each; single KDF in auth.

    Profiles and reserves MUST be supplied from operator measurements. No profile
    means deny, not an optimistic fabricated 200 MB memory budget. No auto stop.
    """
    def __init__(self, memory, profiles=None, reserve=None, timeout=2.0):
        self.memory = memory
        self.profiles = profiles or {}
        self.reserve = reserve
        self.timeout = timeout
        self.cv = threading.Condition()
        self.active = {'login': False, 'normal': False}
        self.waiting = {'login': 0, 'normal': 0}
        self.reserved = {'login': 0, 'normal': 0}

    def checkpoint(self, extra=0):
        s = self.memory.sample()
        if self.reserve is None or s['pressure'] or s['available'] < self.reserve + extra:
            raise Busy('memory pressure; operation refused without stopping proxies')
        return s

    @contextmanager
    def __call__(self, kind):
        lane = 'login' if kind == 'login' else 'normal'
        need = self.profiles.get(kind)
        login = self.profiles.get('login')
        if type(need) is not int or need <= 0 or type(login) is not int or login <= 0 or self.reserve is None:
            raise Busy('unmeasured operation profile; admission refused')
        deadline = time.monotonic() + self.timeout
        with self.cv:
            if self.waiting[lane] >= 1:
                raise Busy('bounded operation queue full')
            self.waiting[lane] += 1
            try:
                while self.active[lane] or (lane == 'normal' and self.waiting['login']):
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise Busy('operation wait deadline exceeded')
                    self.cv.wait(left)
                # Ordinary lane always leaves budget for one login. Already active
                # allocations are reflected by current; reservation adds conservatism.
                other = self.reserved['normal' if lane == 'login' else 'login']
                extra = need + other + (login if lane == 'normal' and not self.active['login'] else 0)
                self.checkpoint(extra)
                self.active[lane] = True; self.reserved[lane] = need
            finally:
                self.waiting[lane] -= 1
                self.cv.notify_all()
        try:
            self.checkpoint(need)
            yield
        finally:
            with self.cv:
                self.active[lane] = False; self.reserved[lane] = 0
                self.cv.notify_all()


class Coordinator:
    def __init__(self, auth: Auth, active, gate: ResourceGate):
        self.auth, self.active, self.gate = auth, active, gate

    def status(self, token):
        self.auth.validate(token)
        return self.active.snapshot()

    def switch(self, token, service, enabled):
        epoch = self.auth.validate(token)
        if self.auth.validate(token) != epoch:
            raise AuthError('authentication changed')
        if enabled is True:
            raise AuthError('start requires configuration readiness validation')
        return self.active.set_service(service, enabled)

    def api(self, token, action, **args):
        self.auth.validate(token)
        methods = {'list': self.auth.list_apis, 'create': self.auth.create_api,
                   'read': self.auth.read_api, 'delete': self.auth.delete_api}
        if action not in methods:
            raise AuthError('unsupported API operation')
        return methods[action](token, **args)

    def configure_xray(self, token, request):
        self.auth.validate(token)
        # No guessed protocol or restart permission; explicit refusal until the
        # durable single-writer transaction implementation is reviewed.
        raise AuthError('Xray configuration writer not enabled: protocol parameters and durable recovery validation required')

    def install(self, token, service, confirmed=False):
        self.auth.validate(token)
        if confirmed is not True:
            raise AuthError('explicit installation confirmation required')
        raise AuthError('installation refused: no measured installer resource profile or approved recipe')
