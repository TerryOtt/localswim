"""Opt-in, best-effort owner nudges independent of board persistence."""

import hashlib
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, cast

from .board_state import JsonObject, JsonValue

MAX_CONFIG_BYTES = 4096
MAX_TICKETS = 128
type TicketOperation = Literal[
    "Created",
    "Contents modified",
    "Comment added",
    "Reviewed & approved",
    "Archived",
    "Restored",
    "Moved lanes",
    "Ticket numbers reordered",
]
OPERATIONS: tuple[TicketOperation, ...] = (
    "Created",
    "Contents modified",
    "Comment added",
    "Ticket numbers reordered",
    "Moved lanes",
    "Reviewed & approved",
    "Archived",
    "Restored",
)


def event_operation(kind: JsonValue, *, approved: bool = False) -> TicketOperation:
    """Classify sanitized audit kinds, with approval supplied by the exact command."""
    if approved and kind in ("commented", "moved"):
        return "Reviewed & approved"
    labels: dict[str, TicketOperation] = {
        "created": "Created",
        "commented": "Comment added",
        "archived": "Archived",
        "unarchived": "Restored",
        "moved": "Moved lanes",
        "ticket_changed": "Ticket numbers reordered",
    }
    return labels.get(kind, "Contents modified") if isinstance(kind, str) else "Contents modified"


def merge_operations(
    first: tuple[TicketOperation, ...], second: tuple[TicketOperation, ...]
) -> tuple[TicketOperation, ...]:
    """Keep every applicable label once in fixed display order."""
    return tuple(operation for operation in OPERATIONS if operation in first or operation in second)


class NotificationConfigError(ValueError):
    """Owner notification preferences are unavailable or invalid."""


@dataclass(frozen=True)
class NotificationSettings:
    board_identity: str
    thread_id: str | None = None
    enabled: bool = False


def _identity(value: str) -> str:
    if not value or len(value) > 1024 or any(ord(c) < 32 for c in value):
        raise NotificationConfigError("invalid notification board identity")
    return value


def _thread(value: str | None) -> str | None:
    if value is not None:
        try:
            if str(uuid.UUID(value)) != value:
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise NotificationConfigError("notification thread must be a canonical UUID") from exc
    return value


def settings_path(board_identity: str, *, config_root: pathlib.Path | None = None) -> pathlib.Path:
    """Return the per-user path without creating directories or reading preferences."""
    identity = _identity(board_identity)
    if config_root is None:
        if sys.platform == "win32":
            config_root = pathlib.Path(
                os.environ.get("LOCALAPPDATA", pathlib.Path.home() / "AppData/Local")
            )
        elif sys.platform == "darwin":
            config_root = pathlib.Path.home() / "Library/Application Support"
        else:
            config_root = pathlib.Path(
                os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config")
            )
        config_root = config_root / "localswim" / "notifications"
    return config_root / (hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".json")


def _unique(pairs: list[tuple[str, JsonValue]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise NotificationConfigError("duplicate notification setting")
        result[key] = value
    return result


def load_settings(
    board_identity: str, *, config_root: pathlib.Path | None = None
) -> NotificationSettings:
    path = settings_path(board_identity, config_root=config_root)
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return NotificationSettings(board_identity)
    except OSError as exc:
        raise NotificationConfigError("notification settings unavailable") from exc
    try:
        if len(data) > MAX_CONFIG_BYTES:
            raise ValueError
        raw = cast(JsonValue, json.loads(data.decode("utf-8"), object_pairs_hook=_unique))
        if not isinstance(raw, dict) or set(raw) != {
            "schema",
            "boardIdentity",
            "threadId",
            "enabled",
        }:
            raise ValueError
        if (
            type(raw["schema"]) is not int
            or raw["schema"] != 1
            or raw["boardIdentity"] != board_identity
        ):
            raise ValueError
        target = raw["threadId"]
        enabled = raw["enabled"]
        if (target is not None and not isinstance(target, str)) or not isinstance(enabled, bool):
            raise ValueError
        return NotificationSettings(board_identity, _thread(target), enabled)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise NotificationConfigError("invalid notification settings") from exc


def _save(settings: NotificationSettings, config_root: pathlib.Path | None) -> NotificationSettings:
    path = settings_path(settings.board_identity, config_root=config_root)
    payload = json.dumps(
        {
            "schema": 1,
            "boardIdentity": settings.board_identity,
            "threadId": settings.thread_id,
            "enabled": settings.enabled,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    if len(payload) > MAX_CONFIG_BYTES:
        raise NotificationConfigError("notification settings too large")
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".notification-", delete=False
        ) as stream:
            temporary = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise NotificationConfigError("notification settings unavailable") from exc
    finally:
        if temporary is not None:
            pathlib.Path(temporary).unlink(missing_ok=True)
    return settings


def save_target(
    board_identity: str, thread_id: str | None, *, config_root: pathlib.Path | None = None
) -> NotificationSettings:
    current = load_settings(board_identity, config_root=config_root)
    return _save(replace(current, thread_id=_thread(thread_id)), config_root)


def set_enabled(
    board_identity: str, enabled: bool, *, config_root: pathlib.Path | None = None
) -> NotificationSettings:
    if type(enabled) is not bool:
        raise NotificationConfigError("enabled must be a boolean")
    current = load_settings(board_identity, config_root=config_root)
    return _save(replace(current, enabled=enabled), config_root)


@dataclass(frozen=True)
class NotificationEvent:
    board_identity: str
    label: str
    since: str
    until: str
    tickets: tuple[int, ...] = ()
    truncated: bool = False
    operations: tuple[tuple[TicketOperation, ...], ...] = ()

    def __post_init__(self) -> None:
        _identity(self.board_identity)
        if not self.label or len(self.label) > 160 or any(ord(c) < 32 for c in self.label):
            raise ValueError("invalid notification label")
        start, end = datetime.fromisoformat(self.since), datetime.fromisoformat(self.until)
        if start.tzinfo is None or end.tzinfo is None or start > end:
            raise ValueError("notification timestamps must be ordered and timezone aware")
        if len(self.tickets) > MAX_TICKETS or any(
            type(t) is not int or t < 1 for t in self.tickets
        ):
            raise ValueError("invalid notification tickets")
        if self.operations and (
            len(self.operations) != len(self.tickets)
            or any(
                not labels
                or len(labels) > len(OPERATIONS)
                or len(set(labels)) != len(labels)
                or any(operation not in OPERATIONS for operation in labels)
                for labels in self.operations
            )
        ):
            raise ValueError("invalid notification operations")
        if type(self.truncated) is not bool:
            raise ValueError("invalid notification truncation flag")

    def resolved_operations(self) -> tuple[tuple[TicketOperation, ...], ...]:
        default: tuple[TicketOperation, ...] = ("Contents modified",)
        return self.operations or (default,) * len(self.tickets)

    def payload(self) -> JsonObject:
        return {
            "boardIdentity": self.board_identity,
            "label": self.label,
            "since": self.since,
            "until": self.until,
            "tickets": list(self.tickets),
            "truncated": self.truncated,
            "operations": [list(labels) for labels in self.resolved_operations()],
        }


class NotificationDispatcher:
    """One bounded pending burst and one daemon worker per owning board service.

    submit only merges immutable metadata under a short condition lock. All config
    and delivery I/O happens in the worker, outside that lock; failed bursts are lost.
    """

    def __init__(
        self,
        board_identity: str,
        deliver: Callable[[str, JsonObject, threading.Event], str],
        *,
        quiet_seconds: float = 15.0,
        config_root: pathlib.Path | None = None,
        settings_loader: Callable[[], NotificationSettings] | None = None,
    ) -> None:
        self.board_identity = _identity(board_identity)
        if not 0 <= quiet_seconds <= 3600:
            raise ValueError("invalid notification quiet period")
        self._quiet = quiet_seconds
        self._deliver = deliver
        self._load = settings_loader or (
            lambda: load_settings(board_identity, config_root=config_root)
        )
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._pending: NotificationEvent | None = None
        self._deadline = 0.0
        self._outcome = "idle"
        self._worker = threading.Thread(
            target=self._run, name="localswim-owner-notifications", daemon=True
        )
        self._worker.start()

    def submit(self, event: NotificationEvent) -> None:
        if event.board_identity != self.board_identity:
            return
        with self._condition:
            if self._stop.is_set():
                return
            previous = self._pending
            if previous is not None:
                operations: dict[int, tuple[TicketOperation, ...]] = {}
                for notice in (previous, event):
                    for ticket, operation in zip(
                        notice.tickets, notice.resolved_operations(), strict=True
                    ):
                        operations[ticket] = merge_operations(operations.get(ticket, ()), operation)
                tickets = sorted(operations)
                event = replace(
                    event,
                    since=min(previous.since, event.since, key=datetime.fromisoformat),
                    until=max(previous.until, event.until, key=datetime.fromisoformat),
                    tickets=tuple(tickets[:MAX_TICKETS]),
                    operations=tuple(operations[ticket] for ticket in tickets[:MAX_TICKETS]),
                    truncated=previous.truncated or event.truncated or len(tickets) > MAX_TICKETS,
                )
            self._pending = event
            self._deadline = time.monotonic() + self._quiet
            self._condition.notify()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._condition:
                while self._pending is None and not self._stop.is_set():
                    self._condition.wait()
                if self._stop.is_set():
                    return
            # Observe disabled preferences promptly without holding the pending lock.
            try:
                settings = self._load()
            except Exception:  # noqa: BLE001 -- best-effort boundary; never log private error text
                self._discard("config_failed")
                continue
            with self._condition:
                if self._stop.is_set():
                    return
                if not settings.enabled or settings.thread_id is None:
                    self._pending = None
                    self._outcome = "disabled"
                    continue
                remaining = self._deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(remaining)
                    continue
                event, self._pending = self._pending, None
            if event is None or self._stop.is_set():
                continue
            try:
                # Resolve the current target at the delivery boundary, never at submit.
                settings = self._load()
                if not settings.enabled or settings.thread_id is None:
                    outcome = "disabled"
                else:
                    outcome = self._deliver(settings.thread_id, event.payload(), self._stop)
                    outcome = "queued" if outcome == "accepted" else "delivery_failed"
            except Exception:  # noqa: BLE001 -- no retry and no private errors cross this boundary
                outcome = "delivery_failed"
            with self._condition:
                self._outcome = outcome

    def _discard(self, outcome: str) -> None:
        with self._condition:
            self._pending = None
            self._outcome = outcome

    def status(self) -> JsonObject:
        with self._condition:
            return {
                "pending": self._pending is not None,
                "outcome": self._outcome,
                "stopped": self._stop.is_set(),
            }

    def close(self, timeout: float = 1.0) -> None:
        self._stop.set()
        with self._condition:
            self._pending = None
            self._condition.notify_all()
            worker = self._worker
        worker.join(max(0.0, min(timeout, 5.0)))
