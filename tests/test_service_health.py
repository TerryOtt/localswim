"""The shared health probe reports bounded stages without leaking private inputs."""

import dataclasses
import http.client
import json
from typing import TYPE_CHECKING

import pytest

from localswim import board_state, service_ownership

if TYPE_CHECKING:
    import pathlib

SENTINEL = "private-board-token-response-exception-sentinel"


def write_descriptor(path: pathlib.Path, *, monitor: bool = False) -> None:
    descriptor = board_state.service_descriptor_path(path)
    descriptor.parent.mkdir(parents=True, exist_ok=True)
    metadata: board_state.JsonObject = {
        "schema": 1,
        "host": "127.0.0.1",
        "port": 12345,
        "board": str(path),
        "token": SENTINEL,
    }
    if monitor:
        metadata["monitorIdentityVersion"] = 1
    descriptor.write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )


class ProbeConnection:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.stage: str | None = None
        self.error: Exception = OSError(SENTINEL)
        self.closed = False
        self.requests: list[tuple[str, str]] = []
        self.read_limits: list[int] = []

    def request(self, method: str, endpoint: str) -> None:
        self.requests.append((method, endpoint))
        if self.stage == "connect_request":
            raise self.error

    def getresponse(self) -> ProbeConnection:
        if self.stage == "response_headers":
            raise self.error
        return self

    def read(self, limit: int) -> bytes:
        self.read_limits.append(limit)
        if self.stage == "response_read":
            raise self.error
        return self.body[:limit]

    def close(self) -> None:
        self.closed = True
        if self.stage == "response_close":
            raise self.error


def install_connection(monkeypatch: pytest.MonkeyPatch, connection: ProbeConnection) -> None:
    def connect(host: str, port: int, *, timeout: int) -> ProbeConnection:
        assert (host, port, timeout) == ("127.0.0.1", 12345, 1)
        return connection

    monkeypatch.setattr(service_ownership.http.client, "HTTPConnection", connect)


def assert_private(result: service_ownership.ServiceHealth, path: pathlib.Path) -> None:
    serialized = json.dumps(result.to_json())
    assert SENTINEL not in serialized
    assert str(path) not in serialized
    assert "boardIdentity" not in serialized
    assert set(result.to_json()) <= {
        "healthy",
        "phase",
        "code",
        "elapsed_ms",
        "port",
        "http_status",
        "error_type",
        "probe_mode",
    }
    assert result.elapsed_ms >= 0


def test_success_is_frozen_private_and_preserves_probe_contract(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / SENTINEL / "board.json"
    write_descriptor(path)
    connection = ProbeConnection(
        json.dumps(
            {
                "boardIdentity": service_ownership.board_identity(path),
                "token": SENTINEL,
            }
        ).encode()
    )
    install_connection(monkeypatch, connection)
    reads = 0
    original = service_ownership.read_service_metadata

    def read_once(board: pathlib.Path) -> board_state.JsonValue:
        nonlocal reads
        reads += 1
        return original(board)

    monkeypatch.setattr(service_ownership, "read_service_metadata", read_once)
    result = service_ownership.probe_service_health(path)
    assert result.healthy
    assert result.probe_mode == "readiness"
    assert (result.phase, result.code, result.port, result.http_status) == (
        "complete",
        "healthy",
        12345,
        200,
    )
    assert reads == 1
    assert connection.requests == [("GET", "/api/v001/browser-session")]
    assert connection.read_limits == [service_ownership.MAX_METADATA_BYTES + 1]
    assert connection.closed
    assert "error_type" not in result.to_json()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.healthy = False  # pyright: ignore[reportAttributeAccessIssue]
    assert_private(result, path)
    assert service_ownership.service_is_healthy(path) is True


@pytest.mark.parametrize("lifecycle", [False, True])
@pytest.mark.parametrize(
    "error,expected",
    [
        (FileNotFoundError(SENTINEL), "missing"),
        (PermissionError(SENTINEL), "permission"),
        (OSError(SENTINEL), "os_error"),
        (ValueError(SENTINEL), "invalid_metadata"),
        (RecursionError(SENTINEL), "recursion_error"),
    ],
)
def test_descriptor_failures_distinguish_read_and_validation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: str,
    lifecycle: bool,
) -> None:
    path = tmp_path / SENTINEL

    def fail(_path: pathlib.Path) -> board_state.JsonValue:
        raise error

    monkeypatch.setattr(service_ownership, "read_service_metadata", fail)
    probe = (
        service_ownership.probe_monitor_lifecycle
        if lifecycle
        else service_ownership.probe_service_health
    )
    result = probe(path)
    validation = isinstance(error, (ValueError, RecursionError))
    assert result.phase == ("descriptor_validation" if validation else "descriptor_read")
    assert result.code == ("descriptor_invalid" if validation else "descriptor_unreadable")
    assert result.error_type == expected
    assert not result.healthy
    assert "port" not in result.to_json()
    assert_private(result, path)
    assert service_ownership.service_is_healthy(path) is False


@pytest.mark.parametrize("lifecycle", [False, True])
@pytest.mark.parametrize(
    "payload",
    [
        b"[",
        b"[]",
        b"\xff",
        b" " * 65537,
        b"[" * 2000 + b"]" * 2000,
        json.dumps({"host": SENTINEL, "token": SENTINEL}).encode(),
    ],
)
def test_invalid_descriptors_never_contact_endpoint(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    lifecycle: bool,
) -> None:
    path = tmp_path / SENTINEL
    descriptor = board_state.service_descriptor_path(path)
    descriptor.write_bytes(payload)
    connection = ProbeConnection(b"{}")
    install_connection(monkeypatch, connection)
    probe = (
        service_ownership.probe_monitor_lifecycle
        if lifecycle
        else service_ownership.probe_service_health
    )
    result = probe(path)
    assert (result.phase, result.code) == ("descriptor_validation", "descriptor_invalid")
    assert not connection.requests
    assert_private(result, path)


@pytest.mark.parametrize("lifecycle", [False, True])
@pytest.mark.parametrize("stage", ["connect_request", "response_headers", "response_read"])
@pytest.mark.parametrize(
    "error,expected",
    [
        (TimeoutError(SENTINEL), "timeout"),
        (ConnectionRefusedError(SENTINEL), "connection_refused"),
        (ConnectionResetError(SENTINEL), "connection_reset"),
        (OSError(SENTINEL), "os_error"),
        (http.client.IncompleteRead(SENTINEL.encode()), "http_error"),
    ],
)
def test_transport_failures_are_bounded_and_private(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    error: Exception,
    expected: str,
    lifecycle: bool,
) -> None:
    path = tmp_path / SENTINEL
    write_descriptor(path, monitor=lifecycle)
    connection = ProbeConnection(b"{}")
    connection.stage, connection.error = stage, error
    install_connection(monkeypatch, connection)
    probe = (
        service_ownership.probe_monitor_lifecycle
        if lifecycle
        else service_ownership.probe_service_health
    )
    result = probe(path)
    assert result.phase == stage
    assert result.error_type == expected
    assert not result.healthy
    assert connection.closed
    assert len(connection.requests) == 1
    assert result.http_status == (200 if stage == "response_read" else None)
    assert_private(result, path)


@pytest.mark.parametrize("lifecycle", [False, True])
@pytest.mark.parametrize(
    "payload,status,phase,code",
    [
        (b"[", 200, "metadata_decode", "metadata_invalid"),
        (b"\xff", 200, "metadata_decode", "metadata_invalid"),
        (b" " * 65537, 200, "metadata_decode", "metadata_invalid"),
        (b"[" * 2000, 200, "metadata_decode", "metadata_invalid"),
        (b"{}", 503, "http_status", "http_status_unexpected"),
        (b"[]", 200, "metadata_validation", "metadata_invalid"),
        (b"{}", 200, "metadata_validation", "metadata_invalid"),
        (b'{"boardIdentity":123}', 200, "metadata_validation", "metadata_invalid"),
        (
            json.dumps({"schema": 1, "boardIdentity": SENTINEL, "token": SENTINEL}).encode(),
            200,
            "identity",
            "identity_mismatch",
        ),
    ],
)
def test_response_failures_preserve_status_and_hide_payload(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    status: int,
    phase: str,
    code: str,
    lifecycle: bool,
) -> None:
    path = tmp_path / SENTINEL
    write_descriptor(path, monitor=lifecycle)
    connection = ProbeConnection(payload, status)
    install_connection(monkeypatch, connection)
    probe = (
        service_ownership.probe_monitor_lifecycle
        if lifecycle
        else service_ownership.probe_service_health
    )
    result = probe(path)
    assert (result.phase, result.code, result.http_status) == (phase, code, status)
    assert not result.healthy
    assert connection.closed
    assert_private(result, path)


def test_health_rejects_unallowlisted_diagnostic_strings() -> None:
    with pytest.raises(ValueError, match="invalid service health metadata"):
        service_ownership.ServiceHealth(False, SENTINEL, SENTINEL, 0)
    with pytest.raises(ValueError, match="invalid service health metadata"):
        service_ownership.ServiceHealth(
            False, "descriptor_read", "descriptor_unreadable", 0, error_type=SENTINEL
        )
    with pytest.raises(ValueError, match="invalid service health metadata"):
        service_ownership.ServiceHealth(
            False, "descriptor_read", "descriptor_unreadable", 0, probe_mode=SENTINEL
        )


@pytest.mark.parametrize("lifecycle", [False, True])
@pytest.mark.parametrize("error", [OSError(SENTINEL), http.client.HTTPException(SENTINEL)])
@pytest.mark.parametrize("healthy_body", [True, False])
def test_close_failure_rejects_success_and_preserves_earlier_failure(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    healthy_body: bool,
    lifecycle: bool,
) -> None:
    path = tmp_path / SENTINEL
    write_descriptor(path, monitor=lifecycle)
    body = json.dumps(
        {"schema": 1, "boardIdentity": service_ownership.board_identity(path)}
    ).encode()
    connection = ProbeConnection(body if healthy_body else b"[")
    connection.stage, connection.error = "response_close", error
    install_connection(monkeypatch, connection)
    probe = (
        service_ownership.probe_monitor_lifecycle
        if lifecycle
        else service_ownership.probe_service_health
    )
    result = probe(path)
    assert not result.healthy
    assert result.phase == ("response_close" if healthy_body else "metadata_decode")
    assert result.code == ("response_close_failed" if healthy_body else "metadata_invalid")
    assert_private(result, path)
    assert service_ownership.service_is_healthy(path) is False


@pytest.mark.parametrize("advertised", [False, True])
def test_monitor_selects_capability_without_retry(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, advertised: bool
) -> None:
    path = tmp_path / SENTINEL
    write_descriptor(path)
    descriptor = board_state.service_descriptor_path(path)
    if advertised:
        raw = json.loads(descriptor.read_text(encoding="utf-8"))
        raw["monitorIdentityVersion"] = 1
        descriptor.write_text(json.dumps(raw), encoding="utf-8")
    connection = ProbeConnection(
        json.dumps({"schema": 1, "boardIdentity": service_ownership.board_identity(path)}).encode()
    )
    install_connection(monkeypatch, connection)
    result = service_ownership.probe_monitor_lifecycle(path)
    assert result.healthy
    assert result.probe_mode == ("lifecycle_identity" if advertised else "legacy_readiness")
    endpoint = "monitor-identity" if advertised else "browser-session"
    assert connection.requests == [("GET", "/api/v001/" + endpoint)]
    assert connection.read_limits == [service_ownership.MAX_METADATA_BYTES + 1]
    assert connection.closed
    assert_private(result, path)


@pytest.mark.parametrize("version", [None, True, False, 0, 2, "1", {}, []])
def test_invalid_monitor_capability_does_not_change_readiness(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, version: board_state.JsonValue
) -> None:
    path = tmp_path / SENTINEL
    write_descriptor(path)
    descriptor = board_state.service_descriptor_path(path)
    raw = json.loads(descriptor.read_text(encoding="utf-8"))
    raw["monitorIdentityVersion"] = version
    descriptor.write_text(json.dumps(raw), encoding="utf-8")
    connection = ProbeConnection(
        json.dumps({"boardIdentity": service_ownership.board_identity(path)}).encode()
    )
    install_connection(monkeypatch, connection)
    result = service_ownership.probe_monitor_lifecycle(path)
    assert not result.healthy
    assert (result.phase, result.code, result.probe_mode) == (
        "monitor_capability_validation",
        "monitor_capability_invalid",
        "discovery",
    )
    assert result.error_type == "invalid_metadata"
    assert not connection.requests
    assert_private(result, path)
    assert service_ownership.probe_service_health(path).healthy
    assert service_ownership.service_is_healthy(path)
    assert connection.requests == [("GET", "/api/v001/browser-session")] * 2


@pytest.mark.parametrize("schema", [None, True, False, 0, 2, "1", {}, []])
def test_monitor_identity_requires_exact_schema(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, schema: board_state.JsonValue
) -> None:
    path = tmp_path / SENTINEL
    write_descriptor(path)
    descriptor = board_state.service_descriptor_path(path)
    raw = json.loads(descriptor.read_text(encoding="utf-8"))
    raw["monitorIdentityVersion"] = 1
    descriptor.write_text(json.dumps(raw), encoding="utf-8")
    connection = ProbeConnection(
        json.dumps(
            {"schema": schema, "boardIdentity": service_ownership.board_identity(path)}
        ).encode()
    )
    install_connection(monkeypatch, connection)
    result = service_ownership.probe_monitor_lifecycle(path)
    assert not result.healthy
    assert (result.phase, result.code, result.probe_mode) == (
        "metadata_validation",
        "metadata_invalid",
        "lifecycle_identity",
    )
    assert connection.requests == [("GET", "/api/v001/monitor-identity")]
    assert_private(result, path)


def test_monitor_discovery_failure_is_unresolved(tmp_path: pathlib.Path) -> None:
    result = service_ownership.probe_monitor_lifecycle(tmp_path / SENTINEL)
    assert not result.healthy
    assert result.probe_mode == "discovery"
    assert result.phase == "descriptor_read"
    assert_private(result, tmp_path / SENTINEL)
