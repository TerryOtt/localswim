"""Read only bounded, validated writer-owned monitor metadata."""

from __future__ import annotations

import datetime as dt
import http.client
import json
import pathlib
import re
import urllib.parse
from typing import cast

from . import activity_feed, board_state, service_ownership

_CURSOR = re.compile(r"[0-9a-f]{32}:(?:0|[1-9][0-9]{0,19})")
_FIELDS = frozenset(
    {"schema", "boardIdentity", "cursor", "coverage", "events", "range", "fileIdentity"}
)
_EVENT_FIELDS = frozenset(
    {
        "ticket",
        "id",
        "kind",
        "at",
        "by",
        "from",
        "to",
        "ownerFrom",
        "ownerTo",
        "priorityFrom",
        "priorityTo",
        "commentChars",
        "attachmentId",
        "relationshipKind",
        "otherTicket",
        "otherId",
        "parentFromTicket",
        "parentFromId",
        "parentToTicket",
        "parentToId",
    }
)
_EVENT_REQUIRED = frozenset({"ticket", "id", "kind", "at", "by"})
_INTEGER_FIELDS = frozenset({"ticket", "otherTicket", "parentFromTicket", "parentToTicket"})
_NULLABLE_FIELDS = frozenset({"parentFromTicket", "parentToTicket", "parentFromId", "parentToId"})
_EVENT_KINDS = frozenset(
    {
        "created",
        "moved",
        "assigned",
        "prioritized",
        "commented",
        "renamed",
        "detail_changed",
        "archived",
        "unarchived",
        "attached",
        "attachment_removed",
        "cover_set",
        "cover_cleared",
        "linked",
        "unlinked",
        "parented",
        "unparented",
    }
)


def _unique(pairs: list[tuple[str, board_state.JsonValue]]) -> board_state.JsonObject:
    result: board_state.JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate metadata field")
        result[key] = value
    return result


def _instant(value: object) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError("invalid metadata timestamp")
    return board_state.parse_activity_bound(value, "monitor activity")


def _validate_event(raw: board_state.JsonValue, start: dt.datetime, end: dt.datetime) -> None:
    if not isinstance(raw, dict) or not _EVENT_REQUIRED <= raw.keys() or raw.keys() - _EVENT_FIELDS:
        raise ValueError("invalid activity event")
    for key, value in raw.items():
        if value is None and key in _NULLABLE_FIELDS:
            continue
        if key in _INTEGER_FIELDS:
            if type(value) is not int or value < 1:
                raise ValueError("invalid event ticket")
        elif key == "commentChars":
            if type(value) is not int or value < 0:
                raise ValueError("invalid event size")
        elif not isinstance(value, str) or not value:
            raise ValueError("invalid event field")
    if not start <= _instant(raw["at"]) <= end:
        raise ValueError("event outside requested interval")
    if raw["kind"] not in _EVENT_KINDS:
        raise ValueError("unknown activity kind")


def _validate(
    raw: board_state.JsonValue, path: pathlib.Path, start: dt.datetime, end: dt.datetime
) -> board_state.JsonObject:
    if not isinstance(raw, dict) or raw.keys() != _FIELDS:
        raise ValueError("invalid activity envelope")
    if type(raw["schema"]) is not int or raw["schema"] != 1:
        raise ValueError("invalid activity schema")
    if raw["boardIdentity"] != service_ownership.board_identity(path):
        raise ValueError("activity identity mismatch")
    cursor = raw["cursor"]
    if not isinstance(cursor, str) or _CURSOR.fullmatch(cursor) is None:
        raise ValueError("invalid activity cursor")
    coverage, interval, events = raw["coverage"], raw["range"], raw["events"]
    if (
        not isinstance(coverage, dict)
        or coverage.keys() != {"complete", "code"}
        or type(coverage["complete"]) is not bool
        or not isinstance(coverage["code"], str)
        or coverage["code"] not in activity_feed.COVERAGE_CODES
        or coverage["complete"] != (coverage["code"] == activity_feed.COMPLETE_CODE)
    ):
        raise ValueError("invalid activity coverage")
    if (
        not isinstance(interval, dict)
        or interval.keys() != {"since", "until"}
        or _instant(interval["since"]) != start
        or _instant(interval["until"]) != end
    ):
        raise ValueError("activity interval mismatch")
    if not isinstance(events, list) or len(events) > activity_feed.LIMIT_EVENTS:
        raise ValueError("invalid activity list")
    for event in events:
        _validate_event(event, start, end)
    stamp = raw["fileIdentity"]
    if stamp is not None and (
        not isinstance(stamp, list)
        or not 4 <= len(stamp) <= 16
        or any(type(part) is not int for part in stamp)
    ):
        raise ValueError("invalid file identity")
    if coverage["complete"] and (
        not events or stamp is None or tuple(stamp) != activity_feed.fingerprint(path)
    ):
        raise ValueError("activity file changed")
    return raw


def read_monitor_activity(
    path: pathlib.Path, start: str, end: str, cursor: str | None = None
) -> board_state.JsonObject:
    """Query the writer without a board load or admission-aware preflight.

    Failure never triggers a fallback board parse. The monitor must alert rather
    than suppress when metadata cannot prove complete coverage.
    """
    connection: http.client.HTTPConnection | None = None
    try:
        first, last = _instant(start), _instant(end)
        if last < first or (cursor is not None and _CURSOR.fullmatch(cursor) is None):
            raise ValueError("invalid activity request")
        descriptor = service_ownership.service_descriptor(path, require_cli_token=True)
        if (
            type(descriptor.get("monitorActivityVersion")) is not int
            or descriptor["monitorActivityVersion"] != 1
        ):
            raise ValueError("writer activity feed unavailable")
        query = {"since": first.isoformat(), "until": last.isoformat()}
        if cursor is not None:
            query["cursor"] = cursor
        connection = http.client.HTTPConnection(
            "127.0.0.1", cast("int", descriptor["port"]), timeout=5
        )
        connection.request(
            "GET",
            board_state.API_PREFIX + "/monitor-activity?" + urllib.parse.urlencode(query),
            headers={"Authorization": "Bearer " + cast("str", descriptor["token"])},
        )
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("writer activity request refused")
        data = response.read(activity_feed.MAX_RESPONSE_BYTES + 1)
        if len(data) > activity_feed.MAX_RESPONSE_BYTES:
            raise ValueError("writer activity response too large")
        raw = cast(
            "board_state.JsonValue", json.loads(data.decode("utf-8"), object_pairs_hook=_unique)
        )
        return _validate(raw, path, first, last)
    except (OSError, ValueError, RecursionError, http.client.HTTPException) as error:
        raise board_state.BoardError(
            "monitor activity unavailable; coverage is unproven"
        ) from error
    finally:
        if connection is not None:
            try:
                connection.close()
            except (OSError, ValueError, http.client.HTTPException) as error:
                raise board_state.BoardError(
                    "monitor activity unavailable; coverage is unproven"
                ) from error
