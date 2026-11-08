import contextlib
import json
import os
import tempfile
import unittest
from unittest import mock

from auth import Auth, AuthError, BackupPending, Busy, Corrupt, Store, UncertainCommit, loads

P = 'synthetic-test-password-123'
N = 'new-synthetic-password-456'
B = 'independent-backup-password'


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.a = Auth(os.path.join(self.tmp.name, 'auth_data'),
                      gate=lambda kind: contextlib.nullcontext(), clock=lambda: self.now)
        self.recovery = self.a.initialize(P, P, local_authority=True)

    def tearDown(self):
        self.a.close(); self.tmp.cleanup()

    def login(self):
        return self.a.login(P, source='test')

    def test_fixed_phc_and_no_plaintext(self):
        raw = self.a.store.read('auth.json')
        self.assertIn(b'm=8192,t=3,p=1', raw)
        self.assertNotIn(P.encode(), raw)
        self.assertNotIn(self.recovery.encode(), raw)

    def test_initialization_cannot_overwrite(self):
        with self.assertRaises(AuthError): self.a.initialize(N, N, local_authority=True)
        with self.assertRaises(AuthError): self.a.initialize(N, N)

    def test_missing_gate_denies(self):
        self.a.gate = None
        with self.assertRaises(Busy): self.login()

    def test_sessions_logout_current_expiry(self):
        a, b = self.login(), self.login()
        self.a.logout(a)
        with self.assertRaises(AuthError): self.a.validate(a)
        self.assertEqual(self.a.validate(b), 1)
        self.now += 86400
        with self.assertRaises(AuthError): self.a.validate(b)

    def test_epoch_immediate_without_cleanup(self):
        a, b = self.login(), self.login()
        self.a.change_password(a, P, N, N, source='test')
        self.assertTrue(self.a.sessions())
        for token in (a, b):
            with self.assertRaises(AuthError): self.a.validate(token)
        self.assertTrue(self.a.login(N, source='test'))

    def test_recovery_rotation(self):
        token = self.login()
        new = self.a.recover(self.recovery, N, N, source='test')
        self.assertNotEqual(new, self.recovery)
        with self.assertRaises(AuthError): self.a.validate(token)
        with self.assertRaises(AuthError): self.a.recover(self.recovery, P, P, source='test')
        self.assertNotIn(new.encode(), self.a.store.read('auth.json'))

    def test_api_independent_objects_and_delete(self):
        token = self.login()
        one = self.a.create_api(token, 'one'); two = self.a.create_api(token, 'two')
        self.assertNotEqual(one['id'], two['id'])
        self.assertEqual(self.a.read_api(token, one['id']), one['key'])
        self.assertTrue(self.a.verify_api(one['key']))
        self.assertEqual(len(os.listdir(os.path.join(self.a.store.root, 'api'))) if hasattr(self.a.store, 'root') else len(self.a.state()['apis']), 2)
        with self.assertRaises(AuthError): self.a.delete_api(token, one['id'], 'wrong')
        self.a.delete_api(token, one['id'], 'one')
        self.assertFalse(self.a.verify_api(one['key']))
        self.assertEqual(len(self.a.list_apis(token)), 1)

    def test_export_restore_and_wrong_password_preserve(self):
        token = self.login(); one = self.a.create_api(token, 'export')
        package = self.a.export_backup(token, B)
        self.assertNotIn(one['key'].encode(), package)
        self.assertNotIn(token.encode(), package)
        self.a.delete_api(token, one['id'], 'export')
        before = self.a.store.read('auth.json')
        with self.assertRaises(Corrupt): self.a.restore_backup(token, package, 'incorrect-backup-password')
        self.assertEqual(before, self.a.store.read('auth.json'))
        self.a.restore_backup(token, package, B)
        with self.assertRaises(AuthError): self.a.validate(token)
        token = self.login()
        self.assertEqual(self.a.read_api(token, one['id']), one['key'])

    def test_oversized_kdf_rejected(self):
        token = self.login(); package = loads(self.a.export_backup(token, B), 1048576)
        package['header']['m'] = 99999999
        with self.assertRaises(Corrupt): self.a.restore_backup(token, json.dumps(package).encode(), B)

    def test_rate_limited(self):
        for _ in range(5):
            with self.assertRaises(AuthError): self.a.login(N, source='attacker')
        with self.assertRaises(Busy): self.a.login(P, source='attacker')

    def test_pre_replace_failure_old_authority_survives(self):
        token = self.login(); before = self.a.store.read('auth.json')
        original = os.replace
        def fail(src, dst, **kwargs):
            if dst == 'auth.json': raise OSError('injected')
            return original(src, dst, **kwargs)
        with mock.patch('auth.os.replace', side_effect=fail):
            with self.assertRaises(OSError): self.a.create_api(token, 'fails')
        self.assertEqual(before, self.a.store.read('auth.json'))
        self.a.validate(token)

    def test_post_replace_sync_failure_poisoned(self):
        real = os.fsync
        count = 0
        def fail(fd):
            nonlocal count
            count += 1
            if count == 2: raise OSError('directory fsync injected')
            return real(fd)
        with mock.patch('auth.os.fsync', side_effect=fail):
            with self.assertRaises(UncertainCommit): self.a.store.write('probe.json', b'{}')
        with self.assertRaises(UncertainCommit): self.a.validate(self.login())

    def test_backup_failure_explicit(self):
        token = self.login()
        with mock.patch.object(self.a, 'backup', side_effect=OSError('injected')):
            with self.assertRaises(BackupPending): self.a.create_api(token, 'committed')
        self.assertEqual(len(self.a.list_apis(token)), 1)

    def test_backup_previous_valid_and_nonce_fresh(self):
        token = self.login()
        first = self.a.store.read('auth_backup_latest.enc', 'backup')
        self.a.create_api(token, 'first')
        self.assertEqual(first, self.a.store.read('auth_backup_previous.enc', 'backup'))
        self.assertNotEqual(first, self.a.store.read('auth_backup_latest.enc', 'backup'))

    def test_symlink_hardlink_mode_and_duplicate_rejected(self):
        root = os.path.join(self.tmp.name, 'auth_data')
        os.symlink('/etc/passwd', os.path.join(root, 'trap.json'))
        with self.assertRaises(OSError): self.a.store.read('trap.json')
        os.link(os.path.join(root, 'auth.json'), os.path.join(root, 'linked'))
        with self.assertRaises(Corrupt): self.a.state()
        os.unlink(os.path.join(root, 'linked'))
        os.chmod(os.path.join(root, 'auth.json'), 0o644)
        with self.assertRaises(Corrupt): self.a.state()
        with self.assertRaises(Corrupt): loads(b'{"a":1,"a":2}')

    def test_ancestor_symlink_refused(self):
        alias = os.path.join(self.tmp.name, 'alias')
        os.symlink(os.path.join(self.tmp.name, 'auth_data'), alias)
        with self.assertRaises(OSError): Store(os.path.join(alias, 'child'))


if __name__ == '__main__':
    unittest.main()

class RotationTests(AuthTests):
    def test_rotation_preserves_api_and_backups(self):
        token = self.login(); api = self.a.create_api(token, 'rotate')
        before = self.a.state()['version']
        self.assertEqual(self.a.rotate_keys(token), before + 1)
        self.assertEqual(self.a.read_api(token, api['id']), api['key'])
        self.a.restore_local(token, 'previous')
        with self.assertRaises(AuthError): self.a.validate(token)
        token = self.login()
        self.assertEqual(self.a.read_api(token, api['id']), api['key'])

    def test_gc_keeps_previous_backup_keys(self):
        token = self.login(); api = self.a.create_api(token, 'gc')
        self.a.rotate_keys(token)
        self.a.collect_objects(token)
        self.a.restore_local(token, 'previous')
        token = self.login()
        self.assertEqual(self.a.read_api(token, api['id']), api['key'])
