"""Ticket permutation planning, replay, and preservation of stable card identity."""

import copy
import datetime as dt
from typing import cast

import pytest

from localswim import board_state as model
from localswim import ticket_order


def _cards(board: model.Board, count: int = 4) -> None:
    for index in range(count):
        board.create(f"card-{index}", f"Card {index}", "backlog", "bot")


def _rows(plan: model.JsonObject) -> list[model.JsonObject]:
    return cast("list[model.JsonObject]", plan["cards"])


def test_order_uses_indirect_full_board_dependencies_and_preserves_data(board: model.Board) -> None:
    _cards(board)
    board.move("card-1", "ready_for_work", "bot")
    board.comment("card-0", "Historical #0003 stays literal", "terry")
    board.link("card-2", "blocks", "card-1", "bot")
    board.link("card-1", "blocks", "card-0", "bot")
    board.set_parent("card-3", "card-0", "bot")
    before = copy.deepcopy(board.to_json())
    plan = board.ticket_order_plan("backlog", ["card-0", "card-2"])
    assert [row["id"] for row in _rows(plan)] == ["card-2", "card-0"]
    assert plan["changedCount"] == 2
    assert board.to_json() == before
    assert board.reorder_tickets("backlog", ["card-2", "card-0"], "bot") == plan
    assert board.next_ticket == 5
    assert [item.ticket for item in board.items] == [3, 2, 1, 4]
    assert [item.storage_ticket for item in board.items] == [1, 2, 3, 4]
    after = board.to_json()
    assert after["schema"] == 6
    restored = model.Board.from_json(after, "ticket test")
    assert restored.to_json() == after
    assert restored.verify() == []
    for old, new in zip(
        cast("list[model.JsonObject]", before["items"]),
        cast("list[model.JsonObject]", after["items"]),
        strict=True,
    ):
        for key in old:
            if key != "ticket":
                assert new[key] == old[key]
    for key in before:
        if key not in {"schema", "items"}:
            assert after[key] == before[key]


def test_stable_ties_parent_links_and_noop_are_not_mutations(board: model.Board) -> None:
    _cards(board)
    board.set_parent("card-0", "card-3", "bot")
    board.link("card-2", "references", "card-0", "bot")
    before = board.to_json()
    result = board.reorder_tickets("backlog", ["card-3", "card-0", "card-2"], "bot")
    assert result["changedCount"] == 0
    assert [row["id"] for row in _rows(result)] == ["card-0", "card-2", "card-3"]
    assert board.to_json() == before
    assert not board.ticket_history


def test_selected_cycles_refused_unselected_cycles_are_transparent(board: model.Board) -> None:
    _cards(board)
    board.link("card-2", "blocks", "card-1", "bot")
    board.link("card-1", "blocks", "card-2", "bot")
    board.link("card-3", "blocks", "card-2", "bot")
    board.link("card-1", "blocks", "card-0", "bot")
    before = board.to_json()
    with pytest.raises(model.BoardError, match="blocking cycle"):
        board.reorder_tickets("backlog", None, "bot")
    assert board.to_json() == before
    plan = board.ticket_order_plan("backlog", ["card-0", "card-3"])
    assert [row["id"] for row in _rows(plan)] == ["card-3", "card-0"]


@pytest.mark.parametrize(
    "refs, message",
    [
        (["card-0", "1"], "duplicate"),
        (["missing"], "no item"),
        (["card-1"], "unarchived"),
        (["card-2"], "unarchived"),
    ],
)
def test_selection_refusals(board: model.Board, refs: list[str], message: str) -> None:
    _cards(board)
    board.move("card-1", "ready_for_work", "bot")
    board.set_archived("card-2", archived=True, by="bot")
    with pytest.raises(model.BoardError, match=message):
        board.ticket_order_plan("backlog", refs)


def test_empty_selection_and_default_active_selection(board: model.Board) -> None:
    _cards(board)
    board.set_archived("card-2", archived=True, by="bot")
    assert board.ticket_order_plan("backlog", [])["selectedCount"] == 0
    assert board.ticket_order_plan("backlog")["selectedCount"] == 3
    with pytest.raises(model.BoardError, match="existing lane"):
        board.ticket_order_plan("missing")
    with pytest.raises(model.BoardError, match="actor"):
        board.reorder_tickets("backlog", None, "unknown")


def test_reverts_keep_origins_and_individual_activity_and_versions(board: model.Board) -> None:
    _cards(board, 2)
    board.link("card-1", "blocks", "card-0", "bot")
    board.reorder_tickets("backlog", None, "terry")
    first_versions = board.ticket_history_by_item()
    board.unlink("card-1", "blocks", "card-0", "bot")
    board.link("card-0", "blocks", "card-1", "bot")
    board.reorder_tickets("backlog", None, "bot")
    assert [item.ticket for item in board.items] == [1, 2]
    assert [item.origin_ticket for item in board.items] == [1, 2]
    assert board.ticket_history_by_item() != first_versions
    assert len(board.ticket_history) == 2
    assert model.Board.from_json(board.to_json(), "revert").verify() == []
    events = [
        event
        for event in model.activity_events(board, dt.datetime.min.replace(tzinfo=dt.UTC), None)
        if event.kind == "ticket_changed"
    ]
    assert len(events) == 4
    assert {event.by for event in events} == {"bot", "terry"}
    assert all(
        "ticketFrom" in event.to_json() and "ticketTo" in event.to_json() for event in events
    )
    assert all(" -> " in event.describe() for event in events)


def _changed_raw(board: model.Board) -> model.JsonObject:
    _cards(board, 3)
    board.link("card-1", "blocks", "card-0", "bot")
    board.reorder_tickets("backlog", None, "bot")
    return board.to_json()


@pytest.mark.parametrize(
    "field, value",
    [
        ("from", True),
        ("to", 0),
        ("id", "missing"),
    ],
)
def test_malformed_change_fields(board: model.Board, field: str, value: model.JsonValue) -> None:
    raw = _changed_raw(board)
    batch = cast("list[model.JsonObject]", raw["ticketHistory"])[0]
    cast("list[model.JsonObject]", batch["changes"])[0][field] = value
    with pytest.raises(model.BoardError, match="ticketHistory"):
        model.Board.from_json(raw, "malformed")


@pytest.mark.parametrize(
    "damage", ["duplicate", "identity", "current", "origin", "orphan", "missing"]
)
def test_audit_tampering_refused(board: model.Board, damage: str) -> None:
    raw = _changed_raw(board)
    items = cast("list[model.JsonObject]", raw["items"])
    changes = cast(
        "list[model.JsonObject]", cast("list[model.JsonObject]", raw["ticketHistory"])[0]["changes"]
    )
    if damage == "duplicate":
        changes.append(copy.deepcopy(changes[0]))
    elif damage == "identity":
        changes[0]["from"] = changes[0]["to"]
    elif damage == "current":
        items[0]["ticket"], items[1]["ticket"] = items[1]["ticket"], items[0]["ticket"]
    elif damage == "origin":
        items[0]["originTicket"] = 3
    elif damage == "orphan":
        items[2]["originTicket"] = 3
    else:
        del items[0]["originTicket"]
    with pytest.raises(model.BoardError):
        model.Board.from_json(raw, "tampered")


@pytest.mark.parametrize(
    "key, value", [("by", "nobody"), ("lane", "missing"), ("at", "2026-11-08")]
)
def test_batch_metadata_is_strict(board: model.Board, key: str, value: str) -> None:
    raw = _changed_raw(board)
    cast("list[model.JsonObject]", raw["ticketHistory"])[0][key] = value
    with pytest.raises(model.BoardError, match="ticketHistory"):
        model.Board.from_json(raw, "metadata")


def test_format_guard_and_strict_origin(board: model.Board) -> None:
    raw = _changed_raw(board)
    raw["schema"] = 4
    with pytest.raises(model.BoardError, match="require schema 6"):
        model.Board.from_json(raw, "old schema")
    raw["schema"] = 6
    cast("list[model.JsonObject]", raw["items"])[0]["originTicket"] = True
    with pytest.raises(model.BoardError, match="originTicket"):
        model.Board.from_json(raw, "bool origin")
    raw = board.to_json()
    raw.pop("ticketHistory")
    with pytest.raises(model.BoardError, match="requires ticket history"):
        model.Board.from_json(raw, "empty schema6")


def test_long_chain_is_iterative_and_deterministic(board: model.Board) -> None:
    _cards(board, 1200)
    board.links = [model.Link(f"card-{i}", "blocks", f"card-{i - 1}") for i in range(1, 1200)]
    plan = board.ticket_order_plan("backlog")
    assert _rows(plan)[0]["id"] == "card-1199"
    assert _rows(plan)[-1]["id"] == "card-0"
    assert plan["changedCount"] == 1200


def test_lane_setting_is_explicit_detached_and_does_not_renumber(board: model.Board) -> None:
    _cards(board)
    old_policy = board.policy
    before_items = copy.deepcopy(board.to_json()["items"])
    assert not board.policy.reorder_lanes
    board.set_ticket_order_enabled("backlog", True, "bot")
    assert board.policy.reorder_lanes == {"backlog"}
    assert not old_policy.reorder_lanes
    assert board.to_json()["items"] == before_items
    assert board.to_json()["schema"] == 4
    with pytest.raises(model.BoardError, match="CLI actor"):
        board.set_ticket_order_enabled("backlog", False, "terry")
    board.set_ticket_order_enabled("backlog", False, "bot")
    assert not board.policy.reorder_lanes


def test_maximum_selection_is_bounded(board: model.Board, monkeypatch: pytest.MonkeyPatch) -> None:
    _cards(board)
    monkeypatch.setattr(ticket_order, "MAX_TICKET_ORDER", 3)
    with pytest.raises(model.BoardError, match="at most 3"):
        board.ticket_order_plan("backlog")


def test_audit_lane_is_historical_and_new_allocations_keep_unique_anchors(
    board: model.Board,
) -> None:
    _changed_raw(board)
    board.move("card-0", "ready_for_work", "bot")
    board.set_archived("card-1", archived=True, by="bot")
    board.create("new", "Later allocation", "backlog", "bot")
    assert board.find("new").ticket == 4
    assert board.find("new").origin_ticket is None
    assert board.ticket_history[0].lane == "backlog"
    assert model.Board.from_json(board.to_json(), "later changes").verify() == []


@pytest.mark.parametrize("bad", [None, 0, 1, "true", [], {}])
def test_lane_opt_in_rejects_non_boolean_values(board: model.Board, bad: model.JsonValue) -> None:
    document = board.policy.to_json()
    cast("list[model.JsonObject]", document["lanes"])[0]["reorderByDependencies"] = bad
    with pytest.raises(model.BoardError, match="reorderByDependencies must be a boolean"):
        model.TransitionPolicy.from_json(document, "invalid flag")


def test_retired_lane_refused_without_invalidating_historical_audit(board: model.Board) -> None:
    _cards(board, 2)
    board.link("card-1", "blocks", "card-0", "bot")
    board.reorder_tickets("backlog", None, "bot")
    for item in board.items:
        board.set_archived(item.id, archived=True, by="bot")
    document = board.policy.to_json()
    document["schema"] = 7
    document["retiredLanes"] = ["backlog"]
    board.policy = model.TransitionPolicy.from_json(document, "retired")
    with pytest.raises(model.BoardError, match="retired"):
        board.ticket_order_plan("backlog")
    assert model.Board.from_json(board.to_json(), "historical retirement").verify() == []


def test_known_owner_cannot_forge_ticket_order_history(board: model.Board) -> None:
    board.users = (*board.users, model.User("observer", "Observer", model.BOT, "#336699"))
    raw = _changed_raw(board)
    cast("list[model.JsonObject]", raw["ticketHistory"])[0]["by"] = "observer"
    with pytest.raises(model.BoardError, match="configured CLI or browser actor"):
        model.Board.from_json(raw, "forged known owner")
    with pytest.raises(model.BoardError, match="configured CLI or browser actor"):
        board.reorder_tickets("backlog", None, "observer")
