#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, kitty contributors

import os
import shutil
import stat
import tempfile
from contextlib import ExitStack
from unittest.mock import patch

from .base import BaseTest


class TestSessionKeys(BaseTest):
    def setUp(self):
        super().setUp()
        from .crypto import is_rlimit_memlock_too_low

        if is_rlimit_memlock_too_low():
            self.skipTest('RLIMIT_MEMLOCK is too low')

    def environment(self):
        from kitty import logout_session as ls
        from kitty import session_keys as sk

        stack = ExitStack()
        self.addCleanup(stack.close)
        root = os.path.realpath(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.object(sk, 'key_storage_root', return_value=os.path.join(root, 'keys')))
        stack.enter_context(patch.object(ls, 'cache_dir', return_value=os.path.join(root, 'cache')))
        stack.enter_context(patch.object(ls, 'kitty_exe', return_value=os.path.join(root, 'kitty')))
        stack.enter_context(patch.object(ls, 'is_macos', True))
        # New snapshots must not depend on any Keychain operation.
        for method in ('store', 'read', 'delete', 'list_ids'):
            stack.enter_context(patch.object(ls.MacOSKeychain, method, side_effect=AssertionError('Unexpected Keychain access')))
        return root, ls, sk

    def test_local_session_key_roundtrip(self):
        from kitty.fast_data_types import random_secret

        _, ls, sk = self.environment()
        store = sk.LocalKeyStore()
        key_id, key = ls.new_key_id(), random_secret()
        store.store(key_id, key)
        self.ae(sk.LocalKeyStore().read_local(key_id), key)
        self.ae(stat.S_IMODE(os.stat(os.path.join(store.directory, sk.MASTER_FILE)).st_mode), 0o600)
        self.ae(stat.S_IMODE(os.stat(store.directory).st_mode), 0o700)
        self.ae(stat.S_IMODE(os.stat(os.path.join(store.directory, store.namespace, key_id)).st_mode), 0o600)
        with patch.object(ls, 'kitty_exe', return_value=ls.kitty_exe() + '-other'):
            other = sk.LocalKeyStore()
            other.store(key_id, random_secret())
            self.assertNotEqual(other.read_local(key_id), key)
        self.ae(store.read_local(key_id), key)

    def test_local_session_key_missing_master(self):
        from kitty.fast_data_types import random_secret

        _, ls, sk = self.environment()
        store = sk.LocalKeyStore()
        key_id, key = ls.new_key_id(), random_secret()
        store.store(key_id, key)
        master = os.path.join(store.directory, sk.MASTER_FILE)
        os.rename(master, master + '.backup')
        with self.assertRaises(FileNotFoundError):
            store.read_local(key_id)
        with self.assertRaises(FileNotFoundError):
            store.store(ls.new_key_id(), random_secret())
        self.assertFalse(os.path.exists(master))
        os.rename(master + '.backup', master)
        self.ae(store.read_local(key_id), key)

    def test_local_session_key_authentication(self):
        from kitty.fast_data_types import CryptoError, random_secret

        _, ls, sk = self.environment()
        store = sk.LocalKeyStore()
        first, second = ls.new_key_id(), ls.new_key_id()
        store.store(first, random_secret())
        store.store(second, random_secret())
        directory = os.path.join(store.directory, store.namespace)
        shutil.copyfile(os.path.join(directory, first), os.path.join(directory, second))
        with self.assertRaises(CryptoError):
            store.read_local(second)
        with open(os.path.join(directory, first), 'r+b') as stream:
            stream.seek(-1, os.SEEK_END)
            last = stream.read(1)
            stream.seek(-1, os.SEEK_END)
            stream.write(bytes([last[0] ^ 1]))
        with self.assertRaises(CryptoError):
            store.read_local(first)

    def test_local_session_key_permissions(self):
        from kitty.fast_data_types import random_secret

        root, ls, sk = self.environment()
        store = sk.LocalKeyStore()
        key_id = ls.new_key_id()
        store.store(key_id, random_secret())
        master = os.path.join(store.directory, sk.MASTER_FILE)
        os.chmod(master, 0o644)
        with self.assertRaises(PermissionError):
            store.read_local(key_id)
        os.chmod(master, 0o600)
        os.rename(master, master + '.original')
        os.symlink(master + '.original', master)
        with self.assertRaises(OSError):
            store.read_local(key_id)
        os.unlink(master)
        os.rename(master + '.original', master)
        os.link(master, os.path.join(root, 'master-hardlink'))
        with self.assertRaises(PermissionError):
            store.read_local(key_id)

    def test_local_session_snapshot_and_agent_restore(self):
        import json
        import shlex

        from kitty.constants import unserialize_launch_flag

        root, ls, sk = self.environment()
        opts = self.set_options({'resume_agents_on_restore': 'yes'})
        session = shlex.join(
            ['launch', unserialize_launch_flag + json.dumps({'id': 7, 'agent': 'claude'}), '--cwd=' + root, '--var=KITTY_AGENT_SESSION=abc-123']
        )
        base = ls.snapshots_dir()
        state = ls.ProcessState((), root, {}, 0)
        path = ls.save_snapshot(base, 10, session, {7: b'private scrollback'}, state, sk.LocalKeyStore())
        # A fresh adapter represents the next application build/process, with Keychain completely unavailable.
        sessions = ls.load_snapshot(path, opts, sk.LocalKeyStore())
        window = sessions[0].tabs[0].windows[0]
        self.ae(window.launch_spec.opts.cwd, root)
        self.assertIn('claude --resume abc-123', window.launch_spec.args[-1])
        self.assertTrue(window.restored_scrollback.startswith(b'private scrollback'))
        # Losing the entire key directory must not silently create a replacement master for existing local backups.
        shutil.rmtree(store_directory := sk.LocalKeyStore().directory)
        with self.assertRaises(FileNotFoundError):
            ls.load_snapshot(path, opts, sk.LocalKeyStore())
        with self.assertRaises(FileNotFoundError):
            ls.save_snapshot(base, 11, session, {}, state, sk.LocalKeyStore())
        self.assertFalse(os.path.exists(os.path.join(store_directory, sk.MASTER_FILE)))
        self.assertTrue(os.path.isdir(path))

    def test_local_session_legacy_migration(self):
        root, ls, sk = self.environment()
        opts = self.set_options()
        keys = {}
        legacy = ls.MacOSKeychain()
        with (
            patch.object(ls.MacOSKeychain, 'read', side_effect=keys.get),
            patch.object(ls.MacOSKeychain, 'store', side_effect=lambda ident, value: keys.__setitem__(ident, value)),
            patch.object(ls.MacOSKeychain, 'delete', side_effect=lambda ident: keys.pop(ident, None)),
            patch.object(ls.MacOSKeychain, 'list_ids', side_effect=lambda: list(keys)),
        ):
            path = ls.save_snapshot(ls.snapshots_dir(), 10, 'launch --cwd=' + root, {}, ls.ProcessState((), root, {}, 0), legacy)
            actual_id = ls.read_key_id(path)
            store = sk.LocalKeyStore()
            # Reading the legacy key alone is not enough to authorize destructive migration.
            self.ae(store.read(actual_id), keys[actual_id])
            self.assertIn(actual_id, keys)
            ls.load_snapshot(path, opts, store)
            self.assertNotIn(actual_id, keys)
            self.ae(ls.load_snapshot(path, opts, sk.LocalKeyStore())[0].tabs[0].windows[0].launch_spec.opts.cwd, root)

    def test_secret_fd_and_authenticated_wrapping(self):
        from kitty.fast_data_types import AES256GCMDecrypt, AES256GCMEncrypt, CryptoError, Secret, random_secret

        key, master = random_secret(), random_secret()
        with tempfile.TemporaryFile() as stream:
            key.write_to_fd(stream.fileno())
            stream.seek(0)
            self.ae(Secret.from_fd(stream.fileno(), 32), key)
            with self.assertRaises(EOFError):
                Secret.from_fd(stream.fileno(), 32)
        e = AES256GCMEncrypt(master)
        ciphertext = e.add_secret_to_be_encrypted(key, True)
        self.ae(AES256GCMDecrypt(master, e.iv, e.tag).add_data_to_be_decrypted_as_secret(ciphertext), key)
        corrupted = ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])
        with self.assertRaises(CryptoError):
            AES256GCMDecrypt(master, e.iv, e.tag).add_data_to_be_decrypted_as_secret(corrupted)
        with self.assertRaises(ValueError):
            AES256GCMDecrypt(master, e.iv, e.tag).add_data_to_be_decrypted_as_secret(ciphertext, False)
        with self.assertRaises(OSError):
            Secret.from_fd(-1)
        with self.assertRaises(OSError):
            key.write_to_fd(-1)

    def test_local_session_key_concurrent_creation(self):
        from concurrent.futures import ThreadPoolExecutor

        from kitty.fast_data_types import random_secret

        _, ls, sk = self.environment()
        keys = [(ls.new_key_id(), random_secret()) for _ in range(8)]

        def save(pair):
            sk.LocalKeyStore().store(*pair)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(save, keys))
        store = sk.LocalKeyStore()
        for key_id, key in keys:
            self.ae(store.read_local(key_id), key)
