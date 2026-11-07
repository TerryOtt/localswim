"""Process-local, latest-only browser metadata; never an audit or replay log.

Publishers serialize metadata away from board transactions. Fan-out only acquires
short memory locks: a slow browser replaces pending values instead of holding up
the producer. A mailbox may merge several hubs into one bounded browser stream.
"""

import http.server
import json
import math
import threading
import time
from collections.abc import Callable
from types import TracebackType

MAX_EVENT_BYTES = 64 * 1024
MAX_PENDING_EVENTS = 18
SSE_WRITE_TIMEOUT_S = 1.0
HEARTBEAT_FRAME = b"event: heartbeat\ndata: {}\n\n"
_EVENT_NAMES = frozenset({"status", "catalog", "board", "error"})
type _EventKey = tuple[str, str | None]


class BrowserEventMailbox:
    """One browser's bounded latest-per-key frames and subscription ownership."""

    def __init__(self, *, max_pending: int = MAX_PENDING_EVENTS) -> None:
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError("invalid browser event mailbox limit")
        self._limit = max_pending
        self._condition = threading.Condition()
        self._pending: dict[_EventKey, bytes] = {}
        self._hubs: set[BrowserEventHub] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def pending_count(self) -> int:
        with self._condition:
            return len(self._pending)

    def _put_locked(self, key: _EventKey, frame: bytes) -> None:
        self._pending.pop(key, None)
        self._pending[key] = frame
        if len(self._pending) > self._limit:
            del self._pending[next(iter(self._pending))]

    def attach_hub(self, hub: BrowserEventHub, cache: dict[_EventKey, bytes]) -> bool:
        """Hub-only handshake; its lock must cover registration and this seed."""
        with self._condition:
            if self._closed:
                return False
            self._hubs.add(hub)
            for key, frame in cache.items():
                self._put_locked(key, frame)
            self._condition.notify_all()
            return True

    def offer(self, key: _EventKey, frame: bytes) -> None:
        """Accept an immutable preframed update from a subscribed hub."""
        with self._condition:
            if not self._closed:
                self._put_locked(key, frame)
                self._condition.notify()

    def detach_hub(self, hub: BrowserEventHub, *, close_if_orphan: bool = False) -> None:
        """Remove one hub while preserving the other subscriptions."""
        with self._condition:
            self._hubs.discard(hub)
            if close_if_orphan and not self._hubs:
                self._closed = True
                self._pending.clear()
            self._condition.notify_all()

    def get(self, timeout: float | None = None) -> bytes | None:
        """Return one preframed update, or None after timeout/close; never do I/O."""
        with self._condition:
            self._condition.wait_for(lambda: bool(self._pending) or self._closed, timeout)
            if self._closed or not self._pending:
                return None
            key = next(iter(self._pending))
            return self._pending.pop(key)

    def close(self) -> None:
        """Unsubscribe every hub without acquiring a hub lock under our lock."""
        with self._condition:
            self._closed = True
            self._pending.clear()
            hubs = tuple(self._hubs)
            self._hubs.clear()
            self._condition.notify_all()
        for hub in hubs:
            hub.unsubscribe(self)

    def __enter__(self) -> BrowserEventMailbox:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


class BrowserEventHub:
    """Atomic subscription/cache handoff and bounded in-memory fan-out.

    ``publish`` belongs on the shared producer or relay, never the board writer.
    A key identifies one coalescing channel (for board events, its stable board
    key). No event IDs or Last-Event-ID replay are supported.
    """

    def __init__(
        self, *, max_bytes: int = MAX_EVENT_BYTES, max_keys: int = MAX_PENDING_EVENTS
    ) -> None:
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("invalid browser metadata size limit")
        if type(max_keys) is not int or max_keys < 1:
            raise ValueError("invalid browser metadata key limit")
        self._max_bytes = max_bytes
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._cache: dict[_EventKey, bytes] = {}
        self._subscribers: set[BrowserEventMailbox] = set()
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, event: str, data: object, *, key: str | None = None) -> bool:
        """Serialize once; replace cached/pending metadata without socket writes."""
        if event not in _EVENT_NAMES:
            raise ValueError("invalid browser event name")
        if key is not None and len(key) > 1024:
            raise ValueError("invalid browser event key")
        payload = json.dumps(data, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        encoded = payload.encode("utf-8")
        if len(encoded) > self._max_bytes:
            raise ValueError("browser event metadata exceeds limit")
        frame = b"event: " + event.encode("ascii") + b"\ndata: " + encoded + b"\n\n"
        channel = (event, key)
        with self._lock:
            if self._closed or self._cache.get(channel) == frame:
                return False
            self._cache.pop(channel, None)
            self._cache[channel] = frame
            if len(self._cache) > self._max_keys:
                del self._cache[next(iter(self._cache))]
            for mailbox in self._subscribers:
                mailbox.offer(channel, frame)
            return True

    def subscribe(self, mailbox: BrowserEventMailbox | None = None) -> BrowserEventMailbox:
        """Seed and register under one lock, so no concurrent publish is missed."""
        if mailbox is None:
            mailbox = BrowserEventMailbox(max_pending=self._max_keys)
        with self._lock:
            if self._closed:
                raise RuntimeError("browser event hub is closed")
            if mailbox not in self._subscribers and mailbox.attach_hub(self, self._cache):
                self._subscribers.add(mailbox)
            elif mailbox.closed:
                raise RuntimeError("browser event mailbox is closed")
        return mailbox

    def unsubscribe(self, mailbox: BrowserEventMailbox) -> None:
        with self._lock:
            self._subscribers.discard(mailbox)
            mailbox.detach_hub(self)

    def close(self) -> None:
        """Detach subscribers; preserve mailboxes still fed by another live hub."""
        with self._lock:
            self._closed = True
            for mailbox in self._subscribers:
                mailbox.detach_hub(self, close_if_orphan=True)
            self._subscribers.clear()
            self._cache.clear()


class BrowserEventProducer:
    """One non-daemon periodic builder with coalesced, memory-only wakeups.

    Construction is inert. Start after the service has bound. Close joins without
    a timeout: the builder may admit disk state, so ownership must remain held
    until it has actually returned. Unexpected builder errors lose that sample
    and are retried at the next ordinary wake/period, without leaking raw errors.
    Without subscribers the timer is inert: no builder or metadata I/O runs.
    """

    def __init__(
        self,
        hub: BrowserEventHub,
        builder: Callable[[], object],
        *,
        event: str = "status",
        interval: float = 5.0,
        key: str | None = None,
        quiet_seconds: float = 0.05,
    ) -> None:
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("invalid browser producer interval")
        if not math.isfinite(quiet_seconds) or quiet_seconds < 0:
            raise ValueError("invalid browser producer quiet period")
        if event not in _EVENT_NAMES:
            raise ValueError("invalid browser event name")
        self._hub = hub
        self._builder = builder
        self._event = event
        self._interval = interval
        self._key = key
        self._quiet = quiet_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lifecycle = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lifecycle:
            if self._stop.is_set():
                raise RuntimeError("browser event producer is closed")
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="localswim-browser-metadata", daemon=False
                )
                self._thread.start()
            elif self._hub.subscriber_count == 1:
                # Call after subscribing: refresh a resumed stream promptly without
                # rebuilding separately for additional concurrent browsers.
                self._wake.set()

    def wake(self) -> None:
        """Notify the worker without building, serializing, reading or writing."""
        self._wake.set()

    def _wait_quiet(self, periodic_deadline: float) -> None:
        deadline = min(time.monotonic() + self._quiet, periodic_deadline)
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._wake.wait(remaining):
                return
            self._wake.clear()
            deadline = min(time.monotonic() + self._quiet, periodic_deadline)

    def _run(self) -> None:
        # Wakes before activation are already covered by the initial sample.
        self._wake.clear()
        deadline = time.monotonic()
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining > 0 and self._wake.wait(remaining):
                self._wake.clear()
                self._wait_quiet(deadline)
            if self._stop.is_set():
                return
            if self._hub.subscriber_count:
                try:
                    self._hub.publish(self._event, self._builder(), key=self._key)
                # Callbacks may raise arbitrary failures; never print private error text.
                except Exception:  # noqa: BLE001, S110 -- best-effort sampling retries next cycle
                    pass
            deadline = time.monotonic() + self._interval

    def close(self) -> None:
        with self._lifecycle:
            self._stop.set()
            self._wake.set()
            worker = self._thread
        if worker is not None:
            worker.join()


def stream_browser_events(
    handler: http.server.BaseHTTPRequestHandler,
    mailbox: BrowserEventMailbox,
    stop_event: threading.Event,
    *,
    heartbeat_interval: float = 5.0,
) -> None:
    """Write one bounded stream, releasing every subscription on any exit."""
    if not math.isfinite(heartbeat_interval) or heartbeat_interval <= 0:
        raise ValueError("invalid browser heartbeat interval")
    with mailbox:
        handler.close_connection = True
        try:
            handler.connection.settimeout(SSE_WRITE_TIMEOUT_S)
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
            handler.send_header("Cache-Control", "no-store, max-age=0")
            handler.send_header("Connection", "close")
            handler.send_header("X-Accel-Buffering", "no")
            handler.end_headers()
            # Ordinary response helpers may restore their usual idle timeout.
            handler.connection.settimeout(SSE_WRITE_TIMEOUT_S)
            handler.wfile.flush()
            heartbeat_at = time.monotonic() + heartbeat_interval
            while not stop_event.is_set() and not mailbox.closed:
                remaining = max(0.0, heartbeat_at - time.monotonic())
                frame = mailbox.get(timeout=min(SSE_WRITE_TIMEOUT_S, remaining))
                if stop_event.is_set() or mailbox.closed:
                    return
                if frame is not None:
                    handler.wfile.write(frame)
                    handler.wfile.flush()
                if time.monotonic() >= heartbeat_at:
                    handler.wfile.write(HEARTBEAT_FRAME)
                    handler.wfile.flush()
                    heartbeat_at = time.monotonic() + heartbeat_interval
        except OSError, ValueError:
            # Disconnects/timeouts terminate only this browser, never its producer.
            return
