"""Owner notification preferences and bounded asynchronous dispatch."""

import json
import pathlib
import threading
import time
from collections.abc import Callable
from typing import cast

import pytest

from localswim.board_state import JsonObject
from localswim.notifications import (
    MAX_TICKETS,
    NotificationConfigError,
    NotificationDispatcher,
    NotificationEvent,
    NotificationSettings,
    TicketOperation,
    load_settings,
    save_target,
    set_enabled,
    settings_path,
)

IDENTITY = "a" * 64
TARGET = "0f351a87-f7ea-4da4-9a89-47a96a64abcb"
OTHER = "ac6ab686-9911-4fa2-81a1-2b645c87b147"


def event(ticket: int = 1) -> NotificationEvent:
    return NotificationEvent(
        IDENTITY, "Board", "2026-11-07T10:00:00+00:00", "2026-11-07T10:00:01+00:00", (ticket,)
    )


def wait_for(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("bounded worker wait expired")
        time.sleep(0.005)


def test_settings_explicit_opt_in_and_target_preserves_enabled(tmp_path: pathlib.Path) -> None:
    assert load_settings(IDENTITY, config_root=tmp_path) == NotificationSettings(IDENTITY)
    assert not list(tmp_path.iterdir())
    assert not save_target(IDENTITY, TARGET, config_root=tmp_path).enabled
    set_enabled(IDENTITY, True, config_root=tmp_path)
    assert save_target(IDENTITY, OTHER, config_root=tmp_path).enabled
    assert load_settings(IDENTITY, config_root=tmp_path).thread_id == OTHER
    assert not set_enabled(IDENTITY, False, config_root=tmp_path).enabled
    assert settings_path(IDENTITY, config_root=tmp_path).parent == tmp_path


@pytest.mark.parametrize("data", [b"{}", b"x" * 4097, b'{"schema":1,"schema":1}', b"\xff"])
def test_settings_refuse_malformed(tmp_path: pathlib.Path, data: bytes) -> None:
    path = settings_path(IDENTITY, config_root=tmp_path)
    path.write_bytes(data)
    with pytest.raises(NotificationConfigError):
        save_target(IDENTITY, TARGET, config_root=tmp_path)
    assert path.read_bytes() == data


def test_config_schema_and_target_validation(tmp_path: pathlib.Path) -> None:
    with pytest.raises(NotificationConfigError):
        save_target(IDENTITY, "not-a-uuid", config_root=tmp_path)
    path = settings_path(IDENTITY, config_root=tmp_path)
    path.write_text(
        json.dumps(
            {"schema": True, "boardIdentity": IDENTITY, "threadId": TARGET, "enabled": True}
        ),
        encoding="utf-8",
    )
    with pytest.raises(NotificationConfigError):
        load_settings(IDENTITY, config_root=tmp_path)


def test_debounce_retarget_and_metadata_only() -> None:
    current = NotificationSettings(IDENTITY, TARGET, True)
    deliveries: list[tuple[str, JsonObject]] = []

    def deliver(target: str, payload: JsonObject, _stop: threading.Event) -> str:
        deliveries.append((target, payload))
        return "accepted"

    dispatcher = NotificationDispatcher(
        IDENTITY, deliver, quiet_seconds=0.06, settings_loader=lambda: current
    )
    try:
        dispatcher.submit(event(1))
        time.sleep(0.035)
        dispatcher.submit(event(2))
        current = NotificationSettings(IDENTITY, OTHER, True)
        time.sleep(0.035)
        assert not deliveries
        wait_for(lambda: len(deliveries) == 1)
        target, payload = deliveries[0]
        assert target == OTHER
        assert payload == {
            "boardIdentity": IDENTITY,
            "label": "Board",
            "since": event().since,
            "until": event().until,
            "tickets": [1, 2],
            "truncated": False,
            "operations": [["Contents modified"], ["Contents modified"]],
        }
        wait_for(lambda: dispatcher.status()["outcome"] == "queued")
    finally:
        dispatcher.close()


def test_blocked_config_and_delivery_do_not_block_submit() -> None:
    entered, release = threading.Event(), threading.Event()
    delivered, delivery_release = threading.Event(), threading.Event()
    count = 0

    def load() -> NotificationSettings:
        entered.set()
        assert release.wait(2)
        return NotificationSettings(IDENTITY, TARGET, True)

    def deliver(_target: str, _payload: JsonObject, _stop: threading.Event) -> str:
        nonlocal count
        count += 1
        delivered.set()
        assert delivery_release.wait(2)
        return "accepted"

    dispatcher = NotificationDispatcher(IDENTITY, deliver, quiet_seconds=0.01, settings_loader=load)
    try:
        dispatcher.submit(event())
        assert entered.wait(1)
        before = time.monotonic()
        dispatcher.submit(event(2))
        assert time.monotonic() - before < 0.2
        release.set()
        assert delivered.wait(1)
        before = time.monotonic()
        dispatcher.submit(event(3))
        assert time.monotonic() - before < 0.2
        delivery_release.set()
        wait_for(lambda: count == 2)
    finally:
        release.set()
        delivery_release.set()
        dispatcher.close()


def test_disabled_drops_pending_and_does_not_catch_up() -> None:
    current = NotificationSettings(IDENTITY, TARGET, True)
    calls: list[JsonObject] = []

    def deliver(_target: str, payload: JsonObject, _stop: threading.Event) -> str:
        calls.append(payload)
        return "accepted"

    dispatcher = NotificationDispatcher(
        IDENTITY, deliver, quiet_seconds=0.05, settings_loader=lambda: current
    )
    try:
        dispatcher.submit(event())
        current = NotificationSettings(IDENTITY, TARGET, False)
        wait_for(lambda: dispatcher.status()["outcome"] == "disabled")
        current = NotificationSettings(IDENTITY, TARGET, True)
        time.sleep(0.07)
        assert not calls
        dispatcher.submit(event(2))
        wait_for(lambda: len(calls) == 1)
        assert calls[0]["tickets"] == [2]
    finally:
        dispatcher.close()


def test_ticket_cap_and_shutdown_without_quiet_wait() -> None:
    entered, release = threading.Event(), threading.Event()

    def load() -> NotificationSettings:
        entered.set()
        assert release.wait(2)
        return NotificationSettings(IDENTITY, TARGET, True)

    def deliver(_target: str, _payload: JsonObject, _stop: threading.Event) -> str:
        pytest.fail("shutdown must drop pending burst")

    dispatcher = NotificationDispatcher(IDENTITY, deliver, settings_loader=load)
    dispatcher.submit(event())
    assert entered.wait(1)
    for ticket in range(2, 140):
        dispatcher.submit(event(ticket))
    pending = dispatcher._pending  # noqa: SLF001 -- inspect the bounded merge while worker is blocked
    assert pending is not None
    assert len(pending.tickets) == MAX_TICKETS
    assert pending.truncated
    assert len(pending.operations) == MAX_TICKETS
    before = time.monotonic()
    dispatcher.close(timeout=0.01)
    assert time.monotonic() - before < 0.2
    release.set()
    dispatcher.close()
    assert not dispatcher.status()["pending"]


@pytest.mark.parametrize("fail_config", [False, True])
def test_failures_drop_without_retry(fail_config: bool) -> None:
    attempts = 0

    def load() -> NotificationSettings:
        if fail_config:
            raise OSError("private data must never be reported")
        return NotificationSettings(IDENTITY, TARGET, True)

    def deliver(_target: str, _payload: JsonObject, _stop: threading.Event) -> str:
        nonlocal attempts
        attempts += 1
        raise OSError("private error")

    dispatcher = NotificationDispatcher(IDENTITY, deliver, quiet_seconds=0, settings_loader=load)
    try:
        dispatcher.submit(event())
        wait_for(lambda: dispatcher.status()["outcome"] in {"config_failed", "delivery_failed"})
        time.sleep(0.03)
        assert attempts == (0 if fail_config else 1)
        assert "private" not in json.dumps(dispatcher.status())
        assert not dispatcher.status()["pending"]
    finally:
        dispatcher.close()


def test_event_refuses_unbounded_and_naive_metadata() -> None:
    with pytest.raises(ValueError, match="timezone aware"):
        NotificationEvent(IDENTITY, "Board", "2026-11-07T10:00:00", event().until)
    with pytest.raises(ValueError, match="tickets"):
        NotificationEvent(IDENTITY, "Board", event().since, event().until, tuple(range(1, 130)))


def test_burst_operations_merge_and_remain_aligned_after_truncation() -> None:
    entered, release = threading.Event(), threading.Event()

    def load() -> NotificationSettings:
        entered.set()
        assert release.wait(2)
        return NotificationSettings(IDENTITY, TARGET, True)

    def deliver(_target: str, _payload: JsonObject, _stop: threading.Event) -> str:
        return "accepted"

    dispatcher = NotificationDispatcher(IDENTITY, deliver, settings_loader=load)
    created: tuple[TicketOperation, ...] = ("Created",)
    try:
        first = event()
        dispatcher.submit(
            NotificationEvent(
                IDENTITY,
                "Board",
                first.since,
                first.until,
                tuple(range(1, MAX_TICKETS + 1)),
                operations=(created,) * MAX_TICKETS,
            )
        )
        assert entered.wait(1)
        dispatcher.submit(
            NotificationEvent(
                IDENTITY,
                "Board",
                first.since,
                first.until,
                (1, 2, 129),
                operations=(("Contents modified",), ("Archived",), ("Moved lanes",)),
            )
        )
        dispatcher.submit(
            NotificationEvent(
                IDENTITY,
                "Board",
                first.since,
                first.until,
                (2, 3),
                operations=(("Created",), ("Contents modified",)),
            )
        )
        pending = dispatcher._pending  # noqa: SLF001 -- inspect bounded pending metadata
        assert pending is not None
        assert pending.tickets == tuple(range(1, MAX_TICKETS + 1))
        assert pending.operations == (
            ("Created", "Contents modified"),
            ("Created", "Archived"),
            ("Created", "Contents modified"),
        ) + (("Created",),) * (MAX_TICKETS - 3)
        assert pending.truncated
    finally:
        dispatcher.close(timeout=0)
        release.set()
        dispatcher.close()


@pytest.mark.parametrize(
    "operations",
    [(("Created",), ("Moved lanes",)), (("archived",),), ((),), (("Created", "Created"),)],
)
def test_event_rejects_invalid_operation_metadata(operations: tuple[tuple[str, ...], ...]) -> None:
    first = event()
    with pytest.raises(ValueError, match="operations"):
        NotificationEvent(
            IDENTITY,
            "Board",
            first.since,
            first.until,
            (1,),
            operations=cast("tuple[tuple[TicketOperation, ...], ...]", operations),
        )
