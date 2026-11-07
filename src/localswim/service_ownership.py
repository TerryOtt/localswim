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
        # https://github.com/python/cpython/blob/v3.15.0/Modules/getpath.py
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
        import msvcrt  # platform-specific lock implementation

        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True
    import fcntl  # platform-specific lock implementation

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt  # platform-specific lock implementation

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl  # platform-specific lock implementation

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
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("service metadata must be a regular file")
        data = stream.read(limit + 1)
    return _decode_json(data, limit)


def _decode_json(data: bytes, limit: int) -> board_state.JsonValue:
    if len(data) > limit:
        raise ValueError("service metadata exceeds size limit")
    try:
        return cast("board_state.JsonValue", json.loads(data))
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid service metadata JSON") from exc


def decode_service_metadata(data: bytes) -> board_state.JsonValue:
    """Decode only small rendezvous/session/status metadata, never board payloads."""
    return _decode_json(data, MAX_METADATA_BYTES)


def board_identity(path: pathlib.Path) -> str:
    """Identify the canonical board independently of its service port."""
    return hashlib.sha256(str(path.resolve()).casefold().encode("utf-8")).hexdigest()


def service_descriptor(
    path: pathlib.Path, *, require_cli_token: bool = False
) -> board_state.JsonObject:
    """Validate a board-bound endpoint, optionally requiring its CLI capability."""
    return _validate_service_descriptor(
        path, read_service_metadata(path), require_cli_token=require_cli_token
    )


def _validate_service_descriptor(
    path: pathlib.Path, raw: board_state.JsonValue, *, require_cli_token: bool = False
) -> board_state.JsonObject:
    if not isinstance(raw, dict):
        raise ValueError("invalid service descriptor")  # malformed JSON metadata
    port, board, token = raw.get("port"), raw.get("board"), raw.get("token")
    if (
        type(raw.get("schema")) is not int
        or raw.get("schema") != 1
        or raw.get("host") != "127.0.0.1"
        or type(port) is not int
        or not 0 < port < PORT_LIMIT
        or not isinstance(board, str)
        or not board
        or board_identity(pathlib.Path(board)) != board_identity(path)
        or (require_cli_token and (not isinstance(token, str) or not token))
    ):
        raise ValueError("invalid board-bound service descriptor")
    return raw


def _validate_launch(path: pathlib.Path, token: str) -> None:
    ticket = _lock_path(path, ".launch-token")
    raw = _read_json(ticket, MAX_TICKET_BYTES)
    saved_token = raw.get("token") if isinstance(raw, dict) else None
    if (
        not isinstance(raw, dict)
        or raw.get("board") != str(path.resolve())
        or not isinstance(saved_token, str)
        or not saved_token.isascii()
        or not token.isascii()
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


HEALTH_PHASE_CODES = {
    "descriptor_read": "descriptor_unreadable",
    "descriptor_validation": "descriptor_invalid",
    "monitor_capability_validation": "monitor_capability_invalid",
    "connect_request": "request_failed",
    "response_headers": "response_headers_failed",
    "response_read": "response_read_failed",
    "metadata_decode": "metadata_invalid",
    "metadata_validation": "metadata_invalid",
    "response_close": "response_close_failed",
    "http_status": "http_status_unexpected",
    "identity": "identity_mismatch",
    "complete": "healthy",
}
HEALTH_ERROR_TYPES = frozenset(
    {
        "missing",
        "permission",
        "timeout",
        "connection_refused",
        "connection_reset",
        "os_error",
        "http_error",
        "invalid_metadata",
        "recursion_error",
    }
)
HEALTH_PROBE_MODES = frozenset({"readiness", "lifecycle_identity", "legacy_readiness", "discovery"})


@dataclass(frozen=True)
class ServiceHealth:
    """Bounded diagnostic metadata, containing no descriptor or response content."""

    healthy: bool
    phase: str
    code: str
    elapsed_ms: int
    port: int | None = None
    http_status: int | None = None
    error_type: str | None = None
    probe_mode: str = "readiness"

    def __post_init__(self) -> None:
        if (
            type(self.healthy) is not bool
            or HEALTH_PHASE_CODES.get(self.phase) != self.code
            or self.healthy != (self.code == "healthy")
            or type(self.elapsed_ms) is not int
            or self.elapsed_ms < 0
            or (
                self.port is not None
                and (type(self.port) is not int or not 0 < self.port < PORT_LIMIT)
            )
            or (
                self.http_status is not None
                and (type(self.http_status) is not int or not 100 <= self.http_status <= 999)
            )
            or (self.error_type is not None and self.error_type not in HEALTH_ERROR_TYPES)
            or type(self.probe_mode) is not str
            or self.probe_mode not in HEALTH_PROBE_MODES
        ):
            raise ValueError("invalid service health metadata")

    def to_json(self) -> board_state.JsonObject:
        """Serialize only validated, explicitly selected diagnostic fields."""
        result: board_state.JsonObject = {
            "healthy": self.healthy,
            "phase": self.phase,
            "code": self.code,
            "elapsed_ms": self.elapsed_ms,
            "probe_mode": self.probe_mode,
        }
        for key, value in (
            ("port", self.port),
            ("http_status", self.http_status),
            ("error_type", self.error_type),
        ):
            if value is not None:
                result[key] = value
        return result


def _health_error_type(error: BaseException) -> str:
    if isinstance(error, FileNotFoundError):
        return "missing"
    if isinstance(error, PermissionError):
        return "permission"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(error, ConnectionResetError):
        return "connection_reset"
    if isinstance(error, http.client.HTTPException):
        return "http_error"
    if isinstance(error, RecursionError):
        return "recursion_error"
    if isinstance(error, ValueError):
        return "invalid_metadata"
    return "os_error"


def probe_service_health(path: pathlib.Path) -> ServiceHealth:
    """Identify the failed stage of the existing bounded correct-board GET probe."""
    return _probe_health(path, lifecycle=False)


def probe_monitor_lifecycle(path: pathlib.Path) -> ServiceHealth:
    """Probe advertised immutable identity, falling back only for legacy services."""
    return _probe_health(path, lifecycle=True)


def _probe_health(path: pathlib.Path, *, lifecycle: bool) -> ServiceHealth:
    started = time.monotonic()
    phase = "descriptor_read"
    probe_mode = "discovery" if lifecycle else "readiness"
    port: int | None = None
    http_status: int | None = None
    connection: http.client.HTTPConnection | None = None
    failure_type: str | None = None

    def result(error_type: str | None = None) -> ServiceHealth:
        return ServiceHealth(
            healthy=phase == "complete",
            phase=phase,
            code=HEALTH_PHASE_CODES[phase],
            elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            port=port,
            http_status=http_status,
            error_type=error_type,
            probe_mode=probe_mode,
        )

    try:
        try:
            raw = read_service_metadata(path)
        except ValueError, RecursionError:
            phase = "descriptor_validation"
            raise
        phase = "descriptor_validation"
        descriptor = _validate_service_descriptor(path, raw)
        port = cast("int", descriptor["port"])
        endpoint = board_state.API_PREFIX + "/browser-session"
        if lifecycle:
            phase = "monitor_capability_validation"
            if "monitorIdentityVersion" in descriptor:
                version = descriptor["monitorIdentityVersion"]
                if type(version) is not int or version != 1:
                    raise ValueError("invalid monitor identity capability")
                probe_mode = "lifecycle_identity"
                endpoint = board_state.API_PREFIX + "/monitor-identity"
            else:
                probe_mode = "legacy_readiness"
        phase = "connect_request"
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        connection.request("GET", endpoint)
        phase = "response_headers"
        response = connection.getresponse()
        http_status = response.status
        phase = "response_read"
        data = response.read(MAX_METADATA_BYTES + 1)
        phase = "metadata_decode"
        session = _decode_json(data, MAX_METADATA_BYTES)
        if http_status != HTTPStatus.OK:
            phase = "http_status"
        elif not _valid_probe_metadata(session, probe_mode):
            phase = "metadata_validation"
        elif cast("board_state.JsonObject", session).get("boardIdentity") != board_identity(path):
            phase = "identity"
        else:
            phase = "complete"
    except (OSError, ValueError, RecursionError, http.client.HTTPException) as error:
        failure_type = _health_error_type(error)
    finally:
        if connection is not None:
            try:
                connection.close()
            except (OSError, ValueError, RecursionError, http.client.HTTPException) as error:
                if phase == "complete":
                    phase = "response_close"
                    failure_type = _health_error_type(error)
    return result(failure_type)


def _valid_probe_metadata(metadata: board_state.JsonValue, probe_mode: str) -> bool:
    if not isinstance(metadata, dict) or not isinstance(metadata.get("boardIdentity"), str):
        return False
    return probe_mode != "lifecycle_identity" or (
        type(metadata.get("schema")) is int and metadata.get("schema") == 1
    )


def service_is_healthy(path: pathlib.Path) -> bool:
    """Recognize legacy correct-board listeners which predate OS ownership leases."""
    return probe_service_health(path).healthy
