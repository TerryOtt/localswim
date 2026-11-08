# Private helpers are shared only across this internal domain-module boundary.
# pyright: reportPrivateUsage=false
"""Dependency-first ticket permutations and their replayable, immutable audit."""

from __future__ import annotations

import heapq
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

MAX_TICKET_ORDER = 10_000


@dataclass(frozen=True)
class TicketChange:
    id: str
    frm: int
    to: int

    def to_json(self) -> model.JsonObject:
        return {"id": self.id, "from": self.frm, "to": self.to}


@dataclass(frozen=True)
class TicketBatch:
    at: str
    by: str
    lane: str
    changes: tuple[TicketChange, ...]

    def to_json(self) -> model.JsonObject:
        return {
            "at": self.at,
            "by": self.by,
            "lane": self.lane,
            "changes": [change.to_json() for change in self.changes],
        }


# Board declares fields using the immutable record types above during import.
# Load its runtime helpers only after those records exist, in either import order.
from . import board_state as model  # noqa: E402


def _positive(raw: model.JsonValue, where: str) -> int:
    if type(raw) is not int or raw < 1:
        raise model.BoardError(f"{where} must be a positive integer")
    return raw


def _parse_change(raw: model.JsonValue, where: str, known: set[str]) -> TicketChange:
    if not isinstance(raw, dict) or set(raw) != {"id", "from", "to"}:
        raise model.BoardError(f"{where} must contain id, from and to")
    card_id = raw["id"]
    if not isinstance(card_id, str) or card_id not in known:
        raise model.BoardError(f"{where} names an unknown card")
    return TicketChange(
        card_id, _positive(raw["from"], f"{where}.from"), _positive(raw["to"], f"{where}.to")
    )


def parse_history(raw: model.JsonValue, board: model.Board) -> list[TicketBatch]:
    if not isinstance(raw, list):
        raise model.BoardError("ticketHistory must be a list")
    known = {item.id for item in board.items}
    result: list[TicketBatch] = []
    for index, value in enumerate(raw):
        where = f"ticketHistory[{index}]"
        if not isinstance(value, dict) or set(value) != {"at", "by", "lane", "changes"}:
            raise model.BoardError(f"{where} must contain at, by, lane and changes")
        at, by, lane, changes = (value[key] for key in ("at", "by", "lane", "changes"))
        if not isinstance(at, str):
            raise model.BoardError(f"{where}.at must be a timestamp")
        model._parse_activity_bound(at, f"{where}.at")  # noqa: SLF001 -- shared strict parser
        if not isinstance(by, str) or by not in {board.cli_user, board.browser_user}:
            raise model.BoardError(f"{where}.by requires the configured CLI or browser actor")
        if not isinstance(cast("object", lane), str) or lane not in board.policy.states:
            raise model.BoardError(f"{where}.lane names an unknown lane")
        if not isinstance(changes, list) or not changes:
            raise model.BoardError(f"{where}.changes must be a nonempty list")
        result.append(
            TicketBatch(
                at,
                by,
                lane,
                tuple(
                    _parse_change(change, f"{where}.changes[{n}]", known)
                    for n, change in enumerate(changes)
                ),
            )
        )
    return result


def audit_problems(board: model.Board) -> list[str]:
    """Reverse whole permutations; origins must recover the initial global mapping."""
    try:
        batches = parse_history([batch.to_json() for batch in board.ticket_history], board)
        current = {item.id: _positive(item.ticket, f"{item.id}.ticket") for item in board.items}
        anchors = {
            item.id: _positive(item.storage_ticket, f"{item.id}.originTicket")
            for item in board.items
        }
        if len(set(current.values())) != len(current):
            raise model.BoardError("ticketHistory: current tickets are not globally unique")
        if len(set(anchors.values())) != len(anchors):
            raise model.BoardError("originTicket anchors are not globally unique")
        if any(ticket >= board.next_ticket for ticket in (*current.values(), *anchors.values())):
            raise model.BoardError("ticketHistory: ticket anchors must be below nextTicket")
        touched: set[str] = set()
        for index in reversed(range(len(batches))):
            changes = batches[index].changes
            ids = {change.id for change in changes}
            before, after = {change.frm for change in changes}, {change.to for change in changes}
            if len(ids) != len(changes) or len(before) != len(changes) or before != after:
                raise model.BoardError(f"ticketHistory[{index}] is not a closed unique permutation")
            for change in changes:
                if change.frm == change.to:
                    raise model.BoardError(f"ticketHistory[{index}] contains an identity change")
                if current[change.id] != change.to:
                    raise model.BoardError(f"ticketHistory[{index}] disagrees with ticket replay")
            current.update((change.id, change.frm) for change in changes)
            touched.update(ids)
        if current != anchors:
            raise model.BoardError("ticketHistory replay disagrees with originTicket anchors")
        origins = {item.id for item in board.items if item.origin_ticket is not None}
        if origins != touched:
            raise model.BoardError(
                "originTicket must exist exactly for cards changed in ticketHistory"
            )
    except model.BoardError as exc:
        return [str(exc)]
    return []


def _selected(board: model.Board, lane: str, refs: Sequence[str] | None) -> list[model.Item]:
    if not isinstance(cast("object", lane), str) or lane not in board.policy.states:
        raise model.BoardError("ticket ordering requires an existing lane")
    if lane in board.policy.retired_lanes:
        raise model.BoardError("ticket ordering cannot use a retired lane")
    if refs is None:
        selected = [item for item in board.items if item.state == lane and not item.archived]
    else:
        if (
            not isinstance(cast("object", refs), Sequence)
            or isinstance(refs, (str, bytes))
            or any(not isinstance(cast("object", ref), str) for ref in refs)
        ):
            raise model.BoardError("ticket ordering refs must be a sequence of strings")
        selected = [board.find(ref) for ref in refs]
    if len(selected) > MAX_TICKET_ORDER:
        raise model.BoardError(f"ticket ordering supports at most {MAX_TICKET_ORDER} cards")
    if len({item.id for item in selected}) != len(selected):
        raise model.BoardError("ticket ordering refuses duplicate card references")
    if any(item.archived or item.state != lane for item in selected):
        raise model.BoardError("ticket ordering requires unarchived cards in the selected lane")
    return selected


def _component_edges(
    graph: dict[str, set[str]], groups: list[set[str]]
) -> tuple[dict[str, int], list[set[int]], list[int]]:
    membership = {node: i for i, group in enumerate(groups) for node in group}
    edges: list[set[int]] = [set() for _ in groups]
    indegrees = [0] * len(groups)
    for source, targets in graph.items():
        for target in targets:
            a, b = membership[source], membership[target]
            if a != b and b not in edges[a]:
                edges[a].add(b)
                indegrees[b] += 1
    return membership, edges, indegrees


def _ordered(board: model.Board, selected: list[model.Item]) -> list[model.Item]:
    """Drain unselected SCCs freely, choosing eligible selected cards by old ticket."""
    graph: dict[str, set[str]] = {item.id: set() for item in board.items}
    for link in board.links:
        if link.kind == "blocks":
            graph[link.frm].add(link.to)
    groups = model._blocking_components(graph)  # noqa: SLF001 -- shared iterative SCC traversal
    membership, edges, indegrees = _component_edges(graph, groups)
    selected_groups: dict[int, model.Item] = {}
    for card in selected:
        group = membership[card.id]
        if len(groups[group]) > 1 or card.id in graph[card.id]:
            raise model.BoardError(f"ticket ordering refused: blocking cycle involves {card.id}")
        selected_groups[group] = card
    free: deque[int] = deque()
    ready: list[tuple[int, str, int]] = []

    def enqueue(group: int) -> None:
        if group in selected_groups:
            item = selected_groups[group]
            heapq.heappush(ready, (item.ticket, item.id, group))
        else:
            free.append(group)

    def finish(group: int) -> None:
        for child in edges[group]:
            indegrees[child] -= 1
            if not indegrees[child]:
                enqueue(child)

    for group, degree in enumerate(indegrees):
        if not degree:
            enqueue(group)
    ordered: list[model.Item] = []
    while free or ready:
        while free:
            finish(free.popleft())
        if ready:
            _, _, group = heapq.heappop(ready)
            ordered.append(selected_groups[group])
            finish(group)
    return ordered


def plan(board: model.Board, lane: str, refs: Sequence[str] | None) -> model.JsonObject:
    selected = _selected(board, lane, refs)
    ordered = _ordered(board, selected)
    tickets = sorted(item.ticket for item in selected)
    return {
        "lane": lane,
        "selectedCount": len(selected),
        "changedCount": sum(
            item.ticket != number for item, number in zip(ordered, tickets, strict=True)
        ),
        "cards": [
            {"id": item.id, "subject": item.subject, "oldTicket": item.ticket, "newTicket": number}
            for item, number in zip(ordered, tickets, strict=True)
        ],
    }


def apply(board: model.Board, lane: str, refs: Sequence[str] | None, by: str) -> model.JsonObject:
    if not by or by not in {board.cli_user, board.browser_user}:
        raise model.BoardError("ticket ordering requires the configured CLI or browser actor")
    problems = audit_problems(board)
    if problems:
        raise model.BoardError(problems[0])
    result = plan(board, lane, refs)
    # The plan is deliberately detached JSON; mutation uses stable IDs captured here.
    cards = cast("list[model.JsonObject]", result["cards"])
    changes = [
        TicketChange(
            cast("str", row["id"]), cast("int", row["oldTicket"]), cast("int", row["newTicket"])
        )
        for row in cards
        if row["oldTicket"] != row["newTicket"]
    ]
    if changes:
        batch = TicketBatch(model.now(), by, lane, tuple(changes))
        lookup = {item.id: item for item in board.items}
        for change in changes:
            item = lookup[change.id]
            if item.origin_ticket is None:
                item.origin_ticket = item.ticket
            item.ticket = change.to
        board.ticket_history.append(batch)
    return result


def validate_schema(raw: model.JsonObject, where: str) -> None:
    schema = raw.get("schema")
    if type(schema) is not int or schema not in {model.SCHEMA, model.TICKET_ORDER_SCHEMA}:
        raise model.BoardError(f"{where}: schema {schema!r}, this build reads 4 or 6")
    items = raw.get("items")
    origins = isinstance(items, list) and any(
        isinstance(item, dict) and "originTicket" in item for item in items
    )
    if schema == model.SCHEMA and ("ticketHistory" in raw or origins):
        raise model.BoardError(f"{where}: ticketHistory/originTicket require schema 6")


def load_metadata(board: model.Board, raw: model.JsonObject, where: str) -> None:
    if raw["schema"] != model.TICKET_ORDER_SCHEMA:
        return
    board.ticket_history = parse_history(raw.get("ticketHistory", []), board)
    if not board.ticket_history:
        raise model.BoardError(f"{where}: schema 6 requires ticket history")
    problems = audit_problems(board)
    if problems:
        raise model.BoardError(f"{where}: {problems[0]}")


def rewrite_lane(raw: model.JsonObject, source: str, destination: str) -> int:
    """Follow an explicit lane-ID migration in the historical batch lane field."""
    history = raw.get("ticketHistory", [])
    if not isinstance(history, list):
        raise model.BoardError("ticketHistory must be a list")
    replacements = 0
    for index, batch in enumerate(history):
        if not isinstance(batch, dict):
            raise model.BoardError(f"ticketHistory[{index}] must be an object")
        if batch.get("lane") == source:
            batch["lane"] = destination
            replacements += 1
    return replacements
