"""Local snapshot ownership; persistent sidecars are never removed or replaced.

This leaf deliberately imports neither board state nor lifecycle/publisher code.
All consumers must be quiescent during legacy cutover or rollback: the old
pathname protocol and this OS lock cannot coordinate concurrent writers.
"""

import contextlib
import errno
import math
import os
import stat
import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Generator

POLL_SECONDS = 0.02


class LockRefusedError(ValueError):
    """The ownership boundary was not entered."""


@dataclass
class _PathMutex:
    mutex: threading.Lock = field(default_factory=threading.Lock)
    owner: int | None = None


_registry_guard = threading.Lock()
_registry: weakref.WeakValueDictionary[str, _PathMutex] = weakref.WeakValueDictionary()


def _path_mutex(path: pathlib.Path) -> _PathMutex:
    key = os.path.normcase(str(path))
    with _registry_guard:
        state = _registry.get(key)
        if state is None:
            state = _PathMutex()
            _registry[key] = state
        if state.owner == threading.get_ident():
            raise LockRefusedError("recursive snapshot transaction refused")
        return state


def _regular(path: pathlib.Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode) or path.is_junction():
        raise OSError("snapshot transaction sidecar must be a regular file")


def _try_lock(descriptor: int) -> bool:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415 -- platform lock primitive

        try:
            # Windows permits locking beyond EOF. Reserve byte zero before any
            # initialization write; contenders must never write through its lock.
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True
    import fcntl  # noqa: PLC0415 -- platform lock primitive

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415 -- platform lock primitive

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl  # noqa: PLC0415 -- platform lock primitive

        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _close(descriptor: int) -> None:
    os.close(descriptor)


@dataclass
class _DescriptorLease:
    descriptor: int | None
    _release_guard: threading.Lock = field(default_factory=threading.Lock)

    def release(self) -> None:
        """Consume only our descriptor, attempting close even if unlock fails."""
        with self._release_guard:
            descriptor, self.descriptor = self.descriptor, None
        if descriptor is None:
            return
        try:
            _unlock(descriptor)
        except BaseException as primary:
            try:
                _close(descriptor)
            except BaseException as cleanup:  # noqa: BLE001 -- preserve original failure, even interruption
                primary.add_note(
                    f"Transaction descriptor close also failed: {type(cleanup).__name__}"
                )
            raise
        else:
            _close(descriptor)


def _claim(path: pathlib.Path, deadline: float) -> _DescriptorLease:
    _regular(path)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0), 0o600)
    lease = _DescriptorLease(descriptor)
    acquired = False
    try:
        os.set_inheritable(descriptor, False)  # noqa: FBT003 -- positional-only OS API
        _regular(path)
        _wait_for_lock(descriptor, deadline)
        acquired = True
        # No pathname replacement or truncation, even for an existing empty file.
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
    except BaseException as primary:
        try:
            if acquired:
                lease.release()
            else:
                lease.descriptor = None
                _close(descriptor)
        except BaseException as cleanup:  # noqa: BLE001 -- acquisition error remains primary
            primary.add_note(f"Transaction acquisition cleanup failed: {type(cleanup).__name__}")
        raise
    return lease


def _wait_for_lock(descriptor: int, deadline: float) -> None:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise OSError("snapshot transaction sidecar must be a regular file")
    while not _try_lock(descriptor):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LockRefusedError("snapshot is locked by another writer; nothing was changed")
        time.sleep(min(POLL_SECONDS, remaining))


def _refuse_legacy(path: pathlib.Path) -> None:
    if os.path.lexists(path.with_name(path.name + ".lock")):
        raise LockRefusedError(
            "legacy snapshot lock exists; coordinated quiescent cutover required"
        )


@contextlib.contextmanager
def hold(path: pathlib.Path, timeout: float) -> Generator[None]:
    """Hold one canonical target with a single thread-plus-OS wait budget."""
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("snapshot transaction timeout must be finite and nonnegative")
    deadline = time.monotonic() + timeout
    state = _path_mutex(path)
    if not state.mutex.acquire(
        timeout=min(threading.TIMEOUT_MAX, max(0, deadline - time.monotonic()))
    ):
        raise LockRefusedError("snapshot is locked by another writer; nothing was changed")
    primary: BaseException | None = None
    lease: _DescriptorLease | None = None
    try:
        with _registry_guard:
            state.owner = threading.get_ident()
        _refuse_legacy(path)
        lease = _claim(path.with_name(path.name + ".transaction.lock"), deadline)
        _refuse_legacy(path)
        yield
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            if lease is not None:
                try:
                    lease.release()
                except BaseException as cleanup:
                    if primary is None:
                        raise
                    primary.add_note(f"Transaction release also failed: {type(cleanup).__name__}")
        finally:
            with _registry_guard:
                state.owner = None
            state.mutex.release()
