# Internal client shares the qualified service preflight and single-submit transport.
# pyright: reportPrivateUsage=false
"""Single-submit CLI operations for previewed dependency ticket ordering."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, cast

from . import board_state

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Sequence

MAX_PREVIEW_BYTES = 32 * 1024 * 1024


def _revision(value: board_state.JsonValue) -> int:
    if type(value) is not int or value < 0:
        raise board_state.BoardError("ticket-order response has an invalid revision")
    return value


def _validate(value: board_state.JsonObject, lane: str) -> board_state.JsonObject:
    _revision(value.get("revision"))
    plan = value.get("plan")
    if (
        not isinstance(value.get("result"), str)
        or type(value.get("applied")) is not bool
        or not isinstance(plan, dict)
        or plan.get("lane") != lane
        or type(plan.get("selectedCount")) is not int
        or type(plan.get("changedCount")) is not int
        or not isinstance(plan.get("cards"), list)
    ):
        raise board_state.BoardError("ticket-order response is incomplete or invalid")
    cards = cast("list[board_state.JsonValue]", plan["cards"])
    ids: set[str] = set()
    old: set[int] = set()
    new: list[int] = []
    new_seen: set[int] = set()
    changed = 0
    for row in cards:
        if (
            not isinstance(row, dict)
            or set(row) != {"id", "subject", "oldTicket", "newTicket"}
            or not isinstance(row["id"], str)
            or not row["id"]
            or not isinstance(row["subject"], str)
            or type(row["oldTicket"]) is not int
            or type(row["newTicket"]) is not int
        ):
            raise board_state.BoardError("ticket-order response contains an invalid card")
        before, after = row["oldTicket"], row["newTicket"]
        if before < 1 or after < 1 or row["id"] in ids or before in old or after in new_seen:
            raise board_state.BoardError(
                "ticket-order response contains duplicate or invalid identities"
            )
        ids.add(row["id"])
        old.add(before)
        new.append(after)
        new_seen.add(after)
        changed += before != after
    if (
        old != set(new)
        or new != sorted(new)
        or plan["selectedCount"] != len(cards)
        or plan["changedCount"] != changed
    ):
        raise board_state.BoardError("ticket-order response is not a complete permutation")
    return value


def _preview(
    base: str, token: str, lane: str, cards: Sequence[str] | None, expected: int | None
) -> board_state.JsonObject:
    body = json.dumps({"lane": lane, "cards": list(cards) if cards is not None else None}).encode()
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    if expected is not None:
        headers["If-Match"] = f'"revision-{expected}"'
    request = urllib.request.Request(  # noqa: S310 -- validated loopback descriptor
        base + "/board/ticket-order/preview", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            raw = response.read(MAX_PREVIEW_BYTES + 1)
            if len(raw) > MAX_PREVIEW_BYTES:
                raise ValueError("preview exceeds response bound")
            value = json.loads(raw)
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read(65536)).get("error", f"HTTP {error.code}")
        except ValueError, AttributeError, OSError, http.client.HTTPException:
            detail = f"HTTP {error.code}"
        finally:
            error.close()
        raise board_state.BoardError(
            f"ticket-order preview refused: {detail}; nothing changed"
        ) from error
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise board_state.BoardError(
            "could not read ticket-order preview; nothing changed"
        ) from error
    if not isinstance(value, dict):
        raise board_state.BoardError("invalid ticket-order preview; nothing changed")
    validated = _validate(cast("board_state.JsonObject", value), lane)
    if validated["applied"]:
        raise board_state.BoardError("invalid preview acknowledgement")
    return validated


def order_tickets(
    path: pathlib.Path,
    lane: str,
    cards: Sequence[str] | None = None,
    *,
    apply: bool = False,
    expected_revision: int | None = None,
) -> board_state.JsonObject:
    """Preview first, then make at most one revision-bound apply request."""
    descriptor = board_state._service_descriptor(path)  # noqa: SLF001 -- shared identity preflight
    base = f"http://{descriptor['host']}:{descriptor['port']}" + board_state.API_PREFIX
    token = str(descriptor["token"])
    preview = _preview(base, token, lane, cards, expected_revision)
    plan = cast("board_state.JsonObject", preview["plan"])
    if not apply or plan["changedCount"] == 0:
        if apply:
            preview["result"] = "Ticket order already matches dependencies"
        return preview
    selected = cast("list[board_state.JsonObject]", plan["cards"])
    revision = _revision(preview["revision"])
    response = board_state._service_json(  # noqa: SLF001 -- shared single-submit mutation transport
        base + "/board/ticket-order/apply",
        token,
        body={"lane": lane, "cards": [row["id"] for row in selected]},
        revision=revision,
    )
    try:
        result = _validate(response, lane)
        if not result["applied"] or result["revision"] != revision + 1 or result["plan"] != plan:
            raise board_state.BoardError("inconsistent ticket-order acknowledgement")
    except board_state.BoardError as error:
        raise board_state.BoardError(
            "ticket-order outcome is unconfirmed; inspect the board before retrying"
        ) from error
    return result


def set_button(path: pathlib.Path, lane: str, enabled: bool) -> board_state.JsonObject:
    """Change one lane's opt-in UI setting through its current live writer."""
    descriptor = board_state._service_descriptor(path)  # noqa: SLF001 -- shared identity preflight
    base = f"http://{descriptor['host']}:{descriptor['port']}" + board_state.API_PREFIX
    token = str(descriptor["token"])
    status = board_state._service_json(base + "/status", token)  # noqa: SLF001
    if status.get("ok") is not True:
        raise board_state.BoardError("board health is not confirmed; nothing changed")
    response = board_state._service_json(  # noqa: SLF001
        base + "/board/ticket-order/config",
        token,
        body={"lane": lane, "enabled": enabled},
        revision=_revision(status.get("revision")),
    )
    if not isinstance(response.get("result"), str) or response.get("enabled") is not enabled:
        raise board_state.BoardError("configuration outcome unconfirmed; inspect before retrying")
    _revision(response.get("revision"))
    return response
