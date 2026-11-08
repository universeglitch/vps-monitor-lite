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


class XrayTransaction:
    """Single-writer, crash-readable transactions over a private Store.

    Caller supplies candidate inbounds; no protocol/port/key defaults are invented.
    configure() requires a one-use confirmation tied to config hash and auth epoch.
    The frozen legacy panel must not write concurrently: fingerprint CAS is not
    atomic against a noncooperating root writer. Deployment must enforce single writer.
    """
    TERMINAL = ('COMMITTED', 'ROLLED_BACK')

    def __init__(self, store, config, gate, test, service, authorize):
        self.store, self.path, self.gate = store, Path(config), gate
        self.test, self.service, self.authorize = test, service, authorize
        self.mutex = threading.RLock()
        self.confirmations = {}

    def _read(self):
        import stat
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 262144:
                raise AuthError('unsafe Xray configuration')
            with os.fdopen(fd, 'rb', closefd=False) as f:
                raw = f.read(262145)
            if len(raw) > 262144:
                raise AuthError('Xray configuration size limit')
            return raw, (st.st_mode & 0o777, st.st_uid, st.st_gid)
        finally:
            os.close(fd)

    @staticmethod
    def fingerprint(raw):
        import hashlib
        return hashlib.sha256(raw).hexdigest()

    def _record(self, record):
        from auth import dumps
        self.store.write('xray-transaction.json', dumps(record))

    def pending(self):
        from auth import loads
        raw = self.store.read('xray-transaction.json', missing=True)
        if raw is None:
            return None
        r = loads(raw)
        if r.get('stage') not in ('PREPARED','CONFIG_APPLIED','SERVICE_VERIFIED','COMMITTED','ROLLBACK_REQUIRED','ROLLED_BACK'):
            raise AuthError('invalid Xray transaction journal')
        for field in ('old_hash','new_hash'):
            import re
            if not re.fullmatch('[0-9a-f]{64}', r.get(field,'')):
                raise AuthError('invalid Xray transaction fingerprint')
        if type(r.get('attempts')) is not int or not 0 <= r['attempts'] <= 1:
            raise AuthError('invalid rollback attempt count')
        return r

    def confirm(self, token):
        epoch = self.authorize(token)
        with self.mutex, self.store.transaction():
            pending = self.pending()
            if pending and pending['stage'] not in self.TERMINAL:
                raise AuthError('unfinished Xray transaction requires recovery')
            raw, _ = self._read()
            ident = __import__('secrets').token_hex(32)
            now = time.monotonic()
            self.confirmations = {k:v for k,v in self.confirmations.items() if v['expires'] > now}
            if len(self.confirmations) >= 16:
                raise Busy('confirmation capacity reached')
            self.confirmations[ident] = {'epoch': epoch, 'token': __import__('hashlib').sha256(token.encode()).hexdigest(),
                                        'hash': self.fingerprint(raw), 'expires': now + 120}
            return {'confirmation': ident, 'config_hash': self.fingerprint(raw),
                    'warning': 'Applying configuration may restart Xray and disconnect existing clients', 'expires_in': 120}

    def _replace(self, raw, meta, expected):
        import tempfile
        current, _ = self._read()
        if self.fingerprint(current) != expected:
            raise AuthError('external configuration change; refusing overwrite')
        fd, tmp = tempfile.mkstemp(prefix='.vps-config-', dir=self.path.parent)
        try:
            os.fchmod(fd, meta[0])
            current_stat = os.fstat(fd)
            if (current_stat.st_uid, current_stat.st_gid) != (meta[1], meta[2]):
                os.fchown(fd, meta[1], meta[2])
            with os.fdopen(fd, 'wb', closefd=False) as f:
                f.write(raw); f.flush(); os.fsync(fd)
            again, _ = self._read()
            if self.fingerprint(again) != expected:
                raise AuthError('external configuration change before replacement')
            os.replace(tmp, self.path)
            d = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(d)
            finally: os.close(d)
            if self.fingerprint(self._read()[0]) != self.fingerprint(raw):
                raise AuthError('external modification after configuration replacement')
        finally:
            os.close(fd)
            try: os.unlink(tmp)
            except FileNotFoundError: pass

    def configure(self, token, confirmation, replacements):
        from auth import dumps, loads, b64
        epoch = self.authorize(token)
        if not isinstance(replacements, dict) or not replacements or len(replacements) > 3:
            raise AuthError('bounded inbound mapping required')
        allowed = ('vps-monitor-ws', 'vps-monitor-xhttp', 'vps-monitor-reality')
        for tag, inbound in replacements.items():
            if tag not in allowed or (inbound is not None and (not isinstance(inbound,dict) or inbound.get('tag') != tag)):
                raise AuthError('only explicitly owned inbound tags can be changed')
        with self.gate('xray-test'), self.mutex, self.store.transaction():
            permit = self.confirmations.pop(confirmation, None)
            if (not permit or permit['expires'] <= time.monotonic() or permit['epoch'] != epoch or
                permit['token'] != __import__('hashlib').sha256(token.encode()).hexdigest()):
                raise AuthError('valid restart confirmation required')
            old, meta = self._read()
            if self.fingerprint(old) != permit['hash']:
                raise AuthError('configuration changed since confirmation')
            config = loads(old)
            if not isinstance(config, dict) or not isinstance(config.get('inbounds'), list):
                raise AuthError('single JSON config with inbounds required; JSONC not rewritten')
            owned_raw = self.store.read('xray-owned.json', missing=True)
            owned = loads(owned_raw) if owned_raw else []
            # Newly added owned tags must not collide with existing user tags.
            existing = [i.get('tag') for i in config['inbounds'] if isinstance(i,dict)]
            if len([t for t in existing if t is not None]) != len(set(t for t in existing if t is not None)):
                raise AuthError('duplicate inbound tags')
            for tag in replacements:
                if tag in existing and tag not in owned:
                    raise AuthError('existing inbound is not owned by this project')
            inbounds=[]
            for inbound in config['inbounds']:
                if not isinstance(inbound,dict): raise AuthError('invalid inbound')
                tag=inbound.get('tag')
                if tag in replacements:
                    if replacements[tag] is not None: inbounds.append(replacements[tag])
                else: inbounds.append(inbound)
            for tag,inbound in replacements.items():
                if tag not in existing and inbound is not None: inbounds.append(inbound)
            config['inbounds']=inbounds
            candidate=dumps(config)
            self.gate.checkpoint()
            self.test(candidate)  # injected bounded Xray self-test, never shell text
            if self.authorize(token) != epoch: raise AuthError('Session epoch changed')
            if self.fingerprint(self._read()[0]) != permit['hash']: raise AuthError('external configuration change during test')
            before = self.service.inspect('xray')
            if not before['installed']: raise AuthError('Xray unit not installed')
            record={'stage':'PREPARED','old_hash':permit['hash'],'new_hash':self.fingerprint(candidate),
                    'old':b64(old),'candidate':b64(candidate),'meta':list(meta),'attempts':0,
                    'was_active':before['active'], 'owned': sorted(set(owned) | set(replacements))}
            self._record(record)
            try:
                self._replace(candidate,meta,permit['hash'])
                record['stage']='CONFIG_APPLIED'; self._record(record)
                # Restart authorized for this exact transaction, not future requests.
                self.service.apply_xray(before['active'])
                if self.fingerprint(self._read()[0]) != record['new_hash']: raise AuthError('external configuration change during service verification')
                record['stage']='SERVICE_VERIFIED'; self._record(record)
                self.store.write('xray-owned.json',dumps(record['owned']))
                record['stage']='COMMITTED'; self._record(record)
                return {'stage':'COMMITTED','config_hash':record['new_hash']}
            except Exception:
                record['stage']='ROLLBACK_REQUIRED'; self._record(record)
                self._rollback(record)
                raise

    def _rollback(self, r):
        from auth import unb64
        current=self.fingerprint(self._read()[0])
        if current not in (r['old_hash'],r['new_hash']):
            raise AuthError('external modification; rollback refuses overwrite')
        if r['attempts'] >= 1:
            raise AuthError('rollback already attempted; manual recovery required')
        r['attempts']=1; r['stage']='ROLLBACK_REQUIRED'; self._record(r)
        old=unb64(r['old'])
        if self.fingerprint(old)!=r['old_hash']: raise AuthError('backup fingerprint mismatch')
        if current!=r['old_hash']: self._replace(old,r['meta'],r['new_hash'])
        self.service.apply_xray(r['was_active'])
        r['stage']='ROLLED_BACK'; self._record(r)

    def recover(self, token, restart_confirmed=False):
        from auth import dumps
        self.authorize(token)
        with self.gate('xray-test'), self.mutex, self.store.transaction():
            r=self.pending()
            if r is None or r['stage'] in self.TERMINAL: return r
            current=self.fingerprint(self._read()[0])
            if current not in (r['old_hash'],r['new_hash']): raise AuthError('external change; manual recovery required')
            if r['stage']=='PREPARED' and current==r['old_hash']:
                r['stage']='ROLLED_BACK';self._record(r);return r
            # Active alone cannot prove which configuration is loaded. Never restart
            # a healthy proxy merely because management restarted.
            if restart_confirmed is not True:
                return {'stage':r['stage'],'requires_confirmation':True}
            self._rollback(r)
            return {'stage':r['stage']}
