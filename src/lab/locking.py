"""Advisory file locks: one global lock for create/archive/restore/delete, one per project."""

import os
from contextlib import contextmanager
from pathlib import Path

from .errors import PreconditionError

try:
    import fcntl

    def _lock(fd, block):
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))

    def _unlock(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)

except ImportError:  # Windows: dev/test only, the host core runs on Linux
    import msvcrt

    def _lock(fd, block):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK if block else msvcrt.LK_NBLCK, 1)

    def _unlock(fd):
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextmanager
def lock(path: str, *, block: bool = False, what: str = "another lab operation"):
    """Hold an exclusive lock on `path`. Non-blocking by default: fail fast with a clear message."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            _lock(fd, block)
        except OSError as e:
            raise PreconditionError(f"{what} is already in progress") from e
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
