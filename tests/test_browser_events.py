"""Bounded fan-out, atomic subscription, sampling and stream teardown."""

import http.server
import json
import threading
import time
from collections.abc import Callable
from typing import cast

import pytest

from localswim import browser_events
from localswim.browser_events import (
    HEARTBEAT_FRAME,
    BrowserEventHub,
    BrowserEventMailbox,
    BrowserEventProducer,
    stream_browser_events,
)


def wait_for(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("bounded browser metadata wait expired")
        time.sleep(0.002)


def test_single_serialization_immutable_cache_and_named_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = json.dumps
    calls = 0

    def encode(data: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        # The exact serializer options are covered by the resulting frame.
        assert kwargs == {"ensure_ascii": True, "separators": (",", ":"), "allow_nan": False}
        return original(data, ensure_ascii=True, separators=(",", ":"), allow_nan=False)

    monkeypatch.setattr(browser_events.json, "dumps", encode)
    hub = BrowserEventHub()
    first, second = hub.subscribe(), hub.subscribe()
    payload = {"nested": {"value": "\n\u00e9"}}
    assert hub.publish("status", payload)
    payload["nested"]["value"] = "changed"
    third = hub.subscribe()
    frames = [mailbox.get(0) for mailbox in (first, second, third)]
    assert calls == 1
    assert frames[0] is frames[1] is frames[2]
    assert frames[0] == b'event: status\ndata: {"nested":{"value":"\\n\\u00e9"}}\n\n'
    assert not hub.publish("status", {"nested": {"value": "\n\u00e9"}})
    assert first.get(0) is None
    hub.close()
    assert first.closed and second.closed and third.closed


def test_subscribe_cache_handoff_has_no_publish_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    hub = BrowserEventHub()
    hub.publish("status", {"revision": 0})
    mailbox = BrowserEventMailbox()
    attached, release, published = threading.Event(), threading.Event(), threading.Event()
    original = mailbox.attach_hub

    def attach(source: BrowserEventHub, cache: dict[tuple[str, str | None], bytes]) -> bool:
        result = original(source, cache)
        attached.set()
        assert release.wait(2)
        return result

    monkeypatch.setattr(mailbox, "attach_hub", attach)
    subscriber = threading.Thread(target=hub.subscribe, args=(mailbox,))

    def publish() -> None:
        hub.publish("status", {"revision": 1})
        published.set()

    publisher = threading.Thread(target=publish)
    try:
        subscriber.start()
        assert attached.wait(2)
        publisher.start()
        assert not published.wait(0.02)
        release.set()
        subscriber.join(2)
        publisher.join(2)
        assert not subscriber.is_alive() and not publisher.is_alive()
        assert published.is_set()
        assert mailbox.get(0) == b'event: status\ndata: {"revision":1}\n\n'
        assert mailbox.get(0) is None
    finally:
        release.set()
        subscriber.join(2)
        if publisher.ident is not None:
            publisher.join(2)
        mailbox.close()
        hub.close()


def test_slow_browser_coalesces_each_board_and_bounds_unknown_keys() -> None:
    hub = BrowserEventHub(max_keys=3)
    fast, slow = hub.subscribe(), hub.subscribe()
    for revision in range(100):
        assert hub.publish("board", {"revision": revision}, key="a")
        assert fast.get(0) is not None
    assert slow.pending_count == 1
    assert slow.get(0) == b'event: board\ndata: {"revision":99}\n\n'
    for key in ("a", "b", "c", "d"):
        hub.publish("board", {"board": key}, key=key)
    assert slow.pending_count == 3
    cached = hub.subscribe()
    for mailbox in (slow, cached):
        assert [mailbox.get(0) for _ in range(3)] == [
            b'event: board\ndata: {"board":"b"}\n\n',
            b'event: board\ndata: {"board":"c"}\n\n',
            b'event: board\ndata: {"board":"d"}\n\n',
        ]
    hub.close()


def test_multiple_hubs_share_mailbox_and_context_removes_all_subscriptions() -> None:
    catalog, board = BrowserEventHub(), BrowserEventHub()
    catalog.publish("catalog", {"boards": []})
    board.publish("board", {"board": "key", "status": {}}, key="key")
    with catalog.subscribe() as mailbox:
        board.subscribe(mailbox)
        assert catalog.subscriber_count == board.subscriber_count == 1
        assert mailbox.pending_count == 2
        board.close()
        assert not mailbox.closed
        assert catalog.publish("catalog", {"boards": ["key"]})
    assert mailbox.closed
    assert catalog.subscriber_count == board.subscriber_count == 0
    with pytest.raises(RuntimeError, match="mailbox is closed"):
        catalog.subscribe(mailbox)
    catalog.close()


def test_close_wakes_blocked_reader_and_refuses_new_subscribers() -> None:
    hub = BrowserEventHub()
    mailbox = hub.subscribe()
    result: list[bytes | None] = []
    reader = threading.Thread(target=lambda: result.append(mailbox.get()))
    reader.start()
    hub.close()
    reader.join(2)
    assert not reader.is_alive()
    assert result == [None]
    assert mailbox.closed
    with pytest.raises(RuntimeError, match="hub is closed"):
        hub.subscribe()
    assert not hub.publish("status", {})


def test_event_validation_and_inclusive_metadata_byte_limit() -> None:
    hub = BrowserEventHub(max_bytes=4)
    assert hub.publish("status", "xx")
    with pytest.raises(ValueError, match="exceeds limit"):
        hub.publish("status", "xxx")
    with pytest.raises(ValueError, match="event name"):
        hub.publish("status\ndata: injected", {})
    with pytest.raises(ValueError, match="event key"):
        hub.publish("board", {}, key="x" * 1025)
    with pytest.raises(ValueError, match="Out of range"):
        hub.publish("status", float("nan"))
    mailbox = hub.subscribe()
    assert mailbox.get(0) == b'event: status\ndata: "xx"\n\n'
    hub.close()


def test_producer_construction_and_wake_are_inert_start_is_shared() -> None:
    hub = BrowserEventHub()
    calls: list[int] = []

    def build() -> object:
        calls.append(threading.get_ident())
        return {"sample": len(calls)}

    producer = BrowserEventProducer(hub, build, interval=0.08)
    producer.wake()
    assert not calls
    mailboxes = [hub.subscribe() for _ in range(4)]
    starters = [threading.Thread(target=producer.start) for _ in range(8)]
    try:
        for starter in starters:
            starter.start()
        for starter in starters:
            starter.join(2)
            assert not starter.is_alive()
        frames = [mailbox.get(2) for mailbox in mailboxes]
        assert frames == [b'event: status\ndata: {"sample":1}\n\n'] * 4
        assert len(calls) == 1
        assert calls[0] != threading.get_ident()
        # Verify the lifetime boundary cannot silently become a daemon worker.
        worker = producer._thread  # noqa: SLF001 -- inspect worker lifetime directly
        assert worker is not None and not worker.daemon
        frames = [mailbox.get(2) for mailbox in mailboxes]
        assert frames == [b'event: status\ndata: {"sample":2}\n\n'] * 4
        assert len(calls) == 2
    finally:
        producer.close()
        hub.close()


def test_wake_burst_debounces_without_per_browser_builds() -> None:
    hub = BrowserEventHub()
    calls = 0

    def build() -> object:
        nonlocal calls
        calls += 1
        return {"sample": calls}

    producer = BrowserEventProducer(hub, build, interval=10, quiet_seconds=0.06)
    mailbox = hub.subscribe()
    try:
        producer.start()
        assert mailbox.get(2) is not None
        for _ in range(12):
            producer.wake()
            time.sleep(0.002)
        assert calls == 1
        assert mailbox.get(2) == b'event: status\ndata: {"sample":2}\n\n'
        assert calls == 2
    finally:
        producer.close()
        hub.close()


def test_close_waits_for_admitted_builder_and_cannot_restart() -> None:
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    hub = BrowserEventHub()
    hub.subscribe()

    def build() -> object:
        entered.set()
        assert release.wait(2)
        return {}

    producer = BrowserEventProducer(hub, build)

    def close() -> None:
        producer.close()
        closed.set()

    closer = threading.Thread(target=close)
    try:
        producer.start()
        assert entered.wait(2)
        closer.start()
        assert not closed.wait(0.03)
        producer.wake()
        release.set()
        closer.join(2)
        assert closed.is_set()
        worker = producer._thread  # noqa: SLF001 -- verify close actually joined
        assert worker is not None and not worker.is_alive()
        with pytest.raises(RuntimeError, match="producer is closed"):
            producer.start()
    finally:
        release.set()
        if closer.ident is not None:
            closer.join(2)
        producer.close()
        hub.close()


def test_producer_without_subscribers_never_builds_on_timer_or_writer_wake() -> None:
    hub = BrowserEventHub()
    calls = 0

    def build() -> object:
        nonlocal calls
        calls += 1
        return {}

    producer = BrowserEventProducer(hub, build, interval=0.02, quiet_seconds=0.005)
    try:
        producer.wake()
        assert producer._thread is None  # noqa: SLF001 -- writer wake must not start a worker
        producer.start()
        for _ in range(8):
            producer.wake()
            time.sleep(0.01)
        assert calls == 0
    finally:
        producer.close()
        hub.close()


def test_producer_quiesces_after_last_close_and_resumes_with_fresh_sample() -> None:
    hub = BrowserEventHub()
    calls = 0

    def build() -> object:
        nonlocal calls
        calls += 1
        return {"sample": calls}

    producer = BrowserEventProducer(hub, build, interval=0.04, quiet_seconds=0.005)
    mailbox = hub.subscribe()
    try:
        producer.start()
        assert mailbox.get(2) == b'event: status\ndata: {"sample":1}\n\n'
        mailbox.close()
        for _ in range(12):
            producer.wake()
            time.sleep(0.01)
        assert calls == 1
        resumed = hub.subscribe()
        assert resumed.get(0) == b'event: status\ndata: {"sample":1}\n\n'
        producer.start()
        assert resumed.get(2) == b'event: status\ndata: {"sample":2}\n\n'
        assert calls == 2
    finally:
        producer.close()
        hub.close()


def test_additional_subscribers_reuse_cache_without_waking_existing_producer() -> None:
    hub = BrowserEventHub()
    calls = 0

    def build() -> object:
        nonlocal calls
        calls += 1
        return {"sample": calls}

    producer = BrowserEventProducer(hub, build, interval=10, quiet_seconds=0.005)
    first = hub.subscribe()
    try:
        producer.start()
        assert first.get(2) is not None
        for _ in range(5):
            additional = hub.subscribe()
            producer.start()
            assert additional.get(0) == b'event: status\ndata: {"sample":1}\n\n'
        time.sleep(0.04)
        assert calls == 1
    finally:
        producer.close()
        hub.close()


def test_failed_sample_is_contained_and_later_period_recovers() -> None:
    hub = BrowserEventHub()
    calls = 0

    def build() -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("private error text")
        return {"healthy": True}

    producer = BrowserEventProducer(hub, build, interval=0.03)
    mailbox = hub.subscribe()
    try:
        producer.start()
        assert mailbox.get(2) == b'event: status\ndata: {"healthy":true}\n\n'
        assert calls == 2
    finally:
        producer.close()
        hub.close()


class _Connection:
    timeout: float | None = None

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout


class _Writer:
    def __init__(self, stop: threading.Event, *, fail: bool = False) -> None:
        self.frames: list[bytes] = []
        self.stop = stop
        self.fail = fail

    def write(self, data: bytes) -> int:
        if self.fail:
            raise TimeoutError
        self.frames.append(data)
        if data == HEARTBEAT_FRAME:
            self.stop.set()
        return len(data)

    def flush(self) -> None:
        return


class _Handler:
    def __init__(self, stop: threading.Event, *, fail: bool = False) -> None:
        self.connection = _Connection()
        self.wfile = _Writer(stop, fail=fail)
        self.close_connection = False
        self.status: int | None = None
        self.headers: dict[str, str] = {}

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.headers[name] = value

    def end_headers(self) -> None:
        self.connection.settimeout(30.0)


def test_stream_heartbeat_is_prebuilt_and_unsubscribes_after_stop() -> None:
    stop = threading.Event()
    hub = BrowserEventHub()
    mailbox = hub.subscribe()
    hub.publish("status", {"revision": 3})
    handler = _Handler(stop)
    stream_browser_events(
        cast(http.server.BaseHTTPRequestHandler, handler), mailbox, stop, heartbeat_interval=0.01
    )
    assert handler.status == 200
    assert handler.headers["Content-Type"] == "text/event-stream; charset=utf-8"
    assert "Content-Length" not in handler.headers
    assert handler.connection.timeout == 1.0
    assert handler.close_connection
    assert handler.wfile.frames == [b'event: status\ndata: {"revision":3}\n\n', HEARTBEAT_FRAME]
    assert hub.subscriber_count == 0 and mailbox.closed


def test_stream_write_timeout_closes_only_its_mailbox() -> None:
    stop = threading.Event()
    hub = BrowserEventHub()
    mailbox, healthy = hub.subscribe(), hub.subscribe()
    hub.publish("status", {})
    handler = _Handler(stop, fail=True)
    stream_browser_events(cast(http.server.BaseHTTPRequestHandler, handler), mailbox, stop)
    assert mailbox.closed
    assert not healthy.closed
    assert hub.subscriber_count == 1
    assert healthy.get(0) == b"event: status\ndata: {}\n\n"
    hub.close()
