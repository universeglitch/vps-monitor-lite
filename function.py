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
    def __init__(self, auth: Auth, active, gate: ResourceGate, xray=None, discovery=None, installer=None):
        self.auth, self.active, self.gate = auth, active, gate
        self.xray, self.discovery, self.installer = xray, discovery, installer

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
        if self.xray is None: raise AuthError('Xray transaction backend not configured')
        if not isinstance(request,dict): raise AuthError('configuration request required')
        return self.xray.configure(token,request.get('confirmation'),request.get('inbounds'))

    def confirm_xray(self, token):
        self.auth.validate(token)
        if self.xray is None: raise AuthError('Xray transaction backend not configured')
        return self.xray.confirm(token)

    def discover(self, token, service):
        self.auth.validate(token)
        if self.discovery is None: raise AuthError('discovery unavailable')
        return self.discovery.inspect(service)

    def install(self, token, service, confirmation=None):
        epoch=self.auth.validate(token)
        if self.installer is None: raise AuthError('approved installer recipes unavailable')
        if confirmation is None:return self.installer.prepare(service,epoch)
        return self.installer.execute(service,epoch,confirmation)


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
        self.service.apply_xray(r['was_active'], rollback=True)
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


class Discovery:
    """Read-only local discovery; absent program != missing config.

    No guesses for Cloudflared credentials/command line. Explicit config paths
    are deployment inputs. No legacy panel import and no port auto-allocation.
    """
    def __init__(self, active, xray_config=Path('/usr/local/etc/xray/config.json'), cloud_config=None):
        self.active=active;self.xray_config=Path(xray_config)
        self.cloud_config=Path(cloud_config) if cloud_config else None

    def inspect(self, name):
        import shutil
        from auth import loads
        if name not in ('xray','cloudflared'):raise AuthError('unsupported service')
        program=shutil.which(name)
        unit=self.active.inspect(name)
        path=self.xray_config if name=='xray' else self.cloud_config
        missing=[]
        if not program:missing.append('program')
        if not unit['installed']:missing.append('systemd unit')
        if missing:return dict(unit,readiness='not_installed',missing=missing)
        if path is None or not path.is_file() or path.is_symlink():
            return dict(unit,readiness='not_configured',missing=['verified config path'])
        if name=='xray':
            if path.stat().st_size>262144:raise AuthError('configuration size limit')
            try:
                data=loads(path.read_bytes())
                if not isinstance(data,dict) or not isinstance(data.get('inbounds'),list):raise ValueError()
            except (ValueError,AuthError):return dict(unit,readiness='config_error',missing=[])
        # JSON syntax/file existence alone never substitutes for executable test.
        return dict(unit,readiness='requires_validation',config=str(path),program=program,missing=[])


class BoundedCommand:
    """Trusted argv only; bounded stdout/stderr, timeout and pressure checkpoints.

    A task requiring cgroup isolation is refused unless a measured task launcher
    is explicitly injected. Never accepts shell strings from HTTP.
    """
    def __init__(self, gate, launcher=None):
        self.gate,self.launcher=gate,launcher

    def run(self, argv, timeout=30, isolated=True):
        import subprocess,selectors
        if not isinstance(argv,(list,tuple)) or not argv or any(not isinstance(a,str) for a in argv):
            raise AuthError('trusted argv required')
        if isolated:
            if self.launcher is None:raise Busy('measured transient task cgroup is not configured')
            argv=self.launcher(tuple(argv))
        self.gate.checkpoint()
        p=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                           env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C'},start_new_session=True)
        sel=selectors.DefaultSelector();output=bytearray();deadline=time.monotonic()+timeout
        for stream in (p.stdout,p.stderr):sel.register(stream,selectors.EVENT_READ)
        try:
            while sel.get_map():
                if time.monotonic()>deadline:raise Busy('task timeout')
                self.gate.checkpoint()
                for key,_ in sel.select(.25):
                    raw=os.read(key.fileobj.fileno(),4096)
                    if not raw:sel.unregister(key.fileobj);continue
                    output.extend(raw)
                    if len(output)>65536:raise Busy('task output limit')
            returncode=p.wait(timeout=max(.01,deadline-time.monotonic()))
            if returncode:raise AuthError('task failed; inspect protected operation log')
            return bytes(output)
        finally:
            if p.poll() is None:
                import signal
                os.killpg(p.pid,signal.SIGKILL)
            p.wait();sel.close();p.stdout.close();p.stderr.close()


class XraySelfTest:
    def __init__(self, runner, executable):
        self.runner,self.executable=runner,executable
    def __call__(self, raw):
        import tempfile
        if not Path(self.executable).is_file():raise AuthError('Xray executable missing')
        fd,path=tempfile.mkstemp(prefix='vps-xray-test-',suffix='.json')
        try:
            os.fchmod(fd,0o600)
            with os.fdopen(fd,'wb',closefd=False) as f:f.write(raw);f.flush();os.fsync(fd)
            self.runner.run([self.executable,'run','-test','-config',path],timeout=30)
        finally:
            os.close(fd);os.unlink(path)


class ConfirmedInstaller:
    """Admin-provisioned fixed recipes, digest checked; no guessed installers.

    This runner is for bounded tasks safe to terminate. General package managers
    are refused: cancelling them under pressure can leave the OS inconsistent.
    """
    def __init__(self, gate, runner, discovery, recipes=None):
        self.gate,self.runner,self.discovery=gate,runner,discovery
        self.recipes=recipes or {};self.permits={};self.lock=threading.Lock()
    def prepare(self, name, epoch):
        import secrets
        info=self.discovery.inspect(name)
        if info['readiness']!='not_installed':return {'installation_needed':False,'discovery':info}
        with self.lock:
            now=time.monotonic();self.permits={k:v for k,v in self.permits.items() if v[2]>now}
            if len(self.permits)>=8:raise Busy('confirmation queue full')
            token=secrets.token_hex(32);self.permits[token]=(name,epoch,now+120)
        return {'installation_needed':True,'missing':info['missing'],'confirmation':token}
    def execute(self, name, epoch, confirmation):
        with self.lock:permit=self.permits.pop(confirmation,None)
        if not permit or permit[0]!=name or permit[1]!=epoch or permit[2]<=time.monotonic():
            raise AuthError('installation confirmation expired or mismatched')
        recipe=self.recipes.get(name)
        if not recipe or recipe.get('safe_to_terminate') is not True:
            raise AuthError('bounded installation recipe unavailable; no automatic package-manager invocation')
        import hashlib,stat
        path=Path(recipe['path']);st=path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_mode&0o022 or st.st_size>262144:
            raise AuthError('unsafe installer recipe')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=recipe['sha256']:
            raise AuthError('installer recipe digest mismatch')
        with self.gate('install-'+name):
            self.runner.run([str(path)],timeout=recipe.get('timeout',120))
            after=self.discovery.inspect(name)
            if after['readiness']=='not_installed':raise AuthError('post-install verification failed')
            return after


class PanelBackend:
    """Authenticated shared state for both independent page implementations.

    No listener. All configuration mutations delegate to XrayTransaction. Node
    names/preferences are separate from protocol settings and contain no secrets.
    """
    def __init__(self, coordinator, panel_config=Path('/etc/node-panel/panel-config.json'), port=None):
        self.c=coordinator;self.store=coordinator.auth.store
        self.panel_config=Path(panel_config);self.port=port
        self.permits={};self.permit_lock=threading.Lock()

    def preferences(self, token, default=None):
        from auth import loads,dumps
        self.c.auth.validate(token)
        with self.store.transaction():
            raw=self.store.read('panel-preferences.json',missing=True)
            p=loads(raw) if raw else {'default_panel':'main','names':{}}
            if default is not None:
                if default not in ('main','panel'):raise AuthError('invalid default panel')
                p['default_panel']=default;self.store.write('panel-preferences.json',dumps(p))
            return p

    def _configuration(self):
        from auth import loads
        if self.c.xray is None:raise AuthError('Xray backend unavailable')
        raw,_=self.c.xray._read();data=loads(raw)
        if not isinstance(data,dict) or not isinstance(data.get('inbounds'),list):raise AuthError('invalid Xray configuration')
        return data

    def metadata(self):
        from auth import loads
        try:
            if self.panel_config.is_symlink() or self.panel_config.stat().st_size>65536:raise AuthError('unsafe panel config')
            return loads(self.panel_config.read_bytes())
        except FileNotFoundError:return {}

    @staticmethod
    def node_id(inbound):
        import hashlib
        # Stable across port/name changes for tagged nodes; untagged uses client
        # identity rather than position, and ambiguity is rejected by enumeration.
        identity=inbound.get('tag') or str((inbound.get('settings') or {}).get('clients'))
        return hashlib.sha256(str(identity).encode()).hexdigest()[:24]

    def nodes(self, token, secrets_allowed=False):
        import uuid
        from urllib.parse import urlencode,quote
        self.c.auth.validate(token)
        try:data=self._configuration()
        except (OSError,AuthError):return []
        cfg=self.metadata();prefs=self.preferences(token)
        cfg.update(prefs.get('node_metadata',{}))
        names=prefs['names'];out=[];seen=set()
        for inbound in data['inbounds']:
            if not isinstance(inbound,dict) or inbound.get('protocol')!='vless':continue
            stream=inbound.get('streamSettings') or {};net=stream.get('network');security=stream.get('security','none')
            kind='ws' if net=='ws' and security in ('none','tls') else 'reality' if net in ('tcp','raw') and security=='reality' else 'xhttp' if net=='xhttp' and security in ('none','tls') else None
            if kind is None:continue
            ident=self.node_id(inbound)
            if ident in seen:continue
            seen.add(ident)
            clients=(inbound.get('settings') or {}).get('clients') or []
            try:
                uid=str(uuid.UUID(clients[0]['id']));listen=int(inbound['port'])
                if not 1<=listen<=65535:continue
                if kind=='ws':
                    meta=cfg.get('ws') or {};host=meta.get('host');port=int(meta.get('port',0));path=(stream.get('wsSettings') or {}).get('path')
                    if not host or not path or not path.startswith('/') or not 1<=port<=65535:continue
                    query={'encryption':'none','security':'tls','sni':host,'fp':'chrome','type':'ws','host':host,'path':path}
                elif kind=='reality':
                    meta=cfg.get('reality') or {};rs=stream.get('realitySettings') or {};host=meta.get('address');port=int(meta.get('port',0));public=meta.get('public_key')
                    snis=rs.get('serverNames') or [];shorts=rs.get('shortIds') or []
                    if not host or not public or not snis or not shorts or not 1<=port<=65535:continue
                    query={'encryption':'none','security':'reality','sni':snis[0],'fp':meta.get('fingerprint','chrome'),'pbk':public,'sid':shorts[0],'type':'tcp'}
                    if clients[0].get('flow'):query['flow']=clients[0]['flow']
                else:
                    meta=cfg.get('xhttp') or {};host=meta.get('host');port=int(meta.get('port',0));xs=stream.get('xhttpSettings') or {}
                    if not host or not xs.get('path') or not 1<=port<=65535:continue
                    query={'encryption':'none','security':security,'type':'xhttp','path':xs['path']}
                    if security=='tls':query['sni']=(stream.get('tlsSettings') or {}).get('serverName',host)
                    if xs.get('mode'):query['mode']=xs['mode']
                import ipaddress
                try:address=str(ipaddress.ip_address(host))
                except ValueError:
                    if not __import__('re').fullmatch(r'[A-Za-z0-9.-]{1,253}',host):continue
                    address=host
                name=names.get(ident,{'ws':'VLESS · WS + TLS','reality':'VLESS · REALITY','xhttp':'VLESS · XHTTP'}[kind])
                node={'id':ident,'name':name,'type':kind,'address':host,'port':port,'listen_port':listen,'tag':inbound.get('tag')}
                if secrets_allowed:
                    if ':' in address:address='['+address+']'
                    node['uri']='vless://'+uid+'@'+address+':'+str(port)+'?'+urlencode(query,quote_via=quote)+'#'+quote(name)
                out.append(node)
            except (KeyError,TypeError,ValueError,IndexError):continue
        return out

    def rename(self, token, ident, name):
        from auth import dumps
        self.c.auth.validate(token)
        if not isinstance(name,str) or not 1<=len(name.strip())<=80 or any(ord(c)<32 for c in name):raise AuthError('invalid display name')
        if ident not in {n['id'] for n in self.nodes(token)}:raise AuthError('node not found')
        with self.store.transaction():
            p=self.preferences(token);p['names'][ident]=name.strip()
            if len(p['names'])>128:raise AuthError('name capacity reached')
            self.store.write('panel-preferences.json',dumps(p))
        return {'ok':True}

    def share(self, token, ident):
        for n in self.nodes(token,True):
            if n['id']==ident:return n['uri']
        raise AuthError('valid node not found')

    def qr(self, token, ident):
        # No precomputed image/cache, no QR import until explicit authenticated reveal.
        import qrcode
        import qrcode.image.svg
        import io,base64
        uri=self.share(token,ident)
        if len(uri)>4096:raise AuthError('share link exceeds QR limit')
        with self.c.gate('qr'):
            image=qrcode.make(uri,image_factory=qrcode.image.svg.SvgPathImage)
            buf=io.BytesIO();image.save(buf)
        return base64.b64encode(buf.getvalue()).decode('ascii')

    def ports(self, token):
        self.c.auth.validate(token)
        out=[]
        try:data=self._configuration()
        except (OSError,AuthError):data={'inbounds':[]}
        for i in data['inbounds']:
            if isinstance(i,dict) and type(i.get('port')) is int and 1<=i['port']<=65535:
                out.append({'id':self.node_id(i),'name':i.get('tag') or '用户入站','port':i['port'],
                            'editable':bool(i.get('tag') in self.owned()),'service':'xray'})
        if self.port is not None:out.append({'id':'management','name':'统一管理面板','port':self.port,'editable':False,'service':'management','reason':'统一监听端口变更需部署事务，当前不支持在线修改'})
        return out

    def owned(self):
        from auth import loads
        raw=self.store.read('xray-owned.json',missing=True)
        return loads(raw) if raw else []

    def modules(self, token):
        self.c.auth.validate(token)
        state=self.c.active.snapshot();mods=[]
        try:data=self._configuration();inbounds=data['inbounds']
        except (OSError,AuthError):inbounds=[]
        kinds={'ws':False,'xhttp':False,'reality':False}
        valid_kinds={n['type'] for n in self.nodes(token)}
        for i in inbounds:
            if not isinstance(i,dict):continue
            ss=i.get('streamSettings') or {}
            net=ss.get('network');security=ss.get('security','none')
            if net=='ws' and security!='reality':kinds['ws']=True
            if net=='xhttp' and security!='reality':kinds['xhttp']=True
            if net in ('raw','tcp') and security=='reality':kinds['reality']=True
        from auth import loads
        disabled_raw=self.store.read('disabled-inbounds.json',missing=True)
        disabled=loads(disabled_raw) if disabled_raw else {}
        for kind,label in [('ws','WS + TLS'),('xhttp','XHTTP'),('reality','REALITY (RAW/TCP)')]:
            unit=state['services'].get('xray',{});configured=kinds[kind] and kind in valid_kinds
            saved={'ws':'vps-monitor-ws','xhttp':'vps-monitor-xhttp','reality':'vps-monitor-reality'}[kind] in disabled
            st='未安装' if not unit.get('installed') else '已停止' if saved and not configured else '配置错误' if kinds[kind] and not configured else '已安装但未配置' if not configured else '正在运行' if unit.get('active') else '已配置但未启动'
            mods.append({'id':kind,'name':label,'state':st,'active':bool(configured and unit.get('active')),'visible':bool((configured or saved) and unit.get('installed')),'configured':configured,'installed':bool(unit.get('installed'))})
        cloud=state['services'].get('cloudflared',{});configured=False
        if self.c.discovery and self.c.discovery.cloud_config:
            path=self.c.discovery.cloud_config;configured=path.is_file() and not path.is_symlink() and path.stat().st_size>0
        mods.append({'id':'cloudflared','name':'Cloudflared','installed':bool(cloud.get('installed')),'configured':configured,'visible':bool(cloud.get('installed') and configured),'active':bool(cloud.get('active')),'state':'未安装' if not cloud.get('installed') else '已安装但未配置' if not configured else '正在运行' if cloud.get('active') else '已停止'})
        installed=(Path(__file__).with_name('panel.py')).is_file()
        mods.append({'id':'panel','name':'详细面板','installed':installed,'configured':installed,'visible':installed,'active':installed,'state':'正在运行' if installed else '未安装'})
        return {'modules':mods,'heartbeat':state['heartbeat']}

    def guide(self, token):
        self.c.auth.validate(token)
        cfg=self.metadata();cfg.update(self.preferences(token).get('node_metadata',{}));ws=cfg.get('ws') or {}
        return {'title':'Cloudflare Tunnel 需要你自行配置',
                'ws_rule':str(ws.get('host') or '$WS_HOST')+' -> HTTP -> 127.0.0.1:'+str(ws.get('listen_port') or '$WS_PORT'),
                'panel_rule':'面板域名 -> HTTP -> 127.0.0.1:'+str(self.port or '$PANEL_PORT'),
                'note':'请在 Cloudflare Zero Trust 控制台自行配置；程序不会修改 Tunnel、DNS 或账户。'}

    def detail(self, token, action, headers):
        import panel
        panel.authorize(self.c,token)
        if action=='visitor':
            class Request:pass
            req=Request();req.headers=headers
            with self.c.gate('diagnostics'):return panel.get_visitor_info(req)
        if action=='serverdiag':
            with self.c.gate('diagnostics'):return panel.get_server_diagnostics()
        if action=='status':
            state=self.c.active.snapshot();nodes=self.nodes(token)
            mem={}
            for line in Path('/proc/meminfo').read_text().splitlines():
                k,v=line.split(':',1);mem[k]=int(v.split()[0])
            total=mem.get('MemTotal',0);used=max(0,total-mem.get('MemAvailable',0))
            return {'xray':state['services'].get('xray',{}).get('active',False),'tunnel':state['services'].get('cloudflared',{}).get('active',False),
                    'node_a':any(n['type']=='ws' and panel.port_active(n['listen_port']) for n in nodes),
                    'node_b':any(n['type']=='reality' and panel.port_active(n['listen_port']) for n in nodes),
                    'reality_enabled':any(n['type']=='reality' for n in nodes),
                    'memory_used':str(round(used/1024))+' MiB','memory_total':str(round(total/1024))+' MiB','memory_percent':round(used*100/total) if total else 0}
        raise AuthError('unknown detailed operation')

    def _permits(self):
        if not hasattr(self,'permits'):self.permits={};self.permit_lock=threading.Lock()
        return self.permits

    def _permit(self, token, payload):
        import secrets,hashlib
        self._permits()
        with self.permit_lock:
            now=time.monotonic();self.permits={k:v for k,v in self.permits.items() if v['expires']>now}
            if len(self.permits)>=16:raise Busy('confirmation capacity reached')
            ident=secrets.token_hex(32);payload.update(epoch=self.c.auth.validate(token),session=hashlib.sha256(token.encode()).hexdigest(),expires=now+120)
            import copy
            self.permits[ident]=copy.deepcopy(payload);return ident

    def _consume(self, token, ident):
        import hashlib
        self._permits()
        with self.permit_lock:r=self.permits.pop(ident,None)
        if not r or r['expires']<=time.monotonic() or r['epoch']!=self.c.auth.validate(token) or r['session']!=hashlib.sha256(token.encode()).hexdigest():raise AuthError('expired or mismatched confirmation')
        return r

    def prepare_service(self, token, ident, enabled):
        from auth import loads,dumps
        if type(enabled) is not bool:raise AuthError('invalid switch value')
        modules=self.modules(token)['modules']
        selected=[m for m in modules if (m['id']==ident or ident=='all') and m['visible'] and m['id']!='panel']
        # Previously disabled owned inbounds have valid saved definitions.
        saved_raw=self.store.read('disabled-inbounds.json',missing=True)
        saved=loads(saved_raw) if saved_raw else {}
        if enabled:
            for k,tag in [('ws','vps-monitor-ws'),('xhttp','vps-monitor-xhttp'),('reality','vps-monitor-reality')]:
                if (ident in (k,'all')) and tag in saved and not any(m['id']==k for m in selected):
                    selected.append({'id':k,'name':k,'active':False,'visible':True})
        if not selected:raise AuthError('no configured services selected')
        xray_selected=[m for m in selected if m['id'] in ('ws','xhttp','reality')]
        cloud_selected=[m for m in selected if m['id']=='cloudflared']
        if xray_selected:
            data=self._configuration();bytag={i.get('tag'):i for i in data['inbounds'] if i.get('tag')}
            replacements={};new_saved=dict(saved)
            for m in xray_selected:
                tag={'ws':'vps-monitor-ws','xhttp':'vps-monitor-xhttp','reality':'vps-monitor-reality'}[m['id']]
                if tag not in self.owned():raise AuthError('该入站为用户配置，不能自动接管或停止共享 Xray')
                if enabled:
                    if tag in bytag:continue
                    if tag not in saved:raise AuthError('没有已验证的停用入站配置')
                    replacements[tag]=saved[tag];new_saved.pop(tag,None)
                elif tag in bytag:
                    replacements[tag]=None;new_saved[tag]=bytag[tag]
            if replacements:
                # Save definitions before disabling; they are inert, never written
                # into Xray without a separately validated confirmed transaction.
                preview=self.preview_mapping(token,replacements)
                with self.permit_lock:
                    self.permits[preview['confirmation']]['cloud_after']=enabled if cloud_selected else None
                    self.permits[preview['confirmation']]['disabled_after']=new_saved
                return preview
        permit=self._permit(token,{'op':'service','services':[m['id'] for m in cloud_selected],'enabled':enabled})
        return {'confirmation':permit,'warning':'确认'+('启动' if enabled else '停止')+' '+', '.join(m['name'] for m in selected)+'？停止会中断相应连接。未选择的用户入站保持不变。'}

    def apply_service(self, token, ident):
        r=self._consume(token,ident)
        if r['op']=='config':
            result=self._apply_record(token,r)
            if r.get('cloud_after') is not None:
                try:self.c.active.set_service('cloudflared',r['cloud_after'])
                except Exception as exc:
                    raise AuthError('Xray 已提交，但 Cloudflared 切换失败；请刷新查看真实状态，不要盲目重试') from exc
            return result
        if r['op']!='service':raise AuthError('confirmation type mismatch')
        return {'results':[self.c.active.set_service(name,r['enabled']) for name in r['services']]}

    def schema(self, token, kind):
        self.c.auth.validate(token)
        if kind not in ('ws','xhttp','reality'):raise AuthError('unsupported protocol')
        fields=[{'name':'port','label':'监听端口','required':True,'help':'1–65535，由你指定，无默认值'},
                {'name':'listen','label':'监听地址','required':True,'help':'填写明确的 IPv4/IPv6 地址'},
                {'name':'uuid','label':'客户端 UUID','required':True,'help':'填写现有或自行生成的有效 UUID'}]
        if kind in ('ws','xhttp'):
            fields.extend([{'name':'path','label':'传输路径','required':True,'help':'以 / 开头，禁止空白'},
                           {'name':'security','label':'源站安全类型','required':True,'help':'none 或 tls；Cloudflare 边缘 TLS 不等于源站 TLS'},
                           {'name':'certificate','label':'TLS 证书文件','required':False,'help':'tls 时必填，服务器现有文件路径'},
                           {'name':'keyfile','label':'TLS 私钥文件','required':False,'help':'tls 时必填，不上传文件内容'}])
        if kind=='xhttp':fields.append({'name':'mode','label':'XHTTP mode','required':True,'help':'auto / packet-up / stream-up / stream-one；由当前 Xray 自测验证'})
        if kind=='reality':
            fields.extend([{'name':'target','label':'REALITY target','required':True,'help':'明确的 host:port'},
                           {'name':'sni','label':'serverName','required':True,'help':'与你的 target 相容的域名'},
                           {'name':'private_key','label':'REALITY 私钥','required':True,'secret':True,'help':'当前 Xray 支持的 X25519 私钥，仅用于候选配置'},
                           {'name':'short_id','label':'Short ID','required':True,'help':'偶数长度十六进制，最多16字符'}])
        fields.extend([{'name':'public_host','label':'客户端连接地址','required':True,'help':'真实公网域名/IP，不从监听地址推断'},
                       {'name':'public_port','label':'客户端连接端口','required':True,'help':'真实入口端口，不从源站端口推断'}])
        if kind=='reality':fields.append({'name':'public_key','label':'REALITY 公钥','required':True,'help':'必须与填写私钥匹配，客户端使用；不会猜测公钥'})
        for f in fields:f.setdefault('value','')
        return {'title':{'ws':'VLESS + WebSocket','xhttp':'VLESS + XHTTP','reality':'VLESS + REALITY（RAW/TCP）'}[kind],
                'description':'不支持 WS + REALITY 组合。参数无自动猜测；必须在当前 Xray 版本自测成功后才可确认应用。现有非本项目入站不会接管。','fields':fields}

    def _inbound(self, kind, v):
        import ipaddress,uuid,re
        if not isinstance(v,dict):raise AuthError('parameter object required')
        try:
            port=int(v['port']);listen=str(ipaddress.ip_address(v['listen']));uid=str(uuid.UUID(v['uuid']))
        except (ValueError,TypeError,KeyError):raise AuthError('invalid port/listen/UUID') from None
        if not 1<=port<=65535:raise AuthError('port outside 1..65535')
        tag={'ws':'vps-monitor-ws','xhttp':'vps-monitor-xhttp','reality':'vps-monitor-reality'}.get(kind)
        if not tag:raise AuthError('unsupported protocol')
        stream={'network':'ws' if kind=='ws' else 'xhttp' if kind=='xhttp' else 'raw'}
        if kind in ('ws','xhttp'):
            path=v.get('path','');security=v.get('security')
            if not path.startswith('/') or len(path)>512 or any(c.isspace() for c in path):raise AuthError('invalid transport path')
            if security not in ('none','tls'):raise AuthError('unsupported security; WS/XHTTP REALITY combinations not generated')
            stream['security']=security
            settings={'path':path}
            if kind=='xhttp':
                if v.get('mode') not in ('auto','packet-up','stream-up','stream-one'):raise AuthError('invalid XHTTP mode')
                settings['mode']=v['mode']
            stream['wsSettings' if kind=='ws' else 'xhttpSettings']=settings
            if security=='tls':
                for f in ('certificate','keyfile'):
                    p=Path(v.get(f,''))
                    if not p.is_absolute() or p.is_symlink() or not p.is_file():raise AuthError('existing regular TLS files required')
                stream['tlsSettings']={'certificates':[{'certificateFile':v['certificate'],'keyFile':v['keyfile']}]}
        else:
            if not re.fullmatch(r'[A-Za-z0-9.-]+:[0-9]{1,5}',v.get('target','')):raise AuthError('explicit host:port target required')
            if not 1<=int(v['target'].rsplit(':',1)[1])<=65535:raise AuthError('invalid target port')
            if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}',v.get('sni','')):raise AuthError('serverName required')
            key=v.get('private_key','');short=v.get('short_id','')
            if not re.fullmatch(r'[A-Za-z0-9_-]{43}',key) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}){1,8}',short):raise AuthError('invalid REALITY key or short ID')
            stream.update(security='reality',realitySettings={'target':v['target'],'serverNames':[v['sni']],'privateKey':key,'shortIds':[short]})
        return {'tag':tag,'listen':listen,'port':port,'protocol':'vless','settings':{'clients':[{'id':uid}],'decryption':'none'},'streamSettings':stream}

    def preview(self, token, kind, values):
        import re,ipaddress
        inbound=self._inbound(kind,values)
        host=values.get('public_host','')
        try:ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}',host):raise AuthError('真实客户端连接域名/IP 必填')
        try:port=int(values.get('public_port',''))
        except (ValueError,TypeError):raise AuthError('客户端连接端口必填') from None
        if not 1<=port<=65535:raise AuthError('客户端端口范围错误')
        metadata={'host':host,'port':port,'listen_port':inbound['port']}
        if kind=='reality':
            if not re.fullmatch(r'[A-Za-z0-9_-]{43}',values.get('public_key','')):raise AuthError('REALITY 公钥格式错误')
            # Derive the public key locally using already-required cryptography,
            # never spawn xray x25519 with private key argv.
            import base64
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
            from cryptography.hazmat.primitives import serialization
            raw=base64.urlsafe_b64decode(values['private_key']+'=')
            derived=X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
            expected=base64.urlsafe_b64encode(derived).decode().rstrip('=')
            if expected!=values['public_key']:raise AuthError('REALITY 公私钥不匹配')
            metadata.update(address=host,public_key=expected)
        preview=self.preview_mapping(token,{inbound['tag']:inbound})
        with self.permit_lock:self.permits[preview['confirmation']]['node_metadata']={kind:metadata}
        return preview

    def preview_mapping(self, token, replacements):
        from auth import dumps
        import copy,hashlib
        self.c.auth.validate(token)
        x=self.c.xray
        if x is None:raise AuthError('Xray task isolation/test backend not configured')
        if getattr(x,'single_writer',False) is not True:raise AuthError('single-writer authority not explicitly configured')
        with x.mutex,self.store.transaction():
            old,_=x._read();data=__import__('auth').loads(old);owned=self.owned()
            pending=x.pending()
            if pending and pending['stage'] not in x.TERMINAL:raise AuthError('unfinished transaction blocks new candidate')
            present={i.get('tag'):i for i in data['inbounds'] if isinstance(i,dict) and i.get('tag')}
            for tag in replacements:
                if tag in present and tag not in owned:raise AuthError('existing inbound is not project-owned')
            candidate=copy.deepcopy(data)
            candidate['inbounds']=[replacements.get(i.get('tag'),i) for i in data['inbounds'] if i.get('tag') not in replacements or replacements[i.get('tag')] is not None]
            candidate['inbounds'].extend(v for k,v in replacements.items() if k not in present and v is not None)
            ports=[i.get('port') for i in candidate['inbounds']]
            if len(ports)!=len(set(ports)):raise AuthError('inbound port conflict')
            for tag,inbound in replacements.items():
                if inbound and (tag not in present or inbound['port']!=present[tag]['port']):self.check_port(inbound['port'],inbound.get('listen','127.0.0.1'))
            raw=dumps(candidate)
            with self.c.gate('xray-test'):x.test(raw)
            if x.fingerprint(x._read()[0])!=x.fingerprint(old):raise AuthError('external change during preview')
            summary=[]
            for tag,new in replacements.items():
                before=present.get(tag)
                summary.append(tag+': '+str(before.get('port') if before else '不存在')+' -> '+str(new.get('port') if new else '关闭'))
            # Candidate bytes remain server-side; UI diff never prints private keys.
            permit=self._permit(token,{'op':'config','hash':x.fingerprint(old),'candidate_hash':x.fingerprint(raw),'replacements':replacements})
            return {'confirmation':permit,'diff':'\n'.join(summary),'candidate_hash':x.fingerprint(raw),'warning':'候选已通过 Xray 自测。应用会启动/重启 Xray，可能中断现有连接；此确认仅用于本候选，120秒有效。'}

    @staticmethod
    def check_port(port,listen):
        import socket,ipaddress
        if type(port) is not int or not 1<=port<=65535:raise AuthError('invalid port')
        ip=ipaddress.ip_address(listen)
        s=socket.socket(socket.AF_INET6 if ip.version==6 else socket.AF_INET,socket.SOCK_STREAM)
        try:s.bind((str(ip),port))
        except OSError:raise AuthError('port is occupied or bind address unavailable') from None
        finally:s.close()

    def port_preview(self, token, ident, value):
        import copy
        try:port=int(value)
        except (ValueError,TypeError):raise AuthError('invalid port') from None
        data=self._configuration()
        for inbound in data['inbounds']:
            if self.node_id(inbound)==ident:
                tag=inbound.get('tag')
                if tag not in self.owned():raise AuthError('user-owned inbound cannot be changed')
                updated=copy.deepcopy(inbound);updated['port']=port
                return self.preview_mapping(token,{tag:updated})
        raise AuthError('port not found')

    def apply_candidate(self, token, ident):
        r=self._consume(token,ident)
        if r['op']!='config':raise AuthError('confirmation type mismatch')
        return self._apply_record(token,r)

    def _apply_record(self, token, r):
        x=self.c.xray
        if x is None or getattr(x,'single_writer',False) is not True:raise AuthError('single-writer Xray backend unavailable')
        if x.fingerprint(x._read()[0])!=r['hash']:raise AuthError('config changed after preview')
        permit=x.confirm(token)
        result=x.configure(token,permit['confirmation'],r['replacements'])
        if hasattr(self.c.active,'desired'):
            with self.c.active.lock:self.c.active.desired['xray']=True
        if 'node_metadata' in r:
            from auth import dumps
            prefs=self.preferences(token);prefs.setdefault('node_metadata',{}).update(r['node_metadata'])
            self.store.write('panel-preferences.json',dumps(prefs))
        if 'disabled_after' in r:
            from auth import dumps
            self.store.write('disabled-inbounds.json',dumps(r['disabled_after']))
        return result


class ModuleInstaller:
    """Built-in official release sources; no remotely downloaded shell execution.

    Release asset must have GitHub's sha256 digest; releases lacking it are refused.
    Programs install without rewriting existing files, unit or user configuration.
    Missing unit/config remains visible as incomplete, never reported ready.
    """
    SOURCES={'xray':('XTLS/Xray-core','Xray-linux-64.zip','Xray-linux-arm64-v8a.zip'),
             'cloudflared':('cloudflare/cloudflared','cloudflared-linux-amd64','cloudflared-linux-arm64')}
    PANEL_SHA256='214fcbc66f104aa7fbe6caea2f4e3c7ff6f9ee5c00134dcf9eb447daaebba771'
    LIMIT=50*1024*1024

    def __init__(self, backend, runner, program_dir=Path('/usr/local/bin')):
        self.backend,self.c,self.runner=backend,backend.c,runner
        self.program_dir=Path(program_dir)

    def _download(self,url,destination,limit):
        import urllib.request,urllib.parse
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme!='https' or parsed.hostname not in ('api.github.com','github.com','raw.githubusercontent.com'):
            raise AuthError('untrusted module source')
        req=urllib.request.Request(url,headers={'User-Agent':'vps-monitor-lite/1.0','Accept':'application/vnd.github+json'})
        # urllib redirect targets must also be HTTPS; GitHub release assets use
        # release-assets.githubusercontent.com, never arbitrary redirect origins.
        class Redirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,req,fp,code,msg,headers,newurl):
                target=urllib.parse.urlsplit(newurl)
                if target.scheme!='https' or target.hostname not in ('github.com','api.github.com','raw.githubusercontent.com','release-assets.githubusercontent.com','objects.githubusercontent.com'):
                    raise AuthError('untrusted download redirect')
                return super().redirect_request(req,fp,code,msg,headers,newurl)
        opener=urllib.request.build_opener(Redirect())
        total=0;deadline=time.monotonic()+90
        with opener.open(req,timeout=10) as response,open(destination,'xb') as output:
            os.chmod(destination,0o600)
            while True:
                self.c.gate.checkpoint()
                if time.monotonic()>deadline:raise Busy('module download timeout')
                raw=response.read(min(65536,limit-total+1))
                if not raw:break
                total+=len(raw)
                if total>limit:raise AuthError('module download size limit')
                output.write(raw)
            output.flush();os.fsync(output.fileno())
        if total==0:raise AuthError('empty module download')

    def prepare(self,token,name):
        import platform,tempfile,hashlib
        from auth import loads
        self.c.auth.validate(token)
        if name in ('ws','xhttp','reality'):name='xray'
        if name not in (*self.SOURCES,'panel'):raise AuthError('unsupported module')
        if name=='panel':
            # Match the reviewed integration revision, never fetch mutable main.
            release={'name':name,'url':'https://raw.githubusercontent.com/universeglitch/vps-monitor-lite/b090bc233945d7c436c1e692b72265b139488478/panel.py',
                     'digest':self.PANEL_SHA256,'asset':'panel.py'}
        else:
            arch=platform.machine()
            if arch not in ('x86_64','aarch64'):raise AuthError('unsupported CPU architecture')
            repo,amd,arm=self.SOURCES[name];asset=amd if arch=='x86_64' else arm
            with self.c.gate('module-metadata'),tempfile.TemporaryDirectory(prefix='vps-module-meta-') as tmp:
                path=Path(tmp)/'release.json';self._download('https://api.github.com/repos/'+repo+'/releases/latest',path,262144)
                metadata=loads(path.read_bytes())
            matches=[a for a in metadata.get('assets',[]) if a.get('name')==asset]
            if len(matches)!=1:raise AuthError('official release asset missing')
            a=matches[0];digest=a.get('digest','')
            if not __import__('re').fullmatch('sha256:[0-9a-f]{64}',digest):raise AuthError('official release lacks verifiable SHA256 digest')
            if type(a.get('size')) is not int or not 0<a['size']<=self.LIMIT:raise AuthError('release size unsupported')
            release={'name':name,'url':a['browser_download_url'],'digest':digest[7:],'asset':asset,'version':metadata.get('tag_name')}
        permit=self.backend._permit(token,{'op':'install','release':release})
        return {'confirmation':permit,'missing':['module components'],'source':release['url'],'version':release.get('version'),'sha256':release['digest'],
                'warning':'只安装已校验模块，不覆盖配置或旧服务；缺少 systemd unit/配置时仍需完成配置，安装不会自动启动代理。'}

    def execute(self,token,confirmation):
        import tempfile,hashlib,zipfile,shutil,stat
        r=self.backend._consume(token,confirmation)
        if r['op']!='install':raise AuthError('confirmation type mismatch')
        release=r['release'];name=release['name']
        with self.c.gate('install-'+name),tempfile.TemporaryDirectory(prefix='vps-module-') as tmp:
            if shutil.disk_usage(self.program_dir if name!='panel' else Path(__file__).parent).free < self.LIMIT*3:
                raise Busy('insufficient staging/rollback disk margin')
            archive=Path(tmp)/'asset';self._download(release['url'],archive,self.LIMIT if name!='panel' else 262144)
            with open(archive,'rb') as downloaded:
                digest=hashlib.file_digest(downloaded,'sha256').hexdigest()
            if digest!=release['digest']:raise AuthError('module SHA256 mismatch')
            source=archive
            if name=='xray':
                with zipfile.ZipFile(archive) as z:
                    entries=[i for i in z.infolist() if i.filename=='xray']
                    if len(entries)!=1 or not 0<entries[0].file_size<=self.LIMIT:raise AuthError('invalid Xray archive')
                    item=entries[0]
                    if stat.S_ISLNK(item.external_attr>>16):raise AuthError('archive link rejected')
                    source=Path(tmp)/'binary'
                    with z.open(item) as inp,open(source,'xb') as out:
                        size=0
                        while True:
                            raw=inp.read(65536)
                            if not raw:break
                            size+=len(raw)
                            if size>self.LIMIT:raise AuthError('extracted size limit')
                            self.c.gate.checkpoint();out.write(raw)
                        out.flush();os.fsync(out.fileno())
            if name=='panel':
                compile(source.read_text(), 'downloaded-panel.py','exec')
                target=Path(__file__).with_name('panel.py')
            else:
                os.chmod(source,0o700)
                self.runner.run([str(source),'version' if name=='xray' else '--version'],timeout=15)
                target=self.program_dir/name
            # Never clobber an existing binary, config or currently imported module.
            if target.exists() or target.is_symlink():raise AuthError('module path exists; refusing overwrite')
            fd,staged=tempfile.mkstemp(prefix='.vps-module-',dir=target.parent)
            try:
                with os.fdopen(fd,'wb',closefd=False) as out,open(source,'rb') as inp:
                    shutil.copyfileobj(inp,out,65536);out.flush();os.fchmod(fd,0o644 if name=='panel' else 0o755);os.fsync(fd)
                os.link(staged,target,follow_symlinks=False)
                d=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                try:os.fsync(d)
                finally:os.close(d)
            finally:
                os.close(fd);os.unlink(staged)
            if name=='panel':return {'program_installed':True,'restart_management_required':True,'ready':False}
            # Actual version test + presence checks; missing units/config are not success.
            check=self.c.discovery.inspect(name)
            return {'program_installed':True,'ready':False,'discovery':check,'next':'配置/unit 就绪及启动验证后才可出现在正常服务列表'}
