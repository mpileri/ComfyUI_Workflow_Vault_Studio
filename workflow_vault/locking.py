"""Cross-process write lock for a vault.

Each ComfyUI process serializes its own writes with an asyncio lock, but in a
studio every artist runs their own ComfyUI against the same vault folder, so
two of them can read-modify-write the same manifest at once and one save is
silently lost. This lock lives in the vault itself (<vault>/.wv_lock) and uses
POSIX record locks (fcntl.lockf), which NFS (via NLM / NFSv4) and SMB/CIFS
mounts honor across machines.

Best-effort by design: on Windows, or if the lock file can't be opened, writes
go ahead unlocked (as upstream always did) rather than failing.
"""

import errno
import os

from . import studio, utils

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

_WARNED = set()


class VaultBusy(Exception):
    """Another process held the vault lock for longer than we were willing to wait."""


class VaultFileLock:
    def __init__(self, vault_root):
        self.path = os.path.join(vault_root, studio.LOCK_FILENAME)
        self._fd = None

    def try_acquire(self):
        """One non-blocking attempt. True when held (or when locking is
        unavailable here), False when another process holds it."""
        if fcntl is None:
            return True
        if self._fd is None:
            try:
                existed = os.path.exists(self.path)
                self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
                if not existed:
                    utils.share_path(self.path)
            except OSError as e:
                if self.path not in _WARNED:
                    _WARNED.add(self.path)
                    print(f"[Workflow Vault] Can't open the vault lock file {self.path} ({e}); "
                          "saving without a cross-process lock. Check the vault's permissions.")
                return True
        try:
            fcntl.lockf(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EAGAIN):
                return False
            # e.g. ENOLCK: the mount has no lock manager. Don't block saves on it.
            if self.path not in _WARNED:
                _WARNED.add(self.path)
                print(f"[Workflow Vault] File locking is unavailable on {self.path} ({e}); "
                      "saving without a cross-process lock.")
            self._close()
            return True

    def release(self):
        if self._fd is None:
            return
        try:
            fcntl.lockf(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        self._close()

    def _close(self):
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
