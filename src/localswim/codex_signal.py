"""One best-effort metadata notice through the supported local ``codex queue`` CLI.

Success means accepted by the queue, not consumed or completed. Desktop may
immediately start a turn in an idle chat. This adapter does not change session
settings or retry uncertain delivery. Call only from the background worker.
"""

import contextlib
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from .notifications import OPERATIONS, TicketOperation, merge_operations

type DeliveryCode = Literal[
    "accepted", "unavailable", "rejected", "invalid", "timeout", "stopped", "protocol_error"
]

_TIMEOUT = 5.0
_POLL = 0.025
_CLEANUP_TIMEOUT = 0.5
_MAX_MESSAGE_BYTES = 32 * 1024
_FIELDS = frozenset({"boardIdentity", "label", "since", "until", "tickets", "truncated"})


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(UTC)


def _ticket_operations(
    tickets: list[object], operations: object
) -> dict[int, tuple[TicketOperation, ...]]:
    if not isinstance(operations, list):
        raise ValueError
    operation_values = cast("list[object]", operations)
    if len(operation_values) != len(tickets):
        raise ValueError
    for labels in operation_values:
        if not isinstance(labels, list):
            raise ValueError
        values = cast("list[object]", labels)
        if not 1 <= len(values) <= len(OPERATIONS) or any(
            label not in OPERATIONS for label in values
        ):
            raise ValueError
        if len(set(cast("list[str]", values))) != len(values):
            raise ValueError
    by_ticket: dict[int, tuple[TicketOperation, ...]] = {}
    for ticket, labels in zip(
        cast("list[int]", tickets),
        cast("list[list[TicketOperation]]", operations),
        strict=True,
    ):
        by_ticket[ticket] = merge_operations(by_ticket.get(ticket, ()), tuple(labels))
    return by_ticket


def _message(thread_id: str, payload: Mapping[str, object]) -> str:
    if str(uuid.UUID(thread_id)) != thread_id or frozenset(payload) not in (
        _FIELDS,
        _FIELDS | {"operations"},
    ):
        raise ValueError
    identity, label = payload["boardIdentity"], payload["label"]
    if not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{64}", identity) is None:
        raise ValueError
    if (
        not isinstance(label, str)
        or not 1 <= len(label) <= 160
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in label)
    ):
        raise ValueError
    since, until = _timestamp(payload["since"]), _timestamp(payload["until"])
    if since > until:
        raise ValueError
    tickets, truncated = payload["tickets"], payload["truncated"]
    if not isinstance(tickets, list) or type(truncated) is not bool:
        raise ValueError
    ticket_values = cast("list[object]", tickets)
    if len(ticket_values) > 128 or any(
        type(ticket) is not int or ticket < 1 for ticket in ticket_values
    ):
        raise ValueError
    by_ticket = _ticket_operations(
        ticket_values, payload.get("operations", [["Contents modified"] for _ in ticket_values])
    )
    ticket_text = (
        ", ".join(
            f"{operation} ({', '.join(f'{ticket:04d}' for ticket in matching)})"
            for operation in OPERATIONS
            if (
                matching := [
                    ticket for ticket in sorted(by_ticket) if operation in by_ticket[ticket]
                ]
            )
        )
        or "unspecified"
    )
    if truncated:
        ticket_text += " (additional tickets omitted)"
    # Round outward so the displayed whole-second interval contains every edit.
    since = since.replace(microsecond=0)
    if until.microsecond:
        until = until.replace(microsecond=0) + timedelta(seconds=1)
    message = (
        "localswim board-change notice\n\n"
        f"- Board: {label} (`{identity[:4]}...{identity[-4:]}`)\n"
        f"- Start: {since:%Y-%m-%d %H:%M:%S} UTC\n"
        f"- End: {until:%Y-%m-%d %H:%M:%S} UTC\n"
        f"- Tickets: {ticket_text}"
    )
    if len(message.encode("utf-8")) > _MAX_MESSAGE_BYTES:
        raise ValueError
    return message


def _cleanup(process: subprocess.Popen[bytes]) -> None:
    # Stop only the exact CLI child owned here, never Desktop or another session.
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=_CLEANUP_TIMEOUT)
        except OSError, subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                process.kill()
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                process.wait(timeout=_CLEANUP_TIMEOUT)


def deliver(
    thread_id: str, payload: Mapping[str, object], stop_event: threading.Event | None = None
) -> DeliveryCode:
    """Queue once and return a fixed safe code, without logging data or errors.

    Timeout, cancellation and errors can follow an accepted enqueue. Drop these
    outcomes; callers must not replay them. The five-second CLI budget excludes
    bounded owned-child cleanup. There is no durable delivery history here.
    """
    if stop_event is not None and stop_event.is_set():
        return "stopped"
    try:
        message = _message(thread_id, payload)
    except TypeError, ValueError, AttributeError, OverflowError, UnicodeError:
        return "invalid"
    windows = sys.platform == "win32"
    executable = shutil.which("codex.exe" if windows else "codex")
    # Windows may implicitly run .cmd/.bat via a shell even with shell=False.
    # Keep cancellation ownership to one native CLI process, never an npm wrapper.
    if executable is None or (windows and not executable.lower().endswith(".exe")):
        return "unavailable"
    deadline = time.monotonic() + _TIMEOUT
    try:
        process = subprocess.Popen(  # noqa: S603 - resolved local CLI, fixed queue arguments
            [executable, "queue", "--thread", thread_id, "--message", message],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0,
        )
    except OSError:
        return "unavailable"
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                return "stopped"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            try:
                exit_code = process.wait(timeout=min(_POLL, remaining))
            except subprocess.TimeoutExpired:
                continue
            return "accepted" if exit_code == 0 else "rejected"
    except OSError:
        return "protocol_error"
    finally:
        _cleanup(process)
