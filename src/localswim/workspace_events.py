"""Shared workspace catalog production and bounded, selected-board SSE relays."""

import contextlib
import http.client
import io
import json
import socket
import threading
import time
import urllib.parse
import uuid
from functools import partial
from typing import TYPE_CHECKING, cast

from . import board_state, browser_events, service_ownership

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

    from _typeshed import WriteableBuffer

    from .workspace import Endpoint, Workspace

CATALOG_INTERVAL = 10.0
MAX_CATALOG_BYTES = 1024 * 1024
MAX_UPSTREAM_FRAME_BYTES = 64 * 1024
RECONNECT_INTERVAL = 1.0
UPSTREAM_IDLE_TIMEOUT = 15.0
UPSTREAM_READ_POLL = 0.1


class _PollingReader(io.RawIOBase):
    """Retry short recv polls without poisoning a socket's buffered makefile."""

    def __init__(self, connection: socket.socket, stop_event: threading.Event) -> None:
        self.connection = connection
        self.stop_event = stop_event
        self.deadline = time.monotonic() + UPSTREAM_IDLE_TIMEOUT
        # Retain normal makefile ownership when HTTP/1.0 closes the connection.
        self._lease = connection.makefile("rb", buffering=0)

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: WriteableBuffer) -> int:
        while not self.stop_event.is_set():
            if time.monotonic() >= self.deadline:
                raise TimeoutError("board event stream idle timeout")
            try:
                count = self.connection.recv_into(buffer)
            except TimeoutError:
                continue
            self.deadline = time.monotonic() + UPSTREAM_IDLE_TIMEOUT
            return count
        raise OSError("board event stream stopped")

    def close(self) -> None:
        self._lease.close()
        super().close()


class _PollingSocket:
    """Supply the HTTP parser with a cancellation-aware receive stream."""

    def __init__(self, connection: socket.socket, stop_event: threading.Event) -> None:
        self.connection = connection
        self.stop_event = stop_event

    def makefile(self, mode: str) -> io.BufferedReader:
        if mode != "rb":
            raise ValueError("invalid board event stream mode")
        return io.BufferedReader(_PollingReader(self.connection, self.stop_event))


class _PollingResponse(http.client.HTTPResponse):
    def __init__(
        self,
        connection: socket.socket,
        *,
        method: str | None = None,
        stop_event: threading.Event,
    ) -> None:
        connection.settimeout(UPSTREAM_READ_POLL)
        super().__init__(
            cast("socket.socket", _PollingSocket(connection, stop_event)), method=method
        )


MAX_WATCH_QUERY_BYTES = 48 * 1024
MAX_WATCH_BOARDS = 1024


def selected_boards(query: str) -> tuple[str, ...]:
    """Validate bounded, explicit watch membership; never infer it from catalog."""
    if len(query.encode("utf-8")) > MAX_WATCH_QUERY_BYTES:
        raise ValueError("workspace events selection exceeds limit")
    parameters = urllib.parse.parse_qsl(
        query, keep_blank_values=True, max_num_fields=MAX_WATCH_BOARDS
    )
    keys: list[str] = []
    for name, key in parameters:
        if name != "board":
            raise ValueError("unknown workspace events parameter")
        parts = key.split("/")
        if (
            not key
            or len(key) > 1024
            or len(key.encode("utf-8")) > 4096
            or key in keys
            or any(
                not part
                or part.startswith(".")
                or (part == "attachments" and index < len(parts) - 1)
                or "\\" in part
                or ":" in part
                for index, part in enumerate(parts)
            )
        ):
            raise ValueError("unknown workspace board")
        keys.append(key)
    return tuple(keys)


def selected_board(query: str) -> str | None:
    """Compatibility parser for callers requiring exactly one selected key."""
    urllib.parse.parse_qsl(query, keep_blank_values=True, max_num_fields=1)
    keys = selected_boards(query)
    if len(keys) > 1:
        raise ValueError("multiple selected workspace boards")
    return keys[0] if keys else None


def upstream_events(response: http.client.HTTPResponse) -> Iterator[board_state.JsonObject]:
    """Yield complete, bounded status objects from the dedicated streaming reader."""
    size = 0
    event = ""
    data: list[bytes] = []
    while True:
        line = response.readline(MAX_UPSTREAM_FRAME_BYTES - size + 1)
        if not line:
            return
        size += len(line)
        if size > MAX_UPSTREAM_FRAME_BYTES or not line.endswith(b"\n"):
            raise ValueError("board event frame exceeds limit")
        line = line.rstrip(b"\r\n")
        if not line:
            if event == "status" and data:
                value = cast("board_state.JsonValue", json.loads(b"\n".join(data)))
                if not isinstance(value, dict) or type(value.get("ok")) is not bool:
                    raise ValueError("invalid board event status")
                yield value
            elif event not in ("", "heartbeat"):
                raise ValueError("unknown board event")
            size, event, data = 0, "", []
        elif line.startswith(b"event: "):
            if event:
                raise ValueError("duplicate board event name")
            event = line[7:].decode("ascii")
        elif line.startswith(b"data: "):
            data.append(line[6:])
        elif not line.startswith(b":"):
            raise ValueError("invalid board event field")


class _BoardRelay:
    """One shared upstream stream for an explicitly watched board key."""

    def __init__(self, workspace: Workspace, key: str) -> None:
        self.workspace = workspace
        self.key = key
        self.hub = browser_events.BrowserEventHub(max_bytes=MAX_UPSTREAM_FRAME_BYTES + 8192)
        self._stop = threading.Event()
        self._transport_lock = threading.Lock()
        self._connection: http.client.HTTPConnection | None = None
        self._response: http.client.HTTPResponse | None = None
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._endpoint: Endpoint | None = None
        self._upgrade_requested = False

    def prepare(self) -> None:
        """Resolve the explicit selection before sending browser stream headers."""
        _, self._endpoint = self.workspace.resolve_endpoint(self.key)

    def start(self) -> None:
        if self._thread is None and not self._stop.is_set():
            self._thread = threading.Thread(
                target=self._run, name="workspace-board-events", daemon=False
            )
            self._thread.start()

    @contextlib.contextmanager
    def _upstream(self, endpoint: Endpoint, route: str) -> Generator[http.client.HTTPResponse]:
        """Track the one active HTTP transport, including a legacy status hint."""
        if type(endpoint.port) is not int or not 0 < endpoint.port < 65536:
            raise ValueError("invalid board event endpoint")
        connection = http.client.HTTPConnection("127.0.0.1", endpoint.port, timeout=1.0)
        connection.response_class = cast(
            "type[http.client.HTTPResponse]", partial(_PollingResponse, stop_event=self._stop)
        )
        response: http.client.HTTPResponse | None = None
        with self._transport_lock:
            if self._stop.is_set():
                raise OSError("board event stream stopped")
            self._connection = connection
        try:
            connection.request("GET", board_state.API_PREFIX + route)
            with self._transport_lock:
                self._socket = connection.sock
            if self._stop.is_set():
                raise OSError("board event stream stopped")
            response = connection.getresponse()
            with self._transport_lock:
                self._response = response
            yield response
        finally:
            with self._transport_lock:
                self._connection = self._response = self._socket = None
            try:
                if response is not None:
                    response.close()
            finally:
                connection.close()

    def _stream(self, endpoint: Endpoint) -> None:
        upgrade = False
        with self._upstream(endpoint, "/events") as response:
            if response.status == 404 and not self._upgrade_requested:
                # Pre-SSE writers learn that their source is stale through status.
                self._upgrade_requested = upgrade = True
            elif (
                response.status != 200
                or response.getheader("Content-Type", "").split(";", 1)[0] != "text/event-stream"
            ):
                raise ValueError("board event response rejected")
            else:
                for status in upstream_events(response):
                    if self._stop.is_set():
                        break
                    self.hub.publish("board", {"board": self.key, "status": status}, key=self.key)
        if upgrade:
            # Use the same cancellation-aware transport for this one read-only
            # hint. A partial/trickling legacy response cannot pin shutdown.
            with self._upstream(endpoint, "/status") as response:
                status = service_ownership.decode_service_metadata(
                    response.read(service_ownership.MAX_METADATA_BYTES + 1)
                )
                if not (
                    response.status == 200
                    and isinstance(status, dict)
                    and isinstance(status.get("build"), str)
                    and "codeStale" in status
                ):
                    raise ValueError("board upgrade status rejected")

    def _run(self) -> None:
        endpoint = self._endpoint
        while not self._stop.is_set():
            try:
                if endpoint is None:
                    _, endpoint = self.workspace.resolve_endpoint(self.key)
                self._stream(endpoint)
            except OSError, http.client.HTTPException, ValueError, RecursionError:
                pass
            if not self._stop.is_set():
                self.hub.publish(
                    "board",
                    {
                        "board": self.key,
                        "status": {
                            "ok": False,
                            "mtime": 0,
                            "stamp": "unavailable",
                            "readAt": None,
                            "error": "board service unavailable",
                        },
                    },
                    key=self.key,
                )
            endpoint = None
            self._stop.wait(RECONNECT_INTERVAL)

    def close(self) -> None:
        """Interrupt transport, then let its reader own response/connection cleanup."""
        self._stop.set()
        with self._transport_lock:
            upstream_socket = self._socket
            if upstream_socket is None and self._connection is not None:
                # request() may have connected before publishing the tracked socket.
                upstream_socket = self._connection.sock
        if upstream_socket is not None:
            with contextlib.suppress(OSError):
                upstream_socket.shutdown(socket.SHUT_RDWR)
        # HTTPResponse.close() mutates the same buffered state as readline().
        # Only _upstream's reader-thread finally may close the response/connection;
        # closing either here races EOF cleanup inside the HTTP parser.
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self.hub.close()


class WorkspaceEvents:
    """Combine the separate catalog channel with explicit retained-board watches.

    Each board has one upstream relay across browser subscribers. Membership
    replacement may reconnect a browser, but the status content markers remain
    authoritative: subscribing does not request or invalidate a full snapshot.
    """

    def __init__(self, workspace: Workspace, catalog: Callable[[], object]) -> None:
        self.workspace = workspace
        self.stop_event = threading.Event()
        self.catalog_hub = browser_events.BrowserEventHub(max_bytes=MAX_CATALOG_BYTES)
        self.catalog_producer = browser_events.BrowserEventProducer(
            self.catalog_hub, catalog, event="catalog", interval=CATALOG_INTERVAL
        )
        self._lock = threading.Lock()
        self._sessions: dict[browser_events.BrowserEventMailbox, tuple[str, ...]] = {}
        self._relays: dict[str, _BoardRelay] = {}

    def subscribe(self, keys: tuple[str, ...] | str | None) -> browser_events.BrowserEventMailbox:
        if keys is None:
            keys = ()
        elif isinstance(keys, str):
            keys = (keys,)
        # Direct callers receive the same bounds and canonical-key checks as HTTP.
        query = urllib.parse.urlencode([("board", key) for key in keys])
        keys = selected_boards(query)
        # One slot per watched board plus catalog and handshake: updates coalesce only
        # within a board, never silently displace another board's currentness.
        mailbox = browser_events.BrowserEventMailbox(max_pending=len(keys) + 2)
        handshake = json.dumps({"after": time.monotonic(), "session": uuid.uuid4().hex})
        mailbox.offer(("watch", None), b"event: watch\ndata: " + handshake.encode() + b"\n\n")
        created: list[_BoardRelay] = []
        try:
            with self._lock:
                if self.stop_event.is_set():
                    raise OSError("workspace events are closed")
                selected: dict[str, _BoardRelay] = {}
                for key in keys:
                    relay = self._relays.get(key)
                    if relay is None:
                        relay = _BoardRelay(self.workspace, key)
                        relay.prepare()
                        created.append(relay)
                    selected[key] = relay
                self.catalog_hub.subscribe(mailbox)
                for relay in selected.values():
                    relay.hub.subscribe(mailbox)
                self.catalog_producer.start()
                for relay in created:
                    relay.start()
                # Publish only fully started relays. A failed request's staged
                # relays must never become another subscriber's inert source.
                self._relays.update(selected)
                self._sessions[mailbox] = keys
            return mailbox
        except BaseException:
            self.unsubscribe(mailbox)
            with self._lock:
                unused = [
                    relay
                    for relay in created
                    if self._relays.get(relay.key) is not relay
                    or not any(relay.key in watched for watched in self._sessions.values())
                ]
                for relay in unused:
                    if self._relays.get(relay.key) is relay:
                        del self._relays[relay.key]
            for relay in unused:
                relay.close()
            raise

    def unsubscribe(self, mailbox: browser_events.BrowserEventMailbox) -> None:
        with self._lock:
            keys = self._sessions.pop(mailbox, ())
            unused = [
                self._relays.pop(key)
                for key in keys
                if key in self._relays
                and not any(key in watched for watched in self._sessions.values())
            ]
        mailbox.close()
        for relay in unused:
            relay.close()

    def wake_catalog(self) -> None:
        self.catalog_producer.wake()

    def close(self) -> None:
        with self._lock:
            self.stop_event.set()
            relays = tuple(self._relays.values())
            self._relays.clear()
            self._sessions.clear()
        # Wake HTTP consumers before a server can join its non-daemon workers.
        self.catalog_hub.close()
        for relay in relays:
            relay.hub.close()
        self.catalog_producer.close()
        for relay in relays:
            relay.close()
