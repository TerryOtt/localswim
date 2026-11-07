"""Early POST refusals remain readable without replaying or parsing late input."""

# ruff: noqa: SLF001 -- verify command gate release before transport cleanup

import http.client
import http.server
import io
import socket
import threading
from collections.abc import Generator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import pytest

from localswim import api_endpoint, workspace


@contextmanager
def refusal_server(kind: str) -> Generator[tuple[int, list[str]]]:
    """Serve real production dispatch with no board or persistent workspace."""
    calls: list[str] = []

    class BoardHandler(api_endpoint.Handler):
        def do_POST(self) -> None:
            calls.append("post")
            if kind == "gated-board":
                with api_endpoint._command_gate:
                    self._json({"error": "refused under command gate"}, 409)
                return
            super().do_POST()

        def _read_json(self, *, unique_fields: bool = False) -> None:  # noqa: ARG002 -- override signature
            calls.append("parse")
            pytest.fail("refused input reached JSON parsing")

        def do_GET(self) -> None:
            calls.append("get")
            pytest.fail("late input was dispatched as another request")

    class WorkspaceHandler(workspace.WorkspaceHandler):
        @property
        def workspace(self) -> workspace.Workspace:
            return cast("workspace.Workspace", SimpleNamespace(token=__name__))

        def do_POST(self) -> None:
            calls.append("post")
            super().do_POST()

        def do_GET(self) -> None:
            calls.append("get")
            pytest.fail("late input was dispatched as another request")

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = False

        def handle_error(self, request: object, client_address: object) -> None:  # noqa: ARG002 -- override signature
            calls.append("handler-error")

    handler = WorkspaceHandler if kind == "workspace" else BoardHandler
    server = Server(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield server.server_port, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize("kind", ["board", "workspace"])
@pytest.mark.parametrize("credential", [None, "Bearer invalid"])
def test_early_auth_refusal_survives_late_post_body(kind: str, credential: str | None) -> None:
    """Read the refusal first, then send an unparsed body and pipelined request once."""
    route = (
        api_endpoint.API_PREFIX + "/cards"
        if kind == "board"
        else workspace.BOARD_PREFIX + "unused/cards"
    )
    late_body = b"not-json\r\nGET / HTTP/1.1\r\nHost: localhost\r\n\r\n"
    authorization = "" if credential is None else f"Authorization: {credential}\r\n"
    request = (
        f"POST {route} HTTP/1.1\r\nHost: localhost\r\n"
        f"Content-Length: {len(late_body)}\r\n{authorization}\r\n"
    ).encode("ascii")
    with refusal_server(kind) as (port, calls):
        with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
            connection.sendall(request)
            response = http.client.HTTPResponse(connection)
            try:
                response.begin()
                assert response.status == 401
                body = response.read()
                assert body == (
                    b'{"error": "missing or invalid service credential"}'
                    if kind == "board"
                    else b'{"error": "missing or invalid workspace credential"}'
                )
                connection.sendall(late_body)
                connection.shutdown(socket.SHUT_WR)
                assert connection.recv(1) == b""
            finally:
                response.close()
        assert calls == ["post"]


def fake_handler(connection: Mock, *, command: str = "POST") -> http.server.BaseHTTPRequestHandler:
    return cast(
        "http.server.BaseHTTPRequestHandler",
        SimpleNamespace(command=command, connection=connection, wfile=io.BytesIO()),
    )


def test_refusal_drain_has_hard_byte_limit_and_restores_timeout() -> None:
    connection = Mock()
    connection.gettimeout.return_value = 30.0

    def full_chunk(size: int) -> bytes:
        return b"x" * size

    connection.recv.side_effect = full_chunk
    handler = fake_handler(connection)
    with patch.object(api_endpoint.time, "monotonic", return_value=0):
        api_endpoint.finish_refused_post(handler)
    assert handler.close_connection is True
    connection.shutdown.assert_called_once_with(socket.SHUT_WR)
    assert connection.recv.call_count == 8
    assert sum(call.args[0] for call in connection.recv.call_args_list) == 64 * 1024
    assert connection.settimeout.call_args_list[-1].args == (30.0,)


def test_refusal_drain_uses_absolute_deadline_across_reads() -> None:
    connection = Mock()
    connection.gettimeout.return_value = None
    connection.recv.return_value = b"x"
    with patch.object(api_endpoint.time, "monotonic", side_effect=[0.0, 0.05, 0.15, 0.21]):
        api_endpoint.finish_refused_post(fake_handler(connection))
    assert connection.recv.call_count == 2
    timeouts = [call.args[0] for call in connection.settimeout.call_args_list]
    assert timeouts[:2] == pytest.approx([0.15, 0.05])
    assert timeouts[-1] is None


@pytest.mark.parametrize("error", [TimeoutError(), ConnectionResetError(), OSError()])
def test_refusal_drain_socket_error_is_contained_and_restores_timeout(error: OSError) -> None:
    connection = Mock()
    connection.gettimeout.return_value = 30.0
    connection.recv.side_effect = error
    api_endpoint.finish_refused_post(fake_handler(connection))
    connection.recv.assert_called_once()
    assert connection.settimeout.call_args_list[-1].args == (30.0,)


def test_refusal_drain_stops_at_peer_eof() -> None:
    connection = Mock()
    connection.gettimeout.return_value = 30.0
    connection.recv.return_value = b""
    api_endpoint.finish_refused_post(fake_handler(connection))
    connection.recv.assert_called_once()
    assert connection.settimeout.call_args_list[-1].args == (30.0,)


def test_refusal_drain_does_not_touch_non_post_connections() -> None:
    connection = Mock()
    api_endpoint.finish_refused_post(fake_handler(connection, command="GET"))
    assert connection.mock_calls == []


def test_board_refusal_drain_runs_after_command_gate_is_released() -> None:
    """Transport cleanup must not hold the global mutation lock for its drain."""
    acquired: list[bool] = []
    finish = api_endpoint.finish_refused_post

    def inspect_gate(handler: http.server.BaseHTTPRequestHandler) -> None:
        available = api_endpoint._command_gate.acquire(blocking=False)
        acquired.append(available)
        if available:
            api_endpoint._command_gate.release()
        finish(handler)

    with patch.object(api_endpoint, "finish_refused_post", side_effect=inspect_gate):
        with refusal_server("gated-board") as (port, calls):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                connection.request("POST", api_endpoint.API_PREFIX + "/cards", body=b"")
                with connection.getresponse() as response:
                    assert response.status == 409
                    assert response.read() == b'{"error": "refused under command gate"}'
            finally:
                connection.close()
        assert calls == ["post"]
    assert acquired == [True]


def test_board_unknown_route_refusal_survives_late_post_body() -> None:
    """Base-handler HTML refusals receive the same transport cleanup as JSON."""
    with refusal_server("board") as (port, calls):
        with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
            connection.sendall(
                b"POST /not-a-route HTTP/1.1\r\nHost: localhost\r\nContent-Length: 8\r\n\r\n"
            )
            response = http.client.HTTPResponse(connection)
            try:
                response.begin()
                assert response.status == 404
                assert b"Error code: 404" in response.read()
                connection.sendall(b"not-json")
                connection.shutdown(socket.SHUT_WR)
                assert connection.recv(1) == b""
            finally:
                response.close()
        assert calls == ["post"]
