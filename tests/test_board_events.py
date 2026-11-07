"""Real board SSE transport, disk reconciliation and writer isolation."""

# ruff: noqa: SLF001 -- verify producer and ownership lifetime boundaries

import http.client
import http.server
import json
import os
import pathlib
import socket
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest

from localswim import api_endpoint, board_state, browser_events
from tests.support import USERS

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


@dataclass
class BoardService:
    path: pathlib.Path
    store: api_endpoint.BoardStore
    server: api_endpoint.BoardHttpServer
    clients: list[tuple[http.client.HTTPConnection, http.client.HTTPResponse]]

    def events(self) -> http.client.HTTPResponse:
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        client.request("GET", api_endpoint.API_PREFIX + "/events")
        response = client.getresponse()
        self.clients.append((client, response))
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        assert response.getheader("Content-Length") is None
        return response


@pytest.fixture
def service(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[BoardService]:
    path = tmp_path / "board.json"
    board_state.save(
        board_state.Board(
            project="API", users=USERS, browser_user="terry", cli_user="bot", default_owner="bot"
        ),
        path,
    )
    store = api_endpoint.BoardStore(path)
    monkeypatch.setattr(api_endpoint, "BOARD_PATH", path)
    monkeypatch.setattr(api_endpoint, "STORE", store)

    # Concurrent source edits in this worktree must not initiate a code restart.
    # Keep the actual disk admission, error containment and metadata builder intact.
    def fixed_source(_server: http.server.HTTPServer) -> board_state.JsonObject:
        return {"build": "test", "codeStale": False, "restarting": False}

    monkeypatch.setattr(api_endpoint, "source_status", fixed_source)
    server = api_endpoint.BoardHttpServer(("127.0.0.1", 0), api_endpoint.Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    running = BoardService(path, store, server, [])
    try:
        yield running
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        for client, response in running.clients:
            response.close()
            client.close()
        assert not thread.is_alive()


def status(response: http.client.HTTPResponse) -> dict[str, object]:
    """Read exactly one bounded status event from the real HTTP stream."""
    for _ in range(100):
        line = response.readline(65537)
        assert line, "event stream closed before status"
        assert len(line) <= 65536
        if line == b"event: status\n":
            data = response.readline(65537)
            assert data.startswith(b"data: ") and len(data) <= 65536
            assert response.readline(2) == b"\n"
            return cast("dict[str, object]", json.loads(data[6:]))
    raise AssertionError("status event missing from bounded stream read")


def wait_for(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, "bounded board event wait expired"
        time.sleep(0.002)


def status_until(
    response: http.client.HTTPResponse, predicate: Callable[[dict[str, object]], bool]
) -> dict[str, object]:
    """Allow an already in-flight periodic sample without an unbounded retry."""
    for _ in range(20):
        sample = status(response)
        if predicate(sample):
            return sample
    raise AssertionError("expected disk status absent from bounded samples")


def test_first_request_starts_one_producer_and_later_subscriber_gets_cache(
    service: BoardService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = service.server.browser_event_producer
    assert producer is not None and producer._interval == 5.0
    assert producer._thread is None
    calls: list[int] = []
    original = service.store.disk_status

    def counted() -> tuple[str | None, float, str, float | None, str | None, int]:
        calls.append(threading.get_ident())
        return original()

    monkeypatch.setattr(service.store, "disk_status", counted)
    first = status(service.events())
    assert first["ok"] is True and first["revision"] == 0
    worker = producer._thread
    assert worker is not None and worker.is_alive() and not worker.daemon
    assert status(service.events()) == first
    assert calls == [worker.ident]
    assert service.server.browser_event_hub is not None
    assert service.server.browser_event_hub.subscriber_count == 2


@pytest.mark.parametrize("actor", ["bot", "terry"])
def test_committed_edits_wake_shared_status_for_either_actor(
    service: BoardService,
    actor: str,
) -> None:
    response = service.events()
    first = status(response)
    result, revision = service.store.execute(
        0, lambda board: board.create("new", "New", "backlog", actor), notification_actor=actor
    )
    assert result and revision == 1
    updated = status(response)
    assert updated["ok"] is True and updated["revision"] == 1
    assert updated["contentVersion"] != first["contentVersion"]
    # A reconnect observes the latest cached sample without another disk read.
    assert status(service.events()) == updated


def test_periodic_status_detects_timestamp_preserving_edit_and_corrupt_recovery(
    service: BoardService,
) -> None:
    producer = service.server.browser_event_producer
    assert producer is not None
    # Exercise the actual periodic path with a shorter test interval.
    producer._interval = 0.08
    response = service.events()
    first = status(response)
    stamp = service.path.stat()
    original = service.path.read_bytes()
    replacement = original.replace(b'"project": "API"', b'"project": "New"')
    assert replacement != original and len(replacement) == len(original)
    service.path.write_bytes(replacement)
    os.utime(service.path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    changed = status_until(
        response, lambda sample: sample.get("contentVersion") != first["contentVersion"]
    )
    assert changed["ok"] is True and changed["revision"] == first["revision"]
    assert changed["mtime"] == first["mtime"]
    assert changed["contentVersion"] != first["contentVersion"]
    assert service.store._board.project == "New"
    service.path.write_text("{", encoding="utf-8")
    broken = status_until(response, lambda sample: sample["ok"] is False)
    assert broken["ok"] is False and broken["stamp"] == "unreadable"
    assert cast(float, broken["readAt"]) >= cast(float, changed["readAt"])
    assert broken["error"]
    assert service.store._board.project == "New"
    service.path.write_bytes(original)
    recovered = status_until(response, lambda sample: sample["ok"] is True)
    assert recovered["ok"] is True and "error" not in recovered
    assert recovered["contentVersion"] == first["contentVersion"]
    assert service.store._board.project == "API"


def test_heartbeat_and_additional_client_do_not_read_board(
    service: BoardService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = browser_events.stream_browser_events

    def fast_heartbeat(
        handler: http.server.BaseHTTPRequestHandler,
        mailbox: browser_events.BrowserEventMailbox,
        stop: threading.Event,
    ) -> None:
        stream(handler, mailbox, stop, heartbeat_interval=0.02)

    monkeypatch.setattr(browser_events, "stream_browser_events", fast_heartbeat)
    response = service.events()
    first = status(response)

    def forbidden() -> tuple[str | None, float, str, float | None, str | None, int]:
        pytest.fail("heartbeat or subscription performed disk admission")

    monkeypatch.setattr(service.store, "disk_status", forbidden)
    assert status(service.events()) == first
    assert response.readline(100) == b"event: heartbeat\n"
    assert response.readline(100) == b"data: {}\n"
    assert response.readline(2) == b"\n"
    assert response.readline(100) == b"event: heartbeat\n"


def test_last_disconnect_suspends_disk_sampling_and_reconnect_resumes_latest(
    service: BoardService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = service.server.browser_event_producer
    hub = service.server.browser_event_hub
    assert producer is not None and hub is not None
    producer._interval = 0.04
    stream = browser_events.stream_browser_events

    def fast_heartbeat(
        handler: http.server.BaseHTTPRequestHandler,
        mailbox: browser_events.BrowserEventMailbox,
        stop: threading.Event,
    ) -> None:
        stream(handler, mailbox, stop, heartbeat_interval=0.02)

    monkeypatch.setattr(browser_events, "stream_browser_events", fast_heartbeat)
    reads: list[int] = []
    disk_status = service.store.disk_status

    def counted() -> tuple[str | None, float, str, float | None, str | None, int]:
        reads.append(threading.get_ident())
        return disk_status()

    monkeypatch.setattr(service.store, "disk_status", counted)
    # Own the peer socket explicitly so shutdown reaches the server even when
    # HTTPResponse's file object still retains a reference to the connection.
    with socket.create_connection(("127.0.0.1", service.server.server_port), timeout=3) as peer:
        peer.sendall(
            f"GET {api_endpoint.API_PREFIX}/events HTTP/1.1\r\nHost: localhost\r\n\r\n".encode()
        )
        with http.client.HTTPResponse(peer) as response:
            response.begin()
            assert response.status == 200
            assert status(response)["revision"] == 0
            peer.shutdown(socket.SHUT_RDWR)
    wait_for(lambda: hub.subscriber_count == 0)
    # Let any sample admitted before the last unsubscribe finish, then observe
    # several full periods with no listeners. A command wake must stay dormant.
    time.sleep(0.12)
    settled = len(reads)
    time.sleep(0.12)
    assert len(reads) == settled
    service.store.execute(0, lambda board: board.create("new", "New", "backlog", "bot"))
    time.sleep(0.12)
    assert len(reads) == settled
    resumed = status_until(service.events(), lambda sample: sample.get("revision") == 1)
    assert resumed["ok"] is True
    assert len(reads) > settled


def test_blocked_metadata_builder_does_not_block_committing_writer(
    service: BoardService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = service.events()
    status(response)
    entered, release, committed = threading.Event(), threading.Event(), threading.Event()
    original = service.store.disk_status

    def blocked() -> tuple[str | None, float, str, float | None, str | None, int]:
        entered.set()
        assert release.wait(3)
        return original()

    monkeypatch.setattr(service.store, "disk_status", blocked)
    producer = service.server.browser_event_producer
    assert producer is not None
    producer.wake()
    assert entered.wait(2)
    errors: list[BaseException] = []

    def commit() -> None:
        try:
            service.store.execute(0, lambda board: board.create("new", "New", "backlog", "bot"))
            committed.set()
        except BaseException as exc:  # noqa: BLE001 -- preserve worker failure for assertion
            errors.append(exc)

    writer = threading.Thread(target=commit)
    try:
        writer.start()
        assert committed.wait(2), "writer waited on unrelated metadata work"
        assert not errors
    finally:
        release.set()
        writer.join(3)
    assert status(response)["revision"] == 1


def test_failed_commands_and_saves_do_not_wake_uncommitted_candidate(
    service: BoardService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    producer = service.server.browser_event_producer
    assert producer is not None
    wakes: list[None] = []
    monkeypatch.setattr(producer, "wake", lambda: wakes.append(None))
    before = service.path.read_bytes()
    with pytest.raises(api_endpoint.RevisionConflict):
        service.store.execute(99, lambda board: board.create("bad", "Bad", "backlog", "bot"))

    def refused(_board: board_state.Board) -> str:
        raise board_state.BoardError("command refused")

    with pytest.raises(board_state.BoardError, match="command refused"):
        service.store.execute(0, refused)

    def failed_save(
        _board: board_state.Board,
        _path: pathlib.Path,
        *,
        outcome: board_state.SaveOutcome | None = None,
    ) -> None:
        del outcome
        raise OSError("injected pre-replacement failure")

    monkeypatch.setattr(board_state, "save", failed_save)
    with pytest.raises(OSError, match="pre-replacement"):
        service.store.execute(0, lambda board: board.create("bad", "Bad", "backlog", "bot"))
    assert wakes == []
    assert service.store._board.revision == 0
    assert service.path.read_bytes() == before


def test_shutdown_closes_stream_and_idle_peer_and_joins_metadata_before_return(
    service: BoardService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api_endpoint, "HTTP_INCOMPLETE_DRAIN_S", 0.05)
    response = service.events()
    status(response)
    with socket.create_connection(("127.0.0.1", service.server.server_port), timeout=2) as idle:
        wait_for(lambda: bool(service.server._reading))
        service.server.shutdown()
        service.server.server_close()
        producer = service.server.browser_event_producer
        assert producer is not None and producer._thread is not None
        assert not producer._thread.is_alive()
        assert service.server.browser_event_hub is not None
        assert service.server.browser_event_hub.subscriber_count == 0
        assert response.read(1) == b""
        assert idle.recv(1) == b""


def test_bind_failure_does_not_construct_browser_worker(
    service: BoardService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden() -> browser_events.BrowserEventHub:
        pytest.fail("bind failure constructed browser event infrastructure")

    def failed_bind(_server: api_endpoint.BoardHttpServer) -> None:
        raise OSError("injected bind failure")

    monkeypatch.setattr(browser_events, "BrowserEventHub", forbidden)
    monkeypatch.setattr(api_endpoint.BoardHttpServer, "server_bind", failed_bind)
    with pytest.raises(OSError, match="injected bind failure"):
        api_endpoint.BoardHttpServer(
            ("127.0.0.1", service.server.server_port), api_endpoint.Handler
        )
