#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, kitty contributors

"""Persistent local wrapping keys for macOS session snapshots, independent of code signing."""

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress

from .fast_data_types import AES256GCMDecrypt, AES256GCMEncrypt, Secret, random_secret

KEY_MAGIC = b'kitty-session-key-1\0'
MASTER_FILE = 'master.key'


def key_storage_root() -> str:
    return os.path.expanduser('~/Library/Application Support/agentty/session-keys')


@contextmanager
def private_directory(path: str) -> Iterator[int]:
    """Traverse without following symlinks; validate permissions on the managed directory."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in os.path.abspath(path).split(os.sep):
            if not part:
                continue
            with suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=fd)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError(f'Session key directory must be owned by the current user with mode 0700: {path}')
        yield fd
    finally:
        os.close(fd)


def check_private_file(fd: int, name: str) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        raise PermissionError(f'Session key file must be a private, single-link regular file with mode 0600: {name}')
    return info


class LocalKeyStore:
    def __init__(self, base: str | None = None) -> None:
        from .logout_session import KEYCHAIN_SERVICE, MacOSKeychain, legacy_snapshots_dir, session_storage_id, snapshots_dir

        legacy = base is not None and os.path.realpath(base) == os.path.realpath(legacy_snapshots_dir())
        # Unscoped legacy snapshots must remain readable by every installation that could restore them before migration.
        self.installation = 'legacy' if legacy else session_storage_id()
        self.namespace = 'keys'
        self.legacy = MacOSKeychain(KEYCHAIN_SERVICE if legacy else None)
        self.snapshot_base = base if base is not None else snapshots_dir()
        self.directory = os.path.join(key_storage_root(), self.installation)

    def _validate_id(self, key_id: str) -> None:
        from .logout_session import key_id_pat

        if key_id_pat().fullmatch(key_id) is None:
            raise ValueError('Invalid session encryption key id')

    @contextmanager
    def _directory(self) -> Iterator[int]:
        # Validate each managed level, not just the leaf. Ancestors are traversed with O_NOFOLLOW.
        with private_directory(key_storage_root()), private_directory(self.directory) as fd:
            yield fd

    def _master(self, directory_fd: int, create: bool) -> Secret:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            lock_fd = os.open('master.lock', flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            lock_fd = os.open('master.lock', flags, dir_fd=directory_fd)
        try:
            check_private_file(lock_fd, 'master.lock')
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                fd = os.open(MASTER_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory_fd)
            except FileNotFoundError:
                from .logout_session import KEY_BACKEND_FILE

                try:
                    snapshots = os.scandir(self.snapshot_base)
                except FileNotFoundError:
                    pass
                else:
                    with snapshots:
                        if any(os.path.exists(os.path.join(snapshot.path, KEY_BACKEND_FILE)) for snapshot in snapshots):
                            raise FileNotFoundError(f'Session master key is missing; restore {self.directory}/{MASTER_FILE} from backup')
                # A missing master must never be silently replaced if any wrapped keys survive.
                for namespace in ('keys',):
                    try:
                        ns_fd = os.open(namespace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory_fd)
                    except FileNotFoundError:
                        continue
                    try:
                        if os.listdir(ns_fd):
                            raise FileNotFoundError(f'Session master key is missing; restore {self.directory}/{MASTER_FILE} from backup')
                    finally:
                        os.close(ns_fd)
                if not create:
                    raise
                key = random_secret()
                name = f'.master-{os.getpid()}-{os.urandom(8).hex()}.tmp'
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory_fd)
                try:
                    try:
                        key.write_to_fd(fd)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    os.link(name, MASTER_FILE, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
                finally:
                    os.unlink(name, dir_fd=directory_fd)
                os.fsync(directory_fd)
                return key
            try:
                if check_private_file(fd, MASTER_FILE).st_size != 32:
                    raise ValueError(f'Invalid session master key length: {self.directory}/{MASTER_FILE}')
                return Secret.from_fd(fd, 32)
            finally:
                os.close(fd)
        finally:
            os.close(lock_fd)

    def _aad(self, key_id: str) -> bytes:
        return KEY_MAGIC + f'{self.installation}/{self.namespace}/{key_id}'.encode('ascii')

    def store(self, key_id: str, key: Secret) -> None:
        self._validate_id(key_id)
        with self._directory() as fd:
            master = self._master(fd, True)
            e = AES256GCMEncrypt(master)
            e.add_authenticated_but_unencrypted_data(self._aad(key_id))
            ciphertext = e.add_secret_to_be_encrypted(key, True)
            data = KEY_MAGIC + e.iv + e.tag + ciphertext
            with private_directory(os.path.join(self.directory, self.namespace)) as keys_fd:
                name = f'.{key_id}-{os.urandom(8).hex()}.tmp'
                out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=keys_fd)
                try:
                    with os.fdopen(out, 'wb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.link(name, key_id, src_dir_fd=keys_fd, dst_dir_fd=keys_fd, follow_symlinks=False)
                finally:
                    os.unlink(name, dir_fd=keys_fd)
                os.fsync(keys_fd)

    def _read_local(self, key_id: str) -> Secret | None:
        with self._directory() as directory_fd:
            with private_directory(os.path.join(self.directory, self.namespace)) as keys_fd:
                try:
                    fd = os.open(key_id, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=keys_fd)
                except FileNotFoundError:
                    return None
                try:
                    info = check_private_file(fd, key_id)
                    if info.st_size != len(KEY_MAGIC) + 12 + 16 + 32:
                        raise ValueError('Invalid wrapped session key length')
                    with os.fdopen(fd, 'rb', closefd=False) as stream:
                        data = stream.read(info.st_size)
                finally:
                    os.close(fd)
            if not data.startswith(KEY_MAGIC):
                raise ValueError('Unsupported wrapped session key format')
            master = self._master(directory_fd, False)
            start = len(KEY_MAGIC)
            d = AES256GCMDecrypt(master, data[start : start + 12], data[start + 12 : start + 28])
            d.add_data_to_be_authenticated_but_not_decrypted(self._aad(key_id))
            return d.add_data_to_be_decrypted_as_secret(data[start + 28 :], True)

    def read(self, key_id: str) -> Secret | None:
        self._validate_id(key_id)
        local = self._read_local(key_id)
        return local if local is not None else self.legacy.read(key_id)

    def read_local(self, key_id: str) -> Secret:
        self._validate_id(key_id)
        key = self._read_local(key_id)
        if key is None:
            raise FileNotFoundError(f'Wrapped session key {key_id} is missing; restore {self.directory} from backup')
        return key

    def migrate(self, key_id: str) -> None:
        """Called only after the entire legacy snapshot has been successfully validated."""
        self._validate_id(key_id)
        if self._read_local(key_id) is not None:
            return
        key = self.legacy.read(key_id)
        if key is None:
            raise ValueError('The legacy snapshot encryption key is missing')
        self.store(key_id, key)
        if self._read_local(key_id) != key:
            raise ValueError('Migrated session encryption key failed verification')
        self.legacy.delete(key_id)

    def delete(self, key_id: str) -> None:
        self._validate_id(key_id)
        with self._directory():
            with private_directory(os.path.join(self.directory, self.namespace)) as fd:
                try:
                    os.unlink(key_id, dir_fd=fd)
                except FileNotFoundError:
                    self.legacy.delete(key_id)
                else:
                    os.fsync(fd)

    def list_ids(self) -> list[str]:
        from .logout_session import key_id_pat

        with self._directory():
            with private_directory(os.path.join(self.directory, self.namespace)) as fd:
                return [name for name in os.listdir(fd) if key_id_pat().fullmatch(name)]
