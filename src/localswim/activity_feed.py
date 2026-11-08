"""Bounded, writer-owned sanitized monitor evidence; uncertainty always alerts.

This is an in-memory cache, not a second durable journal. A service restart,
external replacement, retention gap or failed postcommit observation cannot prove
actor coverage. The legacy timestamp-addressed activity report remains separate.

Coverage concerns committed/admitted snapshots under the single-writer lease.
Observations around transaction selection/preparation also retain detected external
edits as gaps. Out-of-protocol transient edits overwritten between an observation
and atomic replacement are not reconstructable by this cache or the legacy audit.
"""

import copy
import ctypes
import datetime as dt
import hashlib
import json
import os
import pathlib
import uuid
from collections import deque
from dataclasses import dataclass
from typing import BinaryIO

from . import board_state, service_ownership

MAX_RECORDS = 256
MAX_EVENTS = 4096
MAX_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = MAX_BYTES + 8192
LIMIT_EVENTS = MAX_EVENTS
COMPLETE_CODE = "complete"
COVERAGE_CODES = frozenset(
    {
        COMPLETE_CODE,
        "cursor_mismatch",
        "retention_gap",
        "startup_gap",
        "coverage_gap",
        "window_gap",
        "file_changed",
        "empty",
    }
)
MIN_TIME = dt.datetime.min.replace(tzinfo=dt.UTC)


def _handle_stamp(stream: BinaryIO) -> tuple[int, ...]:
    """Use actual Windows ChangeTime, never Windows creation-time st_ctime."""
    info = os.fstat(stream.fileno())
    if os.name != "nt":
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    import msvcrt
    from ctypes import wintypes

    class BasicInfo(ctypes.Structure):
        _fields_ = (
            ("creation", ctypes.c_longlong),
            ("access", ctypes.c_longlong),
            ("write", ctypes.c_longlong),
            ("change", ctypes.c_longlong),
            ("attributes", wintypes.DWORD),
        )

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel.GetFileInformationByHandleEx
    get_info.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    get_info.restype = wintypes.BOOL
    basic = BasicInfo()
    if not get_info(
        msvcrt.get_osfhandle(stream.fileno()), 0, ctypes.byref(basic), ctypes.sizeof(basic)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return (info.st_dev, info.st_ino, info.st_size, basic.write, basic.change)


def fingerprint(path: pathlib.Path) -> tuple[int, ...]:
    """Observe identity and change metadata without reading board content."""
    with path.open("rb") as stream:
        return _handle_stamp(stream)


def bind_written(path: pathlib.Path, expected: bytes) -> tuple[int, ...] | None:
    """Bind exact committed content once, refusing a concurrently changing file.

    A stat after replace alone could bind an intervening external edit. Compare
    exact saved bytes under a stable handle stamp, then verify that path still
    identifies that object. Subsequent feed queries need metadata only.
    """
    with path.open("rb") as stream:
        before = _handle_stamp(stream)
        digest = hashlib.file_digest(stream, "sha256").digest()
        after = _handle_stamp(stream)
    if before != after or after != fingerprint(path):
        return None
    return after if digest == hashlib.sha256(expected).digest() else None


@dataclass(frozen=True)
class HistoryPosition:
    """Array offsets captured before one live command, without copying prose."""

    items: dict[str, tuple[int, int, int]]
    arrays: tuple[int, ...]


def position(board: board_state.Board) -> HistoryPosition:
    """Remember append-only audit positions before a command."""
    return HistoryPosition(
        {
            item.id: (len(item.history), len(item.comments), len(item.attachment_history))
            for item in board.items
        },
        tuple(len(getattr(board, field)) for field in _ARRAYS),
    )


_ARRAYS = (
    "relationship_history",
    "parent_history",
    "subject_history",
    "detail_history",
    "archive_history",
    "ticket_history",
)


def appended_events(
    board: board_state.Board, previous: HistoryPosition
) -> list[board_state.JsonObject]:
    """Reuse legacy sanitization over only new audit entries, preserving precision."""
    delta = copy.copy(board)
    delta.items = []
    for item in board.items:
        history, comments, attachments = previous.items.get(item.id, (0, 0, 0))
        detached = copy.copy(item)
        detached.history = item.history[history:]
        detached.comments = item.comments[comments:]
        detached.attachment_history = item.attachment_history[attachments:]
        delta.items.append(detached)
    for field, offset in zip(_ARRAYS, previous.arrays, strict=True):
        setattr(delta, field, getattr(board, field)[offset:])
    return [event.to_json() for event in board_state.activity_events(delta, MIN_TIME, None)]


@dataclass(frozen=True)
class Record:
    sequence: int
    observed: dt.datetime
    events: list[board_state.JsonObject]
    complete: bool
    size: int


class ActivityFeed:
    """Cache bounded commit metadata independently of board admission and health."""

    def __init__(self, path: pathlib.Path, data: bytes) -> None:
        self.path = path
        self.identity = service_ownership.board_identity(path)
        self.epoch = uuid.uuid4().hex
        self.started = dt.datetime.now(dt.UTC)
        self.sequence = 0
        self.records: deque[Record] = deque()
        self.bytes = 0
        self.events = 0
        self.evicted_at: dt.datetime | None = None
        try:
            self.stamp = bind_written(path, data)
        except OSError:
            self.stamp = None

    def observe(self) -> bool:
        """Record unknown external content once; never parse it on the feed path."""
        try:
            fresh = fingerprint(self.path)
        except OSError:
            fresh = None
        if self.stamp is None or fresh != self.stamp:
            self.stamp = None
            return False
        return True

    def append(self, events: list[board_state.JsonObject], *, complete: bool) -> None:
        """Retain whole commits only; oversized commits become explicit gaps."""
        # Match Handler._json's ASCII escaping, including non-ASCII identities.
        size = len(json.dumps(events).encode("utf-8"))
        if size > MAX_BYTES or len(events) > MAX_EVENTS:
            events, size, complete = [], 2, False
        self.sequence += 1
        self.records.append(Record(self.sequence, dt.datetime.now(dt.UTC), events, complete, size))
        self.bytes += size
        self.events += len(events)
        while len(self.records) > MAX_RECORDS or self.bytes > MAX_BYTES or self.events > MAX_EVENTS:
            removed = self.records.popleft()
            self.bytes -= removed.size
            self.events -= len(removed.events)
            self.evicted_at = removed.observed

    def publish(
        self, board: board_state.Board, previous: HistoryPosition, *, predecessor: bool
    ) -> list[board_state.JsonObject]:
        """Optional postcommit publication; caller contains failures after replacement."""
        data = (json.dumps(board.to_json(), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        stamp = bind_written(self.path, data)
        events = appended_events(board, previous)
        self.append(events, complete=predecessor and stamp is not None and bool(events))
        self.stamp = stamp
        return events

    def query(
        self, start: dt.datetime, end: dt.datetime | None, cursor: str | None = None
    ) -> board_state.JsonObject:
        """Return bounded evidence plus an explicit complete-coverage decision."""
        code = "complete"
        after = 0
        if cursor is not None:
            epoch, separator, number = cursor.partition(":")
            if not separator or epoch != self.epoch or not number.isdecimal():
                code = "cursor_mismatch"
            else:
                after = int(number)
                oldest = self.records[0].sequence - 1 if self.records else self.sequence
                if not oldest <= after <= self.sequence:
                    code = "retention_gap"
        elif start < self.started:
            code = "startup_gap"
        elif self.evicted_at is not None and start <= self.evicted_at:
            code = "retention_gap"
        current = self.observe()
        selected = [
            record
            for record in self.records
            if record.sequence > after and (cursor is not None or record.observed >= start)
        ]
        events: list[board_state.JsonValue] = []
        for record in selected:
            if not record.complete:
                code = "coverage_gap"
            for event in record.events:
                instant = board_state.parse_activity_bound(str(event["at"]), "activity")
                if instant < start or (end is not None and instant > end):
                    code = "window_gap"
                else:
                    events.append(copy.deepcopy(event))
        if not current:
            code = "file_changed"
        if not events and code == "complete":
            code = "empty"
        return {
            "schema": 1,
            "boardIdentity": self.identity,
            "cursor": f"{self.epoch}:{self.sequence}",
            "range": {"since": start.isoformat(), "until": end.isoformat() if end else None},
            "fileIdentity": list(self.stamp) if current and self.stamp is not None else None,
            "coverage": {"complete": code == COMPLETE_CODE, "code": code},
            "events": events,
        }
