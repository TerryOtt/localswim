"""Deterministic ownership checks for interrupted workspace relay transports."""

# ruff: noqa: SLF001 -- exercise the private relay transport ownership boundary

import http.client
import socket
import threading
from typing import TYPE_CHECKING, cast

import pytest

from localswim import workspace, workspace_events

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Callable


class ControlledSocket:
    def __init__(self) -> None:
        self.interrupted = threading.Event()
        self.shutdown_threads: list[int] = []

    def shutdown(self, how: int) -> None:
        assert how == socket.SHUT_RDWR
        self.shutdown_threads.append(threading.get_ident())
        self.interrupted.set()


class ControlledResponse:
    status = 200

    def __init__(self, transport: ControlledSocket) -> None:
        self.transport = transport
        self.reading = threading.Event()
        self.close_threads: list[int] = []

    def getheader(self, name: str, default: str = "") -> str:
        assert name == "Content-Type"
        assert default == ""
        return "text/event-stream"

    def readline(self, limit: int) -> bytes:
        assert limit > 0
        self.reading.set()
        if not self.transport.interrupted.wait(5):
            raise TimeoutError("test reader was not interrupted")
        return b""

    def close(self) -> None:
        self.close_threads.append(threading.get_ident())


class ControlledConnection:
    def __init__(self, *, pause_request: bool) -> None:
        self.transport = ControlledSocket()
        self.response = ControlledResponse(self.transport)
        self.pause_request = pause_request
        self.request_connected = threading.Event()
        self.sock: socket.socket | None = None
        self.response_class: object = None
        self.close_threads: list[int] = []
        self.request_count = 0

    def request(self, method: str, route: str) -> None:
        assert method == "GET"
        assert route == "/api/v001/events"
        self.request_count += 1
        self.sock = cast("socket.socket", self.transport)
        self.request_connected.set()
        if self.pause_request:
            # Connection establishment has happened, but request() has not returned
            # to let the relay publish _socket. Only socket interruption releases it.
            if not self.transport.interrupted.wait(5):
                raise TimeoutError("test request was not interrupted")
            raise OSError("test request interrupted")

    def getresponse(self) -> http.client.HTTPResponse:
        return cast("http.client.HTTPResponse", self.response)

    def close(self) -> None:
        self.close_threads.append(threading.get_ident())
        # Also release a failed old implementation so regression tests cannot leak
        # a non-daemon worker when it closes the connection from the wrong thread.
        self.transport.interrupted.set()
        self.sock = None


@pytest.mark.parametrize("pause_request", [False, True], ids=["blocked-read", "request-gap"])
@pytest.mark.parametrize("closer_count", [1, 2], ids=["one-closer", "concurrent-closers"])
def test_relay_reader_owns_cleanup_after_close(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pause_request: bool,
    closer_count: int,
) -> None:
    connection = ControlledConnection(pause_request=pause_request)

    def connect(host: str, port: int, *, timeout: float) -> http.client.HTTPConnection:
        assert (host, port, timeout) == ("127.0.0.1", 12345, 1.0)
        return cast("http.client.HTTPConnection", connection)

    monkeypatch.setattr(workspace_events.http.client, "HTTPConnection", connect)
    relay = workspace_events._BoardRelay(workspace.Workspace(tmp_path), "selected")
    relay._endpoint = workspace.Endpoint(12345, "", "")
    failures: list[BaseException] = []

    def checked(action: Callable[[], None]) -> None:
        try:
            action()
        except BaseException as exc:  # noqa: BLE001 -- assert worker failures in the test
            failures.append(exc)

    run = relay._run
    monkeypatch.setattr(relay, "_run", lambda: checked(run))
    close_ready = threading.Barrier(closer_count + 1, timeout=3)

    def close() -> None:
        close_ready.wait()
        relay.close()

    closers = [threading.Thread(target=checked, args=(close,)) for _ in range(closer_count)]
    relay.start()
    reader = relay._thread
    assert reader is not None
    try:
        ready = connection.request_connected if pause_request else connection.response.reading
        assert ready.wait(3), "relay never reached the controlled blocking operation"
        assert reader.is_alive()
        with relay._transport_lock:
            assert relay._connection is connection
            if pause_request:
                assert relay._socket is None
            else:
                assert relay._socket is connection.transport
        for closer in closers:
            closer.start()
        close_ready.wait()
        for closer in closers:
            closer.join(3)
        assert all(not closer.is_alive() for closer in closers), "relay close did not finish"
        assert not reader.is_alive(), "close returned before its reader stopped"
        assert not failures
        assert connection.request_count == 1
        assert connection.transport.shutdown_threads, "close must interrupt the actual socket"
        assert reader.ident not in connection.transport.shutdown_threads
        assert connection.close_threads == [reader.ident]
        expected_response_closers = [] if pause_request else [reader.ident]
        assert connection.response.close_threads == expected_response_closers
        with relay._transport_lock:
            assert relay._connection is relay._response is relay._socket is None
        relay.close()
        assert connection.close_threads == [reader.ident]
        assert connection.response.close_threads == expected_response_closers
    finally:
        relay._stop.set()
        connection.transport.interrupted.set()
        close_ready.abort()
        reader.join(3)
        for closer in closers:
            if closer.ident is not None:
                closer.join(3)
        relay.hub.close()
        assert not reader.is_alive()
        assert all(not closer.is_alive() for closer in closers)


@pytest.mark.parametrize("error_type", [OSError, ValueError, AttributeError])
def test_relay_closes_connection_when_response_cleanup_fails(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    connection = ControlledConnection(pause_request=False)
    original_error = error_type("injected response cleanup failure")

    def connect(host: str, port: int, *, timeout: float) -> http.client.HTTPConnection:
        assert (host, port, timeout) == ("127.0.0.1", 12345, 1.0)
        return cast("http.client.HTTPConnection", connection)

    def fail_close() -> None:
        connection.response.close_threads.append(threading.get_ident())
        raise original_error

    monkeypatch.setattr(workspace_events.http.client, "HTTPConnection", connect)
    monkeypatch.setattr(connection.response, "close", fail_close)
    relay = workspace_events._BoardRelay(workspace.Workspace(tmp_path), "selected")
    failures: list[BaseException] = []

    def read() -> None:
        try:
            with relay._upstream(workspace.Endpoint(12345, "", ""), "/events") as response:
                assert response is connection.response
        except BaseException as exc:  # noqa: BLE001 -- verify the exact original worker failure
            failures.append(exc)

    reader = threading.Thread(target=read)
    reader.start()
    try:
        reader.join(3)
        assert not reader.is_alive(), "cleanup failure kept the reader alive"
        assert failures == [original_error]
        assert connection.response.close_threads == [reader.ident]
        assert connection.close_threads == [reader.ident]
        with relay._transport_lock:
            assert relay._connection is relay._response is relay._socket is None
    finally:
        connection.transport.interrupted.set()
        reader.join(3)
        relay.hub.close()
        assert not reader.is_alive()
