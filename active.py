"""Bounded service state and self-healing. Never writes Xray configurations.

No import-time thread. start_monitor() must be explicit. Heartbeat off leaves
running services untouched. Only fixed unit names are passed to systemctl.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections import deque


class ServiceError(Exception):
    pass


UNITS = {'xray': 'xray.service', 'cloudflared': 'cloudflared.service'}


def systemctl(*args):
    command = shutil.which('systemctl')
    if command is None:
        raise ServiceError('systemctl unavailable')
    import selectors
    import os
    p = subprocess.Popen([command, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    selector=selectors.DefaultSelector(); chunks={p.stdout:bytearray(), p.stderr:bytearray()}
    for stream in chunks: selector.register(stream, selectors.EVENT_READ)
    deadline=time.monotonic()+10
    try:
        while selector.get_map():
            remaining=deadline-time.monotonic()
            if remaining<=0: raise ServiceError('service operation timed out')
            for key,_ in selector.select(min(remaining,0.2)):
                data=os.read(key.fileobj.fileno(),4096)
                if not data: selector.unregister(key.fileobj);continue
                chunks[key.fileobj].extend(data)
                if len(chunks[key.fileobj])>32768: raise ServiceError('service output exceeded limit')
        return p.wait(timeout=max(.01,deadline-time.monotonic())), chunks[p.stdout].decode(errors='replace')
    finally:
        if p.poll() is None: p.kill()
        p.wait();selector.close();p.stdout.close();p.stderr.close()



class Active:
    def __init__(self, run=systemctl, clock=time.monotonic, readiness=None):
        self.run, self.clock = run, clock
        self.readiness = readiness
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.heartbeat = False
        # Restart does not infer intent from a previous process's stale memory.
        self.desired = {k: False for k in UNITS}
        self.failures = {k: 0 for k in UNITS}
        self.attempts = {k: deque(maxlen=3) for k in UNITS}
        self.events = deque(maxlen=64)

    def inspect(self, name):
        if name not in UNITS:
            raise ServiceError('unsupported service')
        rc, output = self.run('show', UNITS[name], '--property=LoadState,ActiveState,SubState,Restart', '--no-pager')
        if rc:
            raise ServiceError('service query failed')
        fields = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        return {'service': name, 'installed': fields.get('LoadState') == 'loaded',
                'active': fields.get('ActiveState') == 'active',
                'state': fields.get('ActiveState', 'unknown'),
                'substate': fields.get('SubState', 'unknown'),
                'restart': fields.get('Restart', 'unknown')}

    def snapshot(self):
        out = {}
        for name in UNITS:
            try:
                out[name] = self.inspect(name)
            except ServiceError as exc:
                out[name] = {'service': name, 'state': 'unknown', 'error': str(exc)}
        with self.lock:
            return {'services': out, 'heartbeat': self.heartbeat, 'desired': dict(self.desired)}

    def set_service(self, name, enabled):
        if name not in UNITS or type(enabled) is not bool:
            raise ServiceError('invalid service operation')
        # Manual operations serialize with recovery. No automatic installation.
        with self.lock:
            current = self.inspect(name)
            if not current['installed']:
                raise ServiceError('service unit missing; installation confirmation required')
            if enabled:
                if self.readiness is None:
                    raise ServiceError('start refused: readiness validator unavailable')
                self.readiness(name)
                # Configuration discovery/validation belongs to Coordinator; this
                # low-level method must only receive a prevalidated start request.
                rc, _ = self.run('start', UNITS[name])
            else:
                self.desired[name] = False
                rc, _ = self.run('stop', UNITS[name])
            after = self.inspect(name)
            if rc or after['active'] != enabled:
                raise ServiceError('actual service state did not reach requested state')
            self.desired[name] = enabled
            self.failures[name] = 0
            return after

    def set_heartbeat(self, enabled):
        if type(enabled) is not bool:
            raise ServiceError('invalid heartbeat state')
        with self.lock:
            self.heartbeat = enabled
            for name in UNITS:
                self.failures[name] = 0
        # Deliberately no stop/start calls.

    def tick(self):
        with self.lock:
            if not self.heartbeat:
                return
            for name in UNITS:
                if not self.desired[name]:
                    continue
                try:
                    state = self.inspect(name)
                    if not state['installed']:
                        self.events.append((self.clock(), name, 'unit missing; no recovery'))
                        continue
                    if state['active']:
                        self.failures[name] = 0
                        continue
                    self.failures[name] += 1
                    if self.failures[name] < 2:
                        continue
                    # Never compete with systemd's configured Restart policy.
                    if state['restart'] != 'no':
                        self.events.append((self.clock(), name, 'systemd owns restart policy'))
                        continue
                    now = self.clock(); attempts = self.attempts[name]
                    while attempts and now - attempts[0] >= 600:
                        attempts.popleft()
                    if len(attempts) >= 3 or (attempts and now - attempts[-1] < 60 * 2**(len(attempts)-1)):
                        continue
                    if self.readiness is None:
                        self.events.append((now, name, 'readiness unavailable; no speculative recovery'))
                        continue
                    self.readiness(name)
                    attempts.append(now)
                    rc, _ = self.run('start', UNITS[name])
                    self.events.append((now, name, 'recovery attempted' if rc == 0 else 'recovery failed'))
                except ServiceError:
                    self.events.append((self.clock(), name, 'query failure; no speculative restart'))

    def start_monitor(self):
        if self.thread is not None:
            raise ServiceError('monitor already started')
        def loop():
            while not self.stop_event.wait(60):
                self.tick()
        self.thread = threading.Thread(target=loop, name='service-heartbeat', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=12)

    def apply_xray(self, was_active, rollback=False):
        """Only Coordinator's confirmed transaction may call this operation."""
        with self.lock:
            operation = 'stop' if rollback and not was_active else ('restart' if was_active else 'start')
            rc, _ = self.run(operation, UNITS['xray'])
            expected = not (rollback and not was_active)
            if rc or self.inspect('xray')['active'] != expected:
                raise ServiceError('Xray configuration application failed')
