"""Process-lifetime board ownership and coordinated, capability-scoped launch gates."""

import contextlib
import errno
import hashlib
import http.client
import json
import os
import pathlib
import secrets
import stat
import sys
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, cast

from . import board_state

if TYPE_CHECKING:
    from collections.abc import Generator

LAUNCH_MARKER_ENV = "LOCALSWIM_SERVICE_LAUNCH"
LOCK_WAIT_SECONDS = 15.0
POLL_SECONDS = 0.05
MAX_TICKET_BYTES = 4096
MAX_METADATA_BYTES = 64 * 1024
PORT_LIMIT = 65536


class WriterAlreadyRunningError(OSError):
    """Another service owns this board, even while its listener is restarting."""


def python_child_command(arguments: list[str], environment: dict[str, str]) -> list[str]:
    """Launch the actual Windows interpreter, retaining its virtual environment.

    Windows venv redirectors spawn another process, so their Popen handle cannot
    reliably identify or terminate the board writer during failed handoffs.
    """
    executable = sys.executable
    if os.name == "nt":
        executable = getattr(sys, "_base_executable", sys.executable) or sys.executable
        # CPython uses this marker for venv discovery without its redirector process:
        # https://github.com/python/cpython/blob/3.14/Modules/getpath.py
        environment["__PYVENV_LAUNCHER__"] = sys.executable
    return [executable, *arguments]


def _lock_path(path: pathlib.Path, suffix: str) -> pathlib.Path:
    return board_state.service_descriptor_path(path).with_suffix(suffix)


def _regular_file(path: pathlib.Path) -> None:
    if path.is_symlink() or path.is_junction():
        raise OSError("service ownership paths must not be symbolic links")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise OSError("service ownership paths must be regular files")


def _try_lock(descriptor: int) -> bool:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415 -- platform-specific lock implementation

        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True
    import fcntl  # noqa: PLC0415 -- platform-specific lock implementation

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt  # noqa: PLC0415 -- platform-specific lock implementation

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl  # noqa: PLC0415 -- platform-specific lock implementation

        fcntl.flock(descriptor, fcntl.LOCK_UN)


@dataclass
class WriterLease:
    """A held descriptor; release is idempotent and lock files are never unlinked."""

    descriptor: int | None

    def release(self) -> None:
        descriptor, self.descriptor = self.descriptor, None
        if descriptor is not None:
            try:
                _unlock(descriptor)
            finally:
                os.close(descriptor)


def _claim(path: pathlib.Path) -> WriterLease | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _regular_file(path)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _regular_file(path)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        if _try_lock(descriptor):
            return WriterLease(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    os.close(descriptor)
    return None


@contextlib.contextmanager
def writer_claim(path: pathlib.Path) -> Generator[WriterLease]:
    lease = _claim(_lock_path(path, ".writer.lock"))
    if lease is None:
        raise WriterAlreadyRunningError("another board service already owns this board")
    try:
        yield lease
    finally:
        lease.release()


def writer_busy(path: pathlib.Path) -> bool:
    """Probe OS ownership without trusting PIDs or stale service metadata."""
    lease = _claim(_lock_path(path, ".writer.lock"))
    if lease is None:
        return True
    lease.release()
    return False


@dataclass
class LaunchGate:
    """A held launch gate can issue one private, one-use child launch capability."""

    board: pathlib.Path
    token: str | None = None

    def child_environment(self) -> dict[str, str]:
        self.token = secrets.token_urlsafe(32)
        ticket = _lock_path(self.board, ".launch-token")
        _regular_file(ticket)
        descriptor = os.open(ticket, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump({"board": str(self.board.resolve()), "token": self.token}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        return {LAUNCH_MARKER_ENV: self.token}

    def cleanup(self) -> None:
        ticket = _lock_path(self.board, ".launch-token")
        if self.token is not None:
            with contextlib.suppress(OSError, ValueError, RecursionError):
                raw = _read_json(ticket, MAX_TICKET_BYTES)
                if isinstance(raw, dict) and raw.get("token") == self.token:
                    ticket.unlink(missing_ok=True)


@contextlib.contextmanager
def launch_locked(
    path: pathlib.Path, *, timeout: float = LOCK_WAIT_SECONDS
) -> Generator[LaunchGate]:
    lock = _lock_path(path, ".workspace.lock")
    deadline = time.monotonic() + timeout
    while (lease := _claim(lock)) is None:
        if time.monotonic() >= deadline:
            raise TimeoutError("timed out waiting for board service launch")
        time.sleep(POLL_SECONDS)
    gate = LaunchGate(path.resolve())
    try:
        yield gate
    finally:
        try:
            gate.cleanup()
        finally:
            lease.release()


def _read_json(path: pathlib.Path, limit: int) -> board_state.JsonValue:
    _regular_file(path)
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    return _decode_json(data, limit)


def _decode_json(data: bytes, limit: int) -> board_state.JsonValue:
    if len(data) > limit:
        raise ValueError("service metadata exceeds size limit")
    return cast("board_state.JsonValue", json.loads(data))


def _validate_launch(path: pathlib.Path, token: str) -> None:
    ticket = _lock_path(path, ".launch-token")
    raw = _read_json(ticket, MAX_TICKET_BYTES)
    saved_token = raw.get("token") if isinstance(raw, dict) else None
    if (
        not isinstance(raw, dict)
        or raw.get("board") != str(path.resolve())
        or not isinstance(saved_token, str)
        or not secrets.compare_digest(saved_token, token)
    ):
        raise OSError("invalid board launch capability")
    probe = _claim(_lock_path(path, ".workspace.lock"))
    if probe is not None:
        probe.release()
        raise OSError("board launch capability has no active parent gate")
    ticket.unlink()


def consume_launch(path: pathlib.Path) -> bool:
    """Consume only a valid canonical-board ticket while its parent retains the gate."""
    token = os.environ.pop(LAUNCH_MARKER_ENV, None)
    if token is None:
        return False
    try:
        _validate_launch(path, token)
    except (OSError, ValueError, RecursionError) as exc:
        raise OSError("invalid board launch capability") from exc
    return True


def read_service_metadata(path: pathlib.Path) -> board_state.JsonValue:
    """Read a bounded, regular canonical descriptor without trusting its contents."""
    return _read_json(board_state.service_descriptor_path(path), MAX_METADATA_BYTES)


def service_is_healthy(path: pathlib.Path) -> bool:
    """Recognize legacy correct-board listeners which predate OS ownership leases."""
    try:
        raw = read_service_metadata(path)
        if not isinstance(raw, dict):
            return False
        port, board = raw.get("port"), raw.get("board")
        if (
            raw.get("schema") != 1
            or raw.get("host") != "127.0.0.1"
            or type(port) is not int
            or not 0 < port < PORT_LIMIT
            or not isinstance(board, str)
            or pathlib.Path(board).resolve() != path.resolve()
        ):
            return False
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            connection.request("GET", board_state.API_PREFIX + "/browser-session")
            response = connection.getresponse()
            session = _decode_json(response.read(MAX_METADATA_BYTES + 1), MAX_METADATA_BYTES)
            identity = hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()
            return (
                response.status == HTTPStatus.OK
                and isinstance(session, dict)
                and session.get("boardIdentity") == identity
            )
        finally:
            connection.close()
    except OSError, ValueError, RecursionError, http.client.HTTPException:
        return False
