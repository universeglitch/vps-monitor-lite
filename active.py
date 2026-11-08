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
    try:
        p = subprocess.run([command, *args], capture_output=True, text=True, timeout=10,
                           env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    except subprocess.TimeoutExpired as exc:
        raise ServiceError('service operation timed out') from exc
    if len(p.stdout) > 65536 or len(p.stderr) > 65536:
        raise ServiceError('service output exceeded limit')
    return p.returncode, p.stdout


class Active:
    def __init__(self, run=systemctl, clock=time.monotonic):
        self.run, self.clock = run, clock
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
