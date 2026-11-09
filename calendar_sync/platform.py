"""Small POSIX/Windows portability layer (file locking and permission hardening)."""
from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


@contextmanager
def file_lock(handle) -> Iterator[None]:
    """Exclusive inter-process lock on an open file handle."""
    fileno = handle.fileno()
    if sys.platform != "win32":
        fcntl.flock(fileno, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fileno, fcntl.LOCK_UN)
        return
    handle.seek(0)
    while True:
        try:
            msvcrt.locking(fileno, msvcrt.LK_LOCK, 1)
            break
        except OSError:  # LK_LOCK gives up after ~10 s; keep waiting
            continue
    try:
        yield
    finally:
        handle.seek(0)
        msvcrt.locking(fileno, msvcrt.LK_UNLCK, 1)


def harden_fd(fd: int) -> None:
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)


def harden_path(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def keep_owner(fd: int, reference: os.stat_result) -> None:
    if not hasattr(os, "fchown"):
        return
    try:
        os.fchown(fd, reference.st_uid, reference.st_gid)
    except PermissionError:
        current = os.fstat(fd)
        if (current.st_uid, current.st_gid) != (reference.st_uid, reference.st_gid):
            raise
