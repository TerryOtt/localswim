"""Batch priority mutations retain the workspace's sole-writer proxy boundary."""

import json
from typing import TYPE_CHECKING

import pytest

from localswim import board_state
from tests.test_api import request_json
from tests.test_workspace import (
    RunningWorkspace,
    board_route,
    existing_writer,
)
from tests.test_workspace import gateway as _gateway_fixture

gateway = _gateway_fixture

if TYPE_CHECKING:
    import pathlib

API = board_state.API_PREFIX


def seed_cards(path: pathlib.Path) -> None:
    """Prepare scratch cards before any service acquires their writer lease."""
    board = board_state.load(path)
    board.create("first", "First", "backlog", "terry", priority="P1")
    board.create("second", "Second", "backlog", "terry", priority="P1")
    board_state.save(board, path)


@pytest.mark.parametrize("credential", ["missing", "invalid", "upstream_cli", "upstream_browser"])
def test_batch_route_requires_workspace_credential(
    gateway: RunningWorkspace, credential: str
) -> None:
    seed_cards(gateway.first)
    with existing_writer(gateway.first) as descriptor:
        upstream = f"http://127.0.0.1:{descriptor['port']}"
        _, session = request_json(upstream + API + "/browser-session")
        tokens = {
            "missing": None,
            "invalid": "invalid-workspace-credential",
            "upstream_cli": str(descriptor["token"]),
            "upstream_browser": str(session["token"]),
        }
        before = gateway.first.read_bytes()
        status, payload = request_json(
            board_route(gateway.base, "alpha/board", "cards/priorities"),
            token=tokens[credential],
            revision=0,
            body={"cards": ["first", "second"], "priority": "P3"},
        )
        assert status == 401, payload
        assert gateway.first.read_bytes() == before
        assert board_state.load(gateway.second).revision == 0


def test_batch_proxy_reuses_writer_and_forwards_revision_and_browser_actor(
    gateway: RunningWorkspace,
) -> None:
    seed_cards(gateway.first)
    seed_cards(gateway.second)
    other_before = gateway.second.read_bytes()
    with existing_writer(gateway.first) as descriptor:
        route = board_route(gateway.base, "alpha/board", "cards/priorities")
        body: dict[str, object] = {"cards": ["#0002", "first"], "priority": "P3"}
        status, payload = request_json(route, token=gateway.instance.token, body=body)
        assert status == 428, payload
        assert board_state.load(gateway.first).revision == 0
        status, payload = request_json(route, token=gateway.instance.token, revision=0, body=body)
        assert status == 200, payload
        assert payload["revision"] == 1
        assert payload["priorities"] == {"first": "P3", "second": "P3"}
        assert payload["changes"] == [
            {"id": "first", "from": "P1", "to": "P3"},
            {"id": "second", "from": "P1", "to": "P3"},
        ]
        board = board_state.load(gateway.first)
        for card in board.items:
            assert card.history[-1].by == "terry"
            assert card.history[-1].priority_frm == "P1"
            assert card.history[-1].priority_to == "P3"
        committed = gateway.first.read_bytes()
        status, payload = request_json(
            route,
            token=gateway.instance.token,
            revision=0,
            body={"cards": ["first", "second"], "priority": "P2"},
        )
        assert status == 412, payload
        assert gateway.first.read_bytes() == committed
        current = json.loads(
            board_state.service_descriptor_path(gateway.first).read_text(encoding="utf-8")
        )
        assert current == descriptor
        assert gateway.second.read_bytes() == other_before
        assert not board_state.service_descriptor_path(gateway.second).exists()


def test_batch_proxy_lowers_cycle_together_without_touching_completed(
    gateway: RunningWorkspace,
) -> None:
    seed_cards(gateway.first)
    board = board_state.load(gateway.first)
    board.link("first", "blocks", "second", "terry")
    board.link("second", "blocks", "first", "terry")
    board.create("finished", "Finished", "backlog", "terry", priority="P1")
    board.move("finished", "in_progress", "terry")
    board.move("finished", "ready_for_review", "terry")
    board.move("finished", "completed", "terry")
    board.link("second", "blocks", "finished", "terry")
    board_state.save(board, gateway.first)
    finished_before = board.find("finished").to_json()
    status, payload = request_json(
        board_route(gateway.base, "alpha/board", "cards/priorities"),
        token=gateway.instance.token,
        revision=0,
        body={"cards": ["second", "first"], "priority": "P3"},
    )
    assert status == 200, payload
    assert payload["revision"] == 1
    assert payload["priorities"] == {"first": "P3", "second": "P3"}
    assert payload["changes"] == [
        {"id": "first", "from": "P1", "to": "P3"},
        {"id": "second", "from": "P1", "to": "P3"},
    ]
    after = board_state.load(gateway.first)
    assert after.find("finished").to_json() == finished_before
    assert after.links == board.links
    assert board_state.load(gateway.second).revision == 0


def test_batch_proxy_reports_effective_floor_and_unselected_cascade(
    gateway: RunningWorkspace,
) -> None:
    board = board_state.load(gateway.first)
    for card in ("floor", "selected", "downstream"):
        board.create(card, card, "backlog", "terry", priority="P3")
    board.set_priority("floor", "P2", "terry")
    board.link("floor", "blocks", "selected", "terry")
    board.link("selected", "blocks", "downstream", "terry")
    board_state.save(board, gateway.first)
    route = board_route(gateway.base, "alpha/board", "cards/priorities")
    status, payload = request_json(
        route,
        token=gateway.instance.token,
        revision=0,
        body={"cards": ["selected"], "priority": "P3"},
    )
    assert status == 200, payload
    assert payload["revision"] == 1
    assert payload["priorities"] == {"selected": "P2"}
    assert payload["changes"] == []
    status, payload = request_json(
        route,
        token=gateway.instance.token,
        revision=1,
        body={"cards": ["selected"], "priority": "P1"},
    )
    assert status == 200, payload
    assert payload["revision"] == 2
    assert payload["priorities"] == {"selected": "P1"}
    assert payload["changes"] == [
        {"id": "selected", "from": "P2", "to": "P1"},
        {"id": "downstream", "from": "P2", "to": "P1"},
    ]
    after = board_state.load(gateway.first)
    assert after.find("floor").priority == "P2"
    assert after.find("selected").history[-1].by == "terry"
    assert after.find("downstream").history[-1].by == "terry"
    assert board_state.load(gateway.second).revision == 0
