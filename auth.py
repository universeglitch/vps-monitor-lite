"""V1 authentication vault; no import-time I/O, SQL, network or service actions.

The transport must enforce trusted bootstrap access, CSRF and secure cookies.
Expensive methods require an injected resource gate (absent = deny). API records
are immutable encrypted files; auth.json is the sole authoritative commit point.
Backups are bounded (not unbounded in-memory archives); no Sessions are exported.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager

from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import VerificationError
from argon2.low_level import hash_secret_raw
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_JSON = 262144
MAX_ENVELOPE = 1048576
MAX_APIS = 64
MAX_SESSIONS = 32
TTL = 86400
PH = PasswordHasher(time_cost=3, memory_cost=8192, parallelism=1,
                    hash_len=32, salt_len=16, type=Type.ID)
IDENT = re.compile(r'[0-9a-f]{32}\Z')
HASH_GATE = threading.Lock()


class AuthError(Exception):
    pass


class Busy(AuthError):
    pass


class Corrupt(AuthError):
    pass


class UncertainCommit(AuthError):
    """A rename may have occurred; instance is poisoned until restart/recovery."""


class BackupPending(AuthError):
    """The authentication revision committed, but its event backup did not."""


def _pairs(items):
    out = {}
    for k, v in items:
        if k in out:
            raise Corrupt('duplicate field')
        out[k] = v
    return out


def dumps(value, limit=MAX_JSON):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > limit:
        raise Corrupt('record size limit')
    return raw


def loads(raw, limit=MAX_JSON):
    if len(raw) > limit:
        raise Corrupt('record size limit')
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(Corrupt('nonfinite JSON')))
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise Corrupt('invalid JSON') from exc
    return value


def b64(raw):
    return base64.b64encode(raw).decode('ascii')


def unb64(text, length=None):
    try:
        if not isinstance(text, str) or len(text) > MAX_ENVELOPE:
            raise ValueError()
        raw = base64.b64decode(text, validate=True)
        if length is not None and len(raw) != length:
            raise ValueError()
        return raw
    except ValueError as exc:
        raise Corrupt('invalid encoding') from exc


def seal(key, raw, aad):
    nonce = secrets.token_bytes(12)
    return {'n': b64(nonce), 'c': b64(AESGCM(key).encrypt(nonce, raw, aad))}


def open_seal(key, record, aad):
    try:
        return AESGCM(key).decrypt(unb64(record['n'], 12), unb64(record['c']), aad)
    except Exception as exc:
        raise Corrupt('encrypted record authentication failed') from exc


def password(value):
    if not isinstance(value, str) or not 16 <= len(value) <= 1024 or len(value.encode()) > 4096:
        raise AuthError('password length must be 16..1024 characters, at most 4096 bytes')
    return value


def validate_phc(value):
    try:
        p = extract_parameters(value)
        if (p.type != Type.ID or p.version != 19 or p.memory_cost != 8192 or
                p.time_cost != 3 or p.parallelism != 1 or p.hash_len != 32 or p.salt_len != 16):
            raise ValueError()
    except Exception as exc:
        raise Corrupt('unsupported PHC parameters') from exc


class Store:
    """Private pinned directory, stable flock inode, bounded no-follow files.

    Parent directories must be trusted. Symlink ancestors are always rejected;
    group/world writable ancestors are rejected except sticky temporary parents.
    A post-rename fsync failure poisons this instance to prevent stale decisions.
    """
    def __init__(self, path):
        self.lock = threading.RLock()
        self.poisoned = False
        self.fd = self.lockfd = None
        root = os.path.abspath(os.fspath(path))
        parent, name = os.path.split(root)
        if not name:
            raise Corrupt('data root cannot be filesystem root')
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parent.strip('/').split('/'):
                if not part:
                    continue
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd); fd = new
                s = os.fstat(fd)
                if s.st_mode & 0o022 and not s.st_mode & stat.S_ISVTX:
                    raise Corrupt('unsafe writable ancestor')
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
                os.fsync(fd)
            except FileExistsError:
                pass
            self.fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            self._directory(self.fd)
            self.lockfd = os.open('lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                                  0o600, dir_fd=self.fd)
            self._file(self.lockfd)
            os.fsync(self.fd)
        except Exception:
            self.close()
            raise
        finally:
            os.close(fd)

    @staticmethod
    def _directory(fd):
        s = os.fstat(fd)
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.geteuid() or stat.S_IMODE(s.st_mode) != 0o700:
            raise Corrupt('unsafe private directory')

    @staticmethod
    def _file(fd):
        s = os.fstat(fd)
        if (not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid() or
                stat.S_IMODE(s.st_mode) != 0o600 or s.st_nlink != 1):
            raise Corrupt('unsafe private file')

    def close(self):
        for attr in ('lockfd', 'fd'):
            fd = getattr(self, attr, None)
            if fd is not None:
                os.close(fd); setattr(self, attr, None)

    @contextmanager
    def transaction(self):
        with self.lock:
            if self.poisoned:
                raise UncertainCommit('storage requires restart and verification')
            fcntl.flock(self.lockfd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(self.lockfd, fcntl.LOCK_UN)

    @contextmanager
    def directory(self, area):
        if area not in ('', 'master', 'api', 'backup'):
            raise Corrupt('invalid storage area')
        if not area:
            yield self.fd
            return
        try:
            os.mkdir(area, 0o700, dir_fd=self.fd)
            os.fsync(self.fd)
        except FileExistsError:
            pass
        fd = os.open(area, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            self._directory(fd)
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def name(name):
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,140}', name):
            raise Corrupt('invalid internal path')

    def read(self, name, area='', missing=False):
        self.name(name)
        with self.directory(area) as d:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=d)
            except FileNotFoundError:
                if missing:
                    return None
                raise Corrupt('required record missing') from None
            try:
                self._file(fd)
                if os.fstat(fd).st_size > MAX_ENVELOPE:
                    raise Corrupt('file size limit')
                with os.fdopen(fd, 'rb', closefd=False) as f:
                    raw = f.read(MAX_ENVELOPE + 1)
                if len(raw) > MAX_ENVELOPE:
                    raise Corrupt('file size limit')
                return raw
            finally:
                os.close(fd)

    def write(self, name, raw, area=''):
        self.name(name)
        if len(raw) > MAX_ENVELOPE:
            raise Corrupt('file size limit')
        self.read(name, area, missing=True)
        with self.directory(area) as d:
            tmp = '.tmp-' + secrets.token_hex(16)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=d)
            replaced = False
            try:
                with os.fdopen(fd, 'wb', closefd=False) as f:
                    f.write(raw); f.flush(); os.fsync(fd)
                os.replace(tmp, name, src_dir_fd=d, dst_dir_fd=d)
                replaced = True
                os.fsync(d)
            except Exception as exc:
                if replaced:
                    self.poisoned = True
                    raise UncertainCommit('rename completed but durability uncertain') from exc
                raise
            finally:
                os.close(fd)
                try:
                    os.unlink(tmp, dir_fd=d)
                except FileNotFoundError:
                    pass


class Auth:
    def __init__(self, root, gate=None, clock=time.time):
        self.store = Store(root)
        self.gate = gate
        self.clock = clock
        self.rate_lock = threading.Lock()
        self.rates = OrderedDict()
        self.global_rates = {}

    def close(self):
        self.store.close()

    @contextmanager
    def cost(self, kind):
        if self.gate is None:
            raise Busy('resource admission is not configured')
        with self.gate(kind):
            yield

    def rate(self, source, kind):
        now = time.monotonic()
        with self.rate_lock:
            at, n = self.global_rates.get(kind, (now, 0))
            if now - at >= 60:
                at, n = now, 0
            if n >= 20:
                raise Busy('global authentication limit')
            self.global_rates[kind] = (at, n + 1)
            key = (str(source)[:128], kind)
            at, n = self.rates.get(key, (now, 0))
            if now - at >= 60:
                at, n = now, 0
            if n >= 5:
                raise Busy('source authentication limit')
            self.rates[key] = (at, n + 1)
            self.rates.move_to_end(key)
            while len(self.rates) > 256:
                self.rates.popitem(last=False)

    def hash(self, value):
        password(value)
        if not HASH_GATE.acquire(False):
            raise Busy('Argon2 worker busy')
        try:
            return PH.hash(value)
        finally:
            HASH_GATE.release()

    def verify(self, value, phc):
        password(value); validate_phc(phc)
        if not HASH_GATE.acquire(False):
            raise Busy('Argon2 worker busy')
        try:
            try:
                return PH.verify(phc, value)
            except VerificationError:
                return False
        finally:
            HASH_GATE.release()

    def state(self):
        s = loads(self.store.read('auth.json'))
        self.check_state(s)
        return s

    @staticmethod
    def check_state(s):
        try:
            if not (s['format'] == 1):
                raise Corrupt("invalid state or backup field")
            if not (type(s['epoch']) is int and 1 <= s['epoch'] < 2**63):
                raise Corrupt("invalid state or backup field")
            if not (type(s['revision']) is int and s['revision'] >= 1):
                raise Corrupt("invalid state or backup field")
            if not (type(s['version']) is int and s['version'] >= 1):
                raise Corrupt("invalid state or backup field")
            if not (isinstance(s['apis'], dict) and len(s['apis']) <= MAX_APIS):
                raise Corrupt("invalid state or backup field")
            if not (re.fullmatch('[0-9a-f]{64}', s['recovery'])):
                raise Corrupt("invalid state or backup field")
            validate_phc(s['password'])
            for ident, rec in s['apis'].items():
                if not (IDENT.fullmatch(ident)):
                    raise Corrupt("invalid state or backup field")
                if not (isinstance(rec['name'], str) and 1 <= len(rec['name']) <= 80):
                    raise Corrupt("invalid state or backup field")
                if not (IDENT.fullmatch(rec['object'])):
                    raise Corrupt("invalid state or backup field")
                if not (re.fullmatch('[0-9a-f]{64}', rec['verify'])):
                    raise Corrupt("invalid state or backup field")
        except (KeyError, TypeError, AssertionError) as exc:
            raise Corrupt('invalid authority state') from exc

    def lmk(self):
        state_raw = self.store.read('auth.json', missing=True)
        name = loads(state_raw).get('master_object', 'master.key') if state_raw else 'master.key'
        self.store.name(name)
        key = self.store.read(name, 'master')
        if len(key) != 32:
            raise Corrupt('invalid LMK')
        return key

    def kek(self, s):
        return open_seal(self.lmk(), s['kek'], ('kek/' + str(s['version'])).encode())

    def sessions(self):
        s = loads(self.store.read('sessions.json'))
        if not isinstance(s, dict) or len(s) > MAX_SESSIONS:
            raise Corrupt('invalid Session store')
        return s

    def session(self, s, token):
        if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{64}', token):
            raise AuthError('unauthorized')
        ident = hashlib.sha256(token.encode()).hexdigest()
        rec = self.sessions().get(ident)
        try:
            valid = (rec is not None and rec['epoch'] == s['epoch'] and
                     rec['created'] <= self.clock() < rec['expires'] and
                     rec['expires'] - rec['created'] == TTL)
        except (KeyError, TypeError):
            valid = False
        if not valid:
            raise AuthError('unauthorized')
        return ident

    def validate(self, token):
        with self.store.transaction():
            s = self.state(); self.session(s, token)
            return s['epoch']

    def payload(self, s):
        objects = {}
        for rec in s['apis'].values():
            obj = rec['object']
            objects[obj] = loads(self.store.read('API_' + obj + '.json', 'api'))
        return {'format': 1, 'state': s, 'master': b64(self.lmk()), 'objects': objects}

    @staticmethod
    def check_payload(p):
        try:
            if not (p['format'] == 1):
                raise Corrupt("invalid state or backup field")
            s = p['state']; Auth.check_state(s)
            master = unb64(p['master'], 32)
            kek = open_seal(master, s['kek'], ('kek/' + str(s['version'])).encode())
            if not (len(kek) == 32):
                raise Corrupt("invalid state or backup field")
            if not (set(p['objects']) == {r['object'] for r in s['apis'].values()}):
                raise Corrupt("invalid state or backup field")
            for ident, rec in s['apis'].items():
                aad = ('api/' + ident + '/' + str(rec['version'])).encode()
                obj = p['objects'][rec['object']]
                dek = open_seal(kek, obj['dek'], aad + b'/dek')
                raw = open_seal(dek, obj['key'], aad)
                if not (hmac.compare_digest(hashlib.sha256(raw).hexdigest(), rec['verify'])):
                    raise Corrupt("invalid state or backup field")
        except (KeyError, TypeError, AssertionError, ValueError) as exc:
            raise Corrupt('invalid backup payload') from exc

    def backup(self, s):
        master = self.lmk()
        key = hmac.digest(master, b'local-backup-key/v1', 'sha256')
        payload = self.payload(s)
        self.check_payload(payload)
        envelope = seal(key, dumps(payload), b'backup/v1')
        envelope['master_object'] = s.get('master_object', 'master.key')
        raw = dumps(envelope, MAX_ENVELOPE)
        self.check_payload(loads(open_seal(key, loads(raw, MAX_ENVELOPE), b'backup/v1')))
        old = self.store.read('auth_backup_latest.enc', 'backup', missing=True)
        if old is not None:
            old_env = loads(old, MAX_ENVELOPE)
            old_name = old_env.get('master_object', 'master.key')
            self.store.name(old_name)
            old_master = self.store.read(old_name, 'master')
            old_key = hmac.digest(old_master, b'local-backup-key/v1', 'sha256')
            self.check_payload(loads(open_seal(old_key, old_env, b'backup/v1')))
            self.store.write('auth_backup_previous.enc', old, 'backup')
        self.store.write('auth_backup_latest.enc', raw, 'backup')
        self.store.write('backup_metadata.json', dumps({'revision': s['revision'], 'format': 1}), 'backup')

    def commit(self, s):
        s['revision'] += 1
        self.check_state(s)
        self.store.write('auth.json', dumps(s))
        try:
            self.backup(s)
        except Exception as exc:
            raise BackupPending('state committed; backup incomplete, do not blindly retry') from exc

    def initialize(self, value, confirmation, *, local_authority=False):
        if local_authority is not True or value != confirmation:
            raise AuthError('trusted local bootstrap and matching password required')
        with self.cost('initialize'):
            with self.store.transaction():
                if self.store.read('auth.json', missing=True) is not None or self.store.read('master.key', 'master', True) is not None:
                    raise AuthError('existing or incomplete initialization; refusing overwrite')
                phc = self.hash(value)
                recovery = secrets.token_hex(32)
                master, kek = secrets.token_bytes(32), secrets.token_bytes(32)
                s = {'format': 1, 'epoch': 1, 'revision': 0, 'version': 1,
                     'password': phc, 'recovery': hashlib.sha256(recovery.encode()).hexdigest(),
                     'kek': seal(master, kek, b'kek/1'), 'apis': {}}
                self.store.write('master.key', master, 'master')
                self.store.write('sessions.json', dumps({}))
                self.commit(s)
                return recovery

    def login(self, value, *, source):
        self.rate(source, 'login')
        with self.cost('login'):
            with self.store.transaction():
                before = self.state()
            if not self.verify(value, before['password']):
                raise AuthError('invalid credentials')
            with self.store.transaction():
                s = self.state()
                if (s['epoch'], s['password']) != (before['epoch'], before['password']):
                    raise AuthError('authentication changed; retry')
                sessions = {i: r for i, r in self.sessions().items()
                            if r.get('epoch') == s['epoch'] and r.get('expires', 0) > self.clock()}
                if len(sessions) >= MAX_SESSIONS:
                    raise Busy('Session capacity reached')
                token = secrets.token_hex(32); now = self.clock()
                sessions[hashlib.sha256(token.encode()).hexdigest()] = {
                    'epoch': s['epoch'], 'created': now, 'expires': now + TTL}
                self.store.write('sessions.json', dumps(sessions))
                return token

    def logout(self, token):
        with self.store.transaction():
            ident = self.session(self.state(), token)
            sessions = self.sessions(); del sessions[ident]
            self.store.write('sessions.json', dumps(sessions))

    def change_password(self, token, old, new, confirmation, *, source):
        self.rate(source, 'change')
        if new != confirmation:
            raise AuthError('password confirmation mismatch')
        with self.cost('change-password'):
            with self.store.transaction():
                before = self.state(); self.session(before, token)
            if not self.verify(old, before['password']):
                raise AuthError('invalid credentials')
            phc = self.hash(new)
            with self.store.transaction():
                s = self.state(); self.session(s, token)
                if s['revision'] != before['revision']:
                    raise AuthError('state changed; retry')
                s.update(password=phc, epoch=s['epoch'] + 1)
                self.commit(s)

    def recover(self, recovery, new, confirmation, *, source):
        self.rate(source, 'recovery')
        if new != confirmation or not isinstance(recovery, str) or not re.fullmatch('[0-9a-f]{64}', recovery):
            raise AuthError('invalid recovery input')
        with self.cost('recovery'):
            with self.store.transaction():
                before = self.state()
                if not hmac.compare_digest(hashlib.sha256(recovery.encode()).hexdigest(), before['recovery']):
                    raise AuthError('invalid recovery key')
            phc = self.hash(new); replacement = secrets.token_hex(32)
            with self.store.transaction():
                s = self.state()
                if s['revision'] != before['revision']:
                    raise AuthError('state changed; retry')
                s.update(password=phc, recovery=hashlib.sha256(replacement.encode()).hexdigest(), epoch=s['epoch'] + 1)
                self.commit(s)
                return replacement

    def list_apis(self, token):
        with self.store.transaction():
            s = self.state(); self.session(s, token)
            return [{'id': i, 'name': r['name']} for i, r in s['apis'].items()]

    def create_api(self, token, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise AuthError('invalid API name')
        with self.cost('api-change'):
            with self.store.transaction():
                s = self.state(); self.session(s, token)
                if len(s['apis']) >= MAX_APIS:
                    raise Busy('API capacity reached')
                ident, objid = secrets.token_hex(16), secrets.token_hex(16)
                if ident in s['apis'] or self.store.read('API_' + objid + '.json', 'api', True) is not None:
                    raise Busy('ID collision; retry')
                raw = secrets.token_urlsafe(32); dek = secrets.token_bytes(32)
                aad = ('api/' + ident + '/' + str(s['version'])).encode()
                obj = {'dek': seal(self.kek(s), dek, aad + b'/dek'), 'key': seal(dek, raw.encode(), aad)}
                self.store.write('API_' + objid + '.json', dumps(obj), 'api')
                s['apis'][ident] = {'name': name.strip(), 'object': objid, 'version': s['version'],
                                    'verify': hashlib.sha256(raw.encode()).hexdigest()}
                self.commit(s)
                return {'id': ident, 'name': name.strip(), 'key': raw}

    def read_api(self, token, ident):
        with self.store.transaction():
            s = self.state(); self.session(s, token)
            if not isinstance(ident, str) or not IDENT.fullmatch(ident) or ident not in s['apis']:
                raise AuthError('API not found')
            r = s['apis'][ident]; aad = ('api/' + ident + '/' + str(r['version'])).encode()
            obj = loads(self.store.read('API_' + r['object'] + '.json', 'api'))
            return open_seal(open_seal(self.kek(s), obj['dek'], aad + b'/dek'), obj['key'], aad).decode()

    def delete_api(self, token, ident, confirmation_name):
        with self.cost('api-change'):
            with self.store.transaction():
                s = self.state(); self.session(s, token)
                if ident not in s['apis'] or s['apis'][ident]['name'] != confirmation_name:
                    raise AuthError('API deletion confirmation mismatch')
                del s['apis'][ident]
                self.commit(s)
                # Ciphertext deliberately retained: previous backup may still refer to it.

    def verify_api(self, raw):
        if not isinstance(raw, str) or not 32 <= len(raw) <= 128:
            return False
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.store.transaction():
            return any(hmac.compare_digest(digest, r['verify']) for r in self.state()['apis'].values())

    def export_backup(self, token, independent_password):
        password(independent_password)
        with self.cost('export'):
            with self.store.transaction():
                s = self.state(); self.session(s, token)
                payload = dumps(self.payload(s))
            salt = secrets.token_bytes(16)
            if not HASH_GATE.acquire(timeout=2):
                raise Busy('Argon2 worker busy')
            try:
                key = hash_secret_raw(independent_password.encode(), salt, 3, 8192, 1, 32, Type.ID, 19)
            finally:
                HASH_GATE.release()
            header = {'format': 1, 'kdf': 'argon2id-v19', 'm': 8192, 't': 3, 'p': 1, 'salt': b64(salt)}
            return dumps({'header': header, 'sealed': seal(key, payload, dumps(header))}, MAX_ENVELOPE)

    def restore_backup(self, token, raw, independent_password):
        password(independent_password)
        with self.cost('restore'):
            with self.store.transaction():
                before = self.state(); self.session(before, token)
            envelope = loads(raw, MAX_ENVELOPE); h = envelope['header']
            if set(h) != {'format','kdf','m','t','p','salt'} or (h['format'],h['kdf'],h['m'],h['t'],h['p']) != (1,'argon2id-v19',8192,3,1):
                raise Corrupt('unsupported backup KDF')
            salt = unb64(h['salt'], 16)
            if not HASH_GATE.acquire(timeout=2):
                raise Busy('Argon2 worker busy')
            try:
                key = hash_secret_raw(independent_password.encode(), salt, 3, 8192, 1, 32, Type.ID, 19)
            finally:
                HASH_GATE.release()
            payload = loads(open_seal(key, envelope['sealed'], dumps(h)))
            self.check_payload(payload)
            with self.store.transaction():
                current = self.state(); self.session(current, token)
                if current['revision'] != before['revision']:
                    raise AuthError('state changed; retry restore')
                s = payload['state']
                old_kek = open_seal(unb64(payload['master'], 32), s['kek'], ('kek/' + str(s['version'])).encode())
                # Import into the existing local LMK; only auth.json switches authority.
                s['master_object'] = current.get('master_object', 'master.key')
                s['kek'] = seal(self.lmk(), old_kek, ('kek/' + str(s['version'])).encode())
                s['epoch'] = max(s['epoch'], current['epoch']) + 1
                s['revision'] = current['revision']
                for rec in s['apis'].values():
                    old_obj = rec['object']; rec['object'] = secrets.token_hex(16)
                    name = 'API_' + rec['object'] + '.json'
                    if self.store.read(name, 'api', True) is not None:
                        raise Busy('object collision; retry')
                    self.store.write(name, dumps(payload['objects'][old_obj]), 'api')
                self.commit(s)

    def rotate_keys(self, token):
        """Rotate LMK/KEK/DEKs atomically through auth.json authority.

        Old immutable files are deliberately retained until backup/transaction
        reference validation allows collect_objects(). No online secret logging.
        """
        with self.cost('key-rotation'):
            with self.store.transaction():
                s = self.state(); self.session(s, token)
                old = self.payload(s); self.check_payload(old)
                old_kek = self.kek(s)
                master, kek = secrets.token_bytes(32), secrets.token_bytes(32)
                version = s['version'] + 1
                master_name = 'master-' + secrets.token_hex(16) + '.key'
                if self.store.read(master_name, 'master', True) is not None:
                    raise Busy('master identifier collision')
                self.store.write(master_name, master, 'master')
                for ident, rec in s['apis'].items():
                    aad = ('api/' + ident + '/' + str(rec['version'])).encode()
                    obj = old['objects'][rec['object']]
                    raw = open_seal(open_seal(old_kek, obj['dek'], aad + b'/dek'), obj['key'], aad)
                    new_aad = ('api/' + ident + '/' + str(version)).encode()
                    dek = secrets.token_bytes(32); objid = secrets.token_hex(16)
                    name = 'API_' + objid + '.json'
                    if self.store.read(name, 'api', True) is not None:
                        raise Busy('object identifier collision')
                    self.store.write(name, dumps({'dek': seal(kek, dek, new_aad + b'/dek'),
                                                  'key': seal(dek, raw, new_aad)}), 'api')
                    rec.update(object=objid, version=version)
                s.update(master_object=master_name, version=version,
                         kek=seal(master, kek, ('kek/' + str(version)).encode()))
                self.commit(s)
                return version

    def local_backup(self, token):
        with self.cost('backup'):
            with self.store.transaction():
                s = self.state(); self.session(s, token)
                self.backup(s)
                return s['revision']

    def restore_local(self, token, slot):
        if slot not in ('latest', 'previous'):
            raise AuthError('invalid backup slot')
        with self.cost('restore'):
            with self.store.transaction():
                current = self.state(); self.session(current, token)
                envelope = loads(self.store.read('auth_backup_' + slot + '.enc', 'backup'), MAX_ENVELOPE)
                name = envelope.get('master_object', 'master.key'); self.store.name(name)
                master = self.store.read(name, 'master')
                key = hmac.digest(master, b'local-backup-key/v1', 'sha256')
                payload = loads(open_seal(key, envelope, b'backup/v1'))
                self.check_payload(payload)
                self._import_payload(current, payload)

    def _import_payload(self, current, payload):
        """Caller owns transaction and resource gate; input already authenticated."""
        s = payload['state']
        kek = open_seal(unb64(payload['master'], 32), s['kek'], ('kek/' + str(s['version'])).encode())
        s['master_object'] = current.get('master_object', 'master.key')
        s['kek'] = seal(self.lmk(), kek, ('kek/' + str(s['version'])).encode())
        s['epoch'] = max(s['epoch'], current['epoch']) + 1
        s['revision'] = current['revision']
        for rec in s['apis'].values():
            old_obj = rec['object']; rec['object'] = secrets.token_hex(16)
            name = 'API_' + rec['object'] + '.json'
            if self.store.read(name, 'api', True) is not None:
                raise Busy('object identifier collision')
            self.store.write(name, dumps(payload['objects'][old_obj]), 'api')
        self.commit(s)

    def collect_objects(self, token):
        """Only immutable unreferenced objects; fail closed on invalid backups.

        Serialized with all auth operations, so exports/imports and rotations
        cannot run concurrently. External exports contain their own key material.
        Unknown temporary/transaction records cause refusal, never speculative GC.
        """
        with self.cost('key-cleanup'):
            with self.store.transaction():
                s = self.state(); self.session(s, token)
                roots = [s]
                masters = {s.get('master_object', 'master.key')}
                for slot in ('latest', 'previous'):
                    raw = self.store.read('auth_backup_' + slot + '.enc', 'backup', True)
                    if raw is None:
                        continue
                    env = loads(raw, MAX_ENVELOPE)
                    name = env.get('master_object', 'master.key'); self.store.name(name)
                    master = self.store.read(name, 'master'); masters.add(name)
                    key = hmac.digest(master, b'local-backup-key/v1', 'sha256')
                    payload = loads(open_seal(key, env, b'backup/v1'))
                    self.check_payload(payload); roots.append(payload['state'])
                objects = {r['object'] for state in roots for r in state['apis'].values()}
                with self.store.directory('') as rootfd:
                    names = os.listdir(rootfd)
                    if any(n.startswith('.tmp-') or n.startswith('transaction') for n in names):
                        raise Busy('incomplete storage operation; cleanup refused')
                removed = 0
                for area in ('api', 'master'):
                    with self.store.directory(area) as fd:
                        for name in os.listdir(fd):
                            if name.startswith('.tmp-'):
                                raise Busy('incomplete object write; cleanup refused')
                            if area == 'api':
                                match = re.fullmatch(r'API_([0-9a-f]{32})\.json', name)
                                delete = match is not None and match[1] not in objects
                            else:
                                delete = (name == 'master.key' or re.fullmatch(r'master-[0-9a-f]{32}\.key', name)) and name not in masters
                            if delete:
                                self.store.read(name, area)
                                os.unlink(name, dir_fd=fd); removed += 1
                        os.fsync(fd)
                return removed

    def import_fresh(self, raw, independent_password, *, local_authority=False):
        """Offline/terminal disaster restore when the original VPS is unavailable.

        Rejects ALL initialization traces. Does not import active Sessions. A failed
        precommit restore leaves traces for explicit recovery, never auto-reset.
        """
        if local_authority is not True:
            raise AuthError('trusted terminal restore required')
        password(independent_password)
        with self.cost('restore'):
            envelope = loads(raw, MAX_ENVELOPE); h = envelope['header']
            if set(h) != {'format','kdf','m','t','p','salt'} or (h['format'],h['kdf'],h['m'],h['t'],h['p']) != (1,'argon2id-v19',8192,3,1):
                raise Corrupt('unsupported backup KDF')
            if not HASH_GATE.acquire(timeout=2): raise Busy('Argon2 worker busy')
            try:
                key=hash_secret_raw(independent_password.encode(),unb64(h['salt'],16),3,8192,1,32,Type.ID,19)
            finally: HASH_GATE.release()
            payload=loads(open_seal(key,envelope['sealed'],dumps(h)));self.check_payload(payload)
            with self.store.transaction():
                if self.store.read('auth.json',missing=True) is not None or self.store.read('master.key','master',True) is not None:
                    raise AuthError('fresh restore refuses existing or partial vault')
                s=payload['state'];s.pop('master_object',None)
                self.store.write('master.key',unb64(payload['master'],32),'master')
                self.store.write('sessions.json',dumps({}))
                for objid,obj in payload['objects'].items():
                    if not IDENT.fullmatch(objid):raise Corrupt('invalid imported object ID')
                    self.store.write('API_'+objid+'.json',dumps(obj),'api')
                s['epoch']+=1
                self.commit(s)
