"""Ticket ordering remains discoverable and usable through the real gateway."""

import json
from typing import TYPE_CHECKING

import pytest

from localswim import board_state, workspace
from tests.test_api import request_json
from tests.test_workspace import board_route, catalog_boards
from tests.test_workspace import gateway as gateway_fixture

if TYPE_CHECKING:
    import pathlib

    from tests.test_workspace import RunningWorkspace

gateway = gateway_fixture


def test_gateway_preview_apply_keeps_schema6_board_visible(gateway: RunningWorkspace) -> None:
    board = board_state.load(gateway.first)
    board.create("dependent", "Dependent", "backlog", "bot")
    board.create("prerequisite", "Prerequisite", "backlog", "bot")
    board.link("prerequisite", "blocks", "dependent", "bot")
    board.set_ticket_order_enabled("backlog", True, "bot")
    board_state.save(board, gateway.first)
    route = board_route(gateway.base, "alpha/board", "board/ticket-order/")
    code, preview = request_json(
        route + "preview", token=gateway.instance.token, body={"lane": "backlog", "cards": None}
    )
    assert code == 200
    assert preview["plan"]["changedCount"] == 2
    assert preview["applied"] is False
    assert json.loads(gateway.first.read_bytes())["schema"] == 4
    code, applied = request_json(
        route + "apply",
        token=gateway.instance.token,
        revision=preview["revision"],
        body={"lane": "backlog", "cards": None},
    )
    assert code == 200 and applied["applied"] is True
    assert json.loads(gateway.first.read_bytes())["schema"] == 6
    assert workspace._read_candidate(gateway.first) is not None  # noqa: SLF001
    assert "alpha/board" in {entry["key"] for entry in catalog_boards(gateway.instance)}
    code, visible = request_json(board_route(gateway.base, "alpha/board"))
    assert code == 200
    lane = next(lane for lane in visible["lanes"] if lane["state"] == "backlog")
    assert lane["reorderByDependencies"] is True
    root = next(item for item in lane["items"] if item["id"] == "prerequisite")
    assert root["ticketHistory"][0]["ticketFrom"] == 2
    assert root["ticketHistory"][0]["ticketTo"] == 1
    assert {item["id"]: item["ticket"] for item in lane["items"]} == {
        "prerequisite": "#0001",
        "dependent": "#0002",
    }


@pytest.mark.parametrize("schema", [5, 6.0, "6", True, 7])
def test_discovery_keeps_private_and_ambiguous_formats_out(
    tmp_path: pathlib.Path, schema: object
) -> None:
    path = tmp_path / "board.json"
    path.write_text(json.dumps({"schema": schema, "project": "Test", "items": [], "policy": {}}))
    assert workspace._read_candidate(path) is None  # noqa: SLF001
