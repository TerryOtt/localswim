"""Shared catalog, selected-board relay, parser and gateway SSE boundaries."""

import contextlib
import http.client
import http.server
import io
import json
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from localswim import browser_events, workspace, workspace_events
from tests.test_workspace import _finite_response, make_board

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Generator


STATUS = {
    "ok": True,
    "mtime": 10,
    "stamp": "00:00:10",
    "readAt": 10,
    "contentVersion": "abc",
    "revision": 1,
    "build": "test",
    "codeStale": False,
    "restarting": False,
    "push": {"enabled": False},
}


def take(mailbox: browser_events.BrowserEventMailbox, event: str) -> dict[str, object]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        frame = mailbox.get(timeout=0.1)
        if frame and frame.startswith(f"event: {event}\n".encode()):
            return cast("dict[str, object]", json.loads(frame.split(b"data: ", 1)[1]))
    raise AssertionError(f"missing {event} event")


@dataclass
class Upstream:
    server: http.server.ThreadingHTTPServer
    hub: browser_events.BrowserEventHub
    stopped: threading.Event
    accepted: threading.Event
    requests: list[tuple[str, str | None]]


@contextlib.contextmanager
def upstream(
    *,
    silent: bool = False,
    legacy: bool = False,
    upgrade_on_hint: bool = True,
    trickle_status: bool = False,
) -> Generator[Upstream]:
    hub = browser_events.BrowserEventHub()
    hub.publish("status", STATUS)
    stopped, accepted = threading.Event(), threading.Event()
    requests: list[tuple[str, str | None]] = []
    upgraded = not legacy

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            nonlocal upgraded
            requests.append((self.path, self.headers.get("Authorization")))
            accepted.set()
            if self.path == "/api/v001/status":
                upgraded = upgrade_on_hint
                body = b'{"build":"old","codeStale":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if trickle_status:
                    while not stopped.is_set():
                        try:
                            self.wfile.write(b" ")
                            self.wfile.flush()
                        except OSError:
                            return
                        stopped.wait(0.02)
                    return
                self.wfile.write(body)
                return
            if not upgraded:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if silent:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.flush()
                stopped.wait(5)
            else:
                browser_events.stream_browser_events(
                    self, hub.subscribe(), stopped, heartbeat_interval=0.05
                )

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Upstream(server, hub, stopped, accepted, requests)
    finally:
        stopped.set()
        hub.close()
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_catalog_is_lazy_cached_across_clients_and_wakes_after_commit(
    tmp_path: pathlib.Path,
) -> None:
    calls = 0

    def catalog() -> object:
        nonlocal calls
        calls += 1
        return {"boards": [], "revision": calls}

    events = workspace_events.WorkspaceEvents(workspace.Workspace(tmp_path), catalog)
    assert calls == 0
    first = events.subscribe(None)
    try:
        assert take(first, "catalog")["revision"] == 1
        second = events.subscribe(None)
        try:
            assert take(second, "catalog")["revision"] == 1
            assert calls == 1
            events.wake_catalog()
            assert take(first, "catalog")["revision"] == 2
            assert take(second, "catalog")["revision"] == 2
            assert calls == 2
        finally:
            events.unsubscribe(second)
    finally:
        events.unsubscribe(first)
        events.close()


def test_catalog_stays_idle_without_subscribers_and_refreshes_on_resume(
    tmp_path: pathlib.Path,
) -> None:
    sampled = threading.Event()
    calls = 0

    def catalog() -> object:
        nonlocal calls
        calls += 1
        sampled.set()
        return {"revision": calls}

    events = workspace_events.WorkspaceEvents(workspace.Workspace(tmp_path), catalog)
    first = events.subscribe(None)
    try:
        assert take(first, "catalog")["revision"] == 1
        events.unsubscribe(first)
        sampled.clear()
        events.wake_catalog()
        assert not sampled.wait(0.15)
        assert calls == 1
        resumed = events.subscribe(None)
        try:
            assert sampled.wait(1)
            value = take(resumed, "catalog")
            if value["revision"] == 1:
                value = take(resumed, "catalog")
            assert value["revision"] == 2
            assert calls == 2
        finally:
            events.unsubscribe(resumed)
    finally:
        events.unsubscribe(first)
        events.close()


def test_selected_relay_preserves_status_and_uses_one_upstream(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = workspace.Workspace(tmp_path)
    resolutions: list[str] = []
    with upstream() as source:

        def resolve(key: str) -> tuple[pathlib.Path, workspace.Endpoint]:
            resolutions.append(key)
            return tmp_path / "board.json", workspace.Endpoint(
                source.server.server_port, "secret", ""
            )

        monkeypatch.setattr(instance, "resolve_endpoint", resolve)
        events = workspace_events.WorkspaceEvents(instance, lambda: {"boards": []})
        mailbox = events.subscribe("board")
        try:
            assert take(mailbox, "board") == {"board": "board", "status": STATUS}
            assert resolutions == ["board"]
            assert source.requests == [("/api/v001/events", None)]
            source.hub.publish("status", {**STATUS, "revision": 2, "readAt": 11})
            assert cast("dict[str, object]", take(mailbox, "board")["status"])["readAt"] == 11
        finally:
            events.unsubscribe(mailbox)
            events.close()


def test_last_unsubscribe_interrupts_silent_upstream(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = workspace.Workspace(tmp_path)
    with upstream(silent=True) as source:

        def resolve(_key: str) -> tuple[pathlib.Path, workspace.Endpoint]:
            return (
                tmp_path / "board.json",
                workspace.Endpoint(source.server.server_port, "", ""),
            )

        monkeypatch.setattr(instance, "resolve_endpoint", resolve)
        events = workspace_events.WorkspaceEvents(instance, lambda: {})
        mailbox = events.subscribe("board")
        assert source.accepted.wait(2)
        started = time.monotonic()
        events.unsubscribe(mailbox)
        events.close()
        assert time.monotonic() - started < 1


def test_relay_reset_re_resolves_and_reseeds_latest_status(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_events, "RECONNECT_INTERVAL", 0.01)
    instance = workspace.Workspace(tmp_path)
    with upstream() as before, upstream() as after:
        resolutions = 0

        def resolve(_key: str) -> tuple[pathlib.Path, workspace.Endpoint]:
            nonlocal resolutions
            resolutions += 1
            source = before if resolutions == 1 else after
            return tmp_path / "board.json", workspace.Endpoint(source.server.server_port, "", "")

        monkeypatch.setattr(instance, "resolve_endpoint", resolve)
        events = workspace_events.WorkspaceEvents(instance, lambda: {})
        mailbox = events.subscribe("board")
        try:
            assert take(mailbox, "board")["status"] == STATUS
            after.hub.publish("status", {**STATUS, "build": "replacement", "revision": 0})
            before.stopped.set()
            before.hub.close()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                status = cast("dict[str, object]", take(mailbox, "board")["status"])
                if status.get("build") == "replacement":
                    break
            else:
                raise AssertionError("relay did not connect replacement")
            assert resolutions == 2
            assert after.requests == [("/api/v001/events", None)]
        finally:
            events.unsubscribe(mailbox)
            events.close()


@pytest.mark.parametrize("upgrade_on_hint", [True, False])
def test_legacy_writer_gets_one_status_upgrade_hint(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *, upgrade_on_hint: bool
) -> None:
    monkeypatch.setattr(workspace_events, "RECONNECT_INTERVAL", 0.01)
    instance = workspace.Workspace(tmp_path)
    with upstream(legacy=True, upgrade_on_hint=upgrade_on_hint) as source:

        def resolve(_key: str) -> tuple[pathlib.Path, workspace.Endpoint]:
            return tmp_path / "board.json", workspace.Endpoint(source.server.server_port, "", "")

        monkeypatch.setattr(instance, "resolve_endpoint", resolve)
        events = workspace_events.WorkspaceEvents(instance, lambda: {})
        mailbox = events.subscribe("board")
        try:
            deadline = time.monotonic() + 2
            if upgrade_on_hint:
                while time.monotonic() < deadline:
                    status = cast("dict[str, object]", take(mailbox, "board")["status"])
                    if status["ok"] is True:
                        break
                else:
                    raise AssertionError("upgraded writer did not stream status")
            else:
                while time.monotonic() < deadline:
                    if sum(path == "/api/v001/events" for path, _ in source.requests) >= 3:
                        break
                    time.sleep(0.01)
                else:
                    raise AssertionError("relay did not retry the unavailable event route")
            assert sum(path == "/api/v001/status" for path, _ in source.requests) == 1
        finally:
            events.unsubscribe(mailbox)
            events.close()


def test_legacy_upgrade_hint_trickling_body_is_cancelled(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = workspace.Workspace(tmp_path)
    with upstream(legacy=True, trickle_status=True) as source:

        def resolve(_key: str) -> tuple[pathlib.Path, workspace.Endpoint]:
            return tmp_path / "board.json", workspace.Endpoint(source.server.server_port, "", "")

        monkeypatch.setattr(instance, "resolve_endpoint", resolve)
        events = workspace_events.WorkspaceEvents(instance, lambda: {})
        mailbox = events.subscribe("board")
        try:
            deadline = time.monotonic() + 2
            while not any(path == "/api/v001/status" for path, _ in source.requests):
                assert time.monotonic() < deadline
                time.sleep(0.01)
            started = time.monotonic()
            events.unsubscribe(mailbox)
            assert time.monotonic() - started < 1
        finally:
            events.close()


@pytest.mark.parametrize(
    "query",
    [
        "board=",
        "token=secret",
        "board=../board",
        "board=.hidden/board",
        "board=a%5Cb",
        "board=attachments/board",
        "board=C%3A/board",
        "board=" + "a" * 1025,
        "&".join("board=a" for _ in range(17)),
    ],
)
def test_selected_board_query_boundaries(query: str) -> None:
    with pytest.raises(ValueError, match=r"board|fields|parameter"):
        workspace_events.selected_board(query)


def test_selected_board_query_catalog_only_and_one_board() -> None:
    assert workspace_events.selected_board("") is None
    assert workspace_events.selected_board("board=project%2Fboard") == "project/board"
    with pytest.raises(ValueError, match="fields"):
        workspace_events.selected_board("board=a&board=a")


def test_upstream_reader_handles_chunked_frames_and_ignores_heartbeats() -> None:
    raw = b'event: heartbeat\ndata: {}\n\nevent: status\ndata: {"ok":true,"readAt":10}\n\n'
    body = b"".join(f"{len(part):x}\r\n".encode() + part + b"\r\n" for part in [raw[:19], raw[19:]])
    response = _finite_response(b"Transfer-Encoding: chunked\r\n", body + b"0\r\n\r\n")
    response.fp = io.BufferedReader(response.fp)
    with response:
        assert list(workspace_events.upstream_events(response)) == [{"ok": True, "readAt": 10}]


@pytest.mark.parametrize(
    "frame",
    [
        b"event: status\ndata: " + b"x" * 65536,
        b'event: status\ndata: {"ok":1}\n\n',
        b'event: board\ndata: {"ok":true}\n\n',
        b'event: status\nevent: status\ndata: {"ok":true}\n\n',
    ],
)
def test_upstream_reader_refuses_oversized_or_malformed_frames(frame: bytes) -> None:
    response = _finite_response(b"", frame)
    with response, pytest.raises(ValueError, match="board"):
        list(workspace_events.upstream_events(response))


def test_gateway_public_catalog_events_and_auth_boundaries(tmp_path: pathlib.Path) -> None:
    instance = workspace.Workspace(tmp_path)
    server = workspace.WorkspaceServer(("127.0.0.1", 0), instance)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stream = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        stream.request("GET", workspace.PREFIX + "/events")
        response = stream.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        assert response.readline() == b"event: catalog\n"
        data = json.loads(response.readline().removeprefix(b"data: "))
        assert data["identity"] == instance.identity
        assert data["build"]
        assert "codeStale" in data
        assert "token" not in data
        asset = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            asset.request("GET", "/browser_stream.js")
            asset_response = asset.getresponse()
            assert asset_response.status == 200
            assert asset_response.getheader("Content-Type") == "text/javascript; charset=utf-8"
            assert b"EventSource" in asset_response.read()
        finally:
            asset.close()
        for route, expected in [("?token=secret", 400), ("?board=unknown", 404)]:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            try:
                connection.request("GET", workspace.PREFIX + "/events" + route)
                refused = connection.getresponse()
                assert refused.status == expected
                refused.read()
            finally:
                connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("GET", workspace.BOARD_PREFIX + "unknown/events")
            refused = connection.getresponse()
            assert refused.status == 404
            assert json.loads(refused.read()) == {"error": "unknown workspace route"}
        finally:
            connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("POST", workspace.PREFERENCES_ROUTE, body=b"{}")
            assert connection.getresponse().status == 401
        finally:
            connection.close()
    finally:
        # Leave a live reader connected to prove server_close wakes it before joining.
        server.shutdown()
        started = time.monotonic()
        server.server_close()
        assert time.monotonic() - started < 2
        stream.close()
        thread.join(2)


def test_unknown_selection_does_not_launch_a_hidden_or_escaped_board(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_board(tmp_path / ".private" / "board.json", "Private")
    instance = workspace.Workspace(tmp_path)
    events = workspace_events.WorkspaceEvents(instance, lambda: {})
    starts: list[object] = []

    def endpoint(candidate: object) -> None:
        starts.append(candidate)

    monkeypatch.setattr(instance, "_endpoint", endpoint)
    try:
        with pytest.raises(ValueError, match="unknown workspace board"):
            events.subscribe(".private/board")
        assert starts == []
        assert not events._sessions  # noqa: SLF001
    finally:
        events.close()
