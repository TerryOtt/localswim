"""Offline lane maintenance preserves schema-6 ticket identities and replay."""

import copy
import json
import socket
from typing import TYPE_CHECKING, cast

import pytest

from localswim import board_state as model

if TYPE_CHECKING:
    import pathlib


@pytest.fixture
def ordered_path(board: model.Board, tmp_path: pathlib.Path) -> pathlib.Path:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        board.port = sock.getsockname()[1]
    board.create("dependent", "Dependent", "backlog", "bot")
    board.create("prerequisite", "Prerequisite", "backlog", "bot")
    board.link("prerequisite", "blocks", "dependent", "bot")
    board.set_ticket_order_enabled("backlog", True, "bot")
    board.reorder_tickets("backlog", None, "bot")
    for item in board.items:
        board.set_archived(item.id, archived=True, by="bot")
    path = tmp_path / "board.json"
    model.save(board, path)
    return path


def _read(path: pathlib.Path) -> model.Board:
    raw = cast("model.JsonValue", json.loads(path.read_text(encoding="utf-8")))
    board = model.Board.from_json(raw, str(path))
    assert board.verify() == []
    return board


def test_schema6_lane_administration_preserves_ticket_audit(ordered_path: pathlib.Path) -> None:
    before = _read(ordered_path)
    before_items = copy.deepcopy(before.to_json()["items"])
    history = copy.deepcopy(before.to_json()["ticketHistory"])
    model.add_lane(ordered_path, "Incoming", create_for=("bot",), connect_for=("bot",))
    added = _read(ordered_path)
    assert added.to_json()["items"] == before_items
    assert added.to_json()["ticketHistory"] == history
    assert added.to_json()["schema"] == 6
    model.rename_lane_label(ordered_path, "backlog", "Pool")
    renamed = _read(ordered_path)
    assert renamed.to_json()["items"] == before_items
    assert renamed.to_json()["ticketHistory"] == history
    model.retire_lane(ordered_path, "backlog")
    retired = _read(ordered_path)
    assert retired.to_json()["items"] == before_items
    assert retired.to_json()["ticketHistory"] == history
    assert "backlog" in retired.policy.retired_lanes
    model.migrate_lane_id(ordered_path, "backlog", "old_backlog")
    migrated = _read(ordered_path)
    assert "old_backlog" in migrated.policy.retired_lanes
    assert migrated.policy.reorder_lanes == {"old_backlog"}
    assert migrated.ticket_history[0].lane == "old_backlog"
    assert migrated.ticket_history[0].changes == before.ticket_history[0].changes
    assert [item.storage_ticket for item in migrated.items] == [1, 2]
    assert [item.ticket for item in migrated.items] == [2, 1]
    assert migrated.next_ticket == before.next_ticket


@pytest.mark.parametrize("operation", ["add", "retire", "rename", "migrate"])
def test_schema5_stays_private_and_unmodified(ordered_path: pathlib.Path, operation: str) -> None:
    raw = json.loads(ordered_path.read_text(encoding="utf-8"))
    raw["schema"] = 5
    ordered_path.write_text(json.dumps(raw), encoding="utf-8")
    before = ordered_path.read_bytes()
    with pytest.raises(model.BoardError, match="schema 5"):
        _maintain(ordered_path, operation)
    assert ordered_path.read_bytes() == before


def _maintain(path: pathlib.Path, operation: str) -> None:
    if operation == "add":
        model.add_lane(path, "Incoming")
    elif operation == "retire":
        model.retire_lane(path, "backlog")
    elif operation == "rename":
        model.rename_lane_label(path, "backlog", "Pool")
    else:
        model.migrate_lane_id(path, "backlog", "old_backlog")
