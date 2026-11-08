"""Authenticated, revision-bound ticket permutations through the sole writer."""

import copy
import json
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import pytest

from localswim import api_endpoint, board_state
from tests.test_api import API, RunningApi, assert_http_error, request_json
from tests.test_api import api as api_fixture

if TYPE_CHECKING:
    import pathlib

api = api_fixture


def seed(api: RunningApi, board: board_state.Board) -> None:
    """Install a synthetic graph before testing the real HTTP boundary."""
    for name in ("leaf", "bridge", "root", "untouched"):
        board.create(name, name.title(), "backlog", "bot")
    board.move("bridge", "ready_for_work", "bot")
    board.link("root", "blocks", "bridge", "bot")
    board.link("bridge", "blocks", "leaf", "bot")
    board.comment("leaf", "Literal historical reference #0003", "terry")
    board_state.save(board, api.path)
    api_endpoint.STORE = api_endpoint.BoardStore(api.path)


def order(
    api: RunningApi,
    action: str,
    *,
    cards: list[str] | None = None,
    revision: int | None = None,
    browser: bool = False,
) -> tuple[int, dict[str, Any]]:
    return request_json(
        api.base + API + "/board/ticket-order/" + action,
        token=api_endpoint.BROWSER_TOKEN if browser else api_endpoint.CLI_TOKEN,
        revision=revision,
        body={"lane": "backlog", "cards": cards},
    )


def test_preview_and_apply_indirect_subset_preserve_every_unrelated_field(
    api: RunningApi, board: board_state.Board
) -> None:
    seed(api, board)
    before_bytes = api.path.read_bytes()
    before = board_state.load(api.path)
    code, preview = order(api, "preview", cards=["#0001", "root"])
    assert code == 200
    assert set(preview) == {"result", "revision", "plan", "applied"}
    assert isinstance(preview["result"], str)
    assert preview["revision"] == 0
    assert preview["applied"] is False
    assert preview["plan"] == {
        "lane": "backlog",
        "selectedCount": 2,
        "changedCount": 2,
        "cards": [
            {"id": "root", "subject": "Root", "oldTicket": 3, "newTicket": 1},
            {"id": "leaf", "subject": "Leaf", "oldTicket": 1, "newTicket": 3},
        ],
    }
    assert api.path.read_bytes() == before_bytes
    code, applied = order(api, "apply", cards=["root", "leaf"], revision=0)
    assert code == 200
    assert set(applied) == set(preview)
    assert applied["plan"] == preview["plan"]
    assert applied["revision"] == 1 and applied["applied"] is True
    after = board_state.load(api.path)
    assert [item.ticket for item in after.items] == [3, 2, 1, 4]
    assert after.next_ticket == before.next_ticket
    for old, new in zip(before.items, after.items, strict=True):
        old_fields, new_fields = old.to_json(), new.to_json()
        for field in old_fields:
            if field != "ticket":
                assert new_fields[field] == old_fields[field]
    assert after.links == before.links
    assert after.relationship_history == before.relationship_history
    assert after.policy.to_json() == before.policy.to_json()
    assert len(after.ticket_history) == 1
    assert after.ticket_history[0].by == "bot"
    assert after.verify() == []


def test_full_lane_includes_visible_cards_only(api: RunningApi, board: board_state.Board) -> None:
    seed(api, board)
    board.set_archived("untouched", archived=True, by="bot")
    board_state.save(board, api.path)
    api_endpoint.STORE = api_endpoint.BoardStore(api.path)
    code, response = order(api, "preview")
    assert code == 200
    assert response["plan"]["selectedCount"] == 2
    assert [row["id"] for row in response["plan"]["cards"]] == ["root", "leaf"]


@pytest.mark.parametrize("cards", [[], ["leaf"], None])
def test_noop_apply_never_saves_or_publishes(
    api: RunningApi,
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
    cards: list[str] | None,
) -> None:
    seed(api, board)
    board.unlink("root", "blocks", "bridge", "bot")
    board_state.save(board, api.path)
    api_endpoint.STORE = api_endpoint.BoardStore(api.path)
    before = api.path.read_bytes()

    def forbidden_save(*_args: object, **_kwargs: object) -> None:
        pytest.fail("no-op attempted a snapshot save")

    monkeypatch.setattr(board_state, "save", forbidden_save)
    code, response = order(api, "apply", cards=cards, revision=0)
    assert code == 200
    assert response["revision"] == 0 and response["applied"] is False
    assert response["plan"]["changedCount"] == 0
    assert api.path.read_bytes() == before
    assert not board_state.load(api.path).ticket_history


@pytest.mark.parametrize("action", ["preview", "apply"])
@pytest.mark.parametrize("token", [None, "invalid"])
def test_all_order_routes_require_authentication(
    api: RunningApi, action: str, token: str | None
) -> None:
    before = api.path.read_bytes()
    code, _ = request_json(
        api.base + API + "/board/ticket-order/" + action,
        token=token,
        revision=0,
        body={"lane": "backlog", "cards": None},
    )
    assert code == 401
    assert api.path.read_bytes() == before


@pytest.mark.parametrize("action", ["preview", "apply"])
def test_browser_flag_is_required_for_both_order_routes(
    api: RunningApi, board: board_state.Board, action: str
) -> None:
    seed(api, board)
    before = api.path.read_bytes()
    assert order(api, action, browser=True, revision=0)[0] == 409
    assert api.path.read_bytes() == before
    code, configured = request_json(
        api.base + API + "/board/ticket-order/config",
        token=api_endpoint.CLI_TOKEN,
        revision=0,
        body={"lane": "backlog", "enabled": True},
    )
    assert code == 200 and configured["revision"] == 1
    code, response = order(api, action, browser=True, revision=1)
    assert code == 200
    assert response["applied"] is (action == "apply")
    if action == "apply":
        assert board_state.load(api.path).ticket_history[0].by == "terry"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"lane": "backlog"},
        {"lane": "backlog", "cards": None, "actor": "terry"},
        {"lane": True, "cards": None},
        {"lane": "backlog", "cards": "leaf"},
        {"lane": "backlog", "cards": [True]},
        {"lane": "backlog", "cards": [""]},
        {"lane": "backlog", "cards": ["leaf", "#0001"]},
        {"lane": "backlog", "cards": ["leaf", "bridge"]},
        {"lane": "backlog", "cards": ["leaf", "missing"]},
        {"lane": "absent", "cards": None},
    ],
)
@pytest.mark.parametrize("action", ["preview", "apply"])
def test_invalid_selection_is_atomic(
    api: RunningApi, board: board_state.Board, body: dict[str, object], action: str
) -> None:
    seed(api, board)
    before = api.path.read_bytes()
    code, _ = request_json(
        api.base + API + "/board/ticket-order/" + action,
        token=api_endpoint.CLI_TOKEN,
        revision=0,
        body=body,
    )
    assert code in {400, 409}
    assert api.path.read_bytes() == before


@pytest.mark.parametrize("action", ["preview", "apply", "config"])
def test_duplicate_json_fields_are_rejected(api: RunningApi, action: str) -> None:
    before = api.path.read_bytes()
    body = (
        b'{"lane":"backlog","lane":"backlog","enabled":true}'
        if action == "config"
        else b'{"lane":"backlog","lane":"backlog","cards":null}'
    )
    request = urllib.request.Request(
        api.base + API + "/board/ticket-order/" + action,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_endpoint.CLI_TOKEN}",
            "Content-Type": "application/json",
            "If-Match": '"revision-0"',
        },
    )
    assert_http_error(request, 400)
    assert api.path.read_bytes() == before


@pytest.mark.parametrize("action", ["preview", "apply"])
def test_selected_dependency_cycle_refuses_entire_operation(
    api: RunningApi, board: board_state.Board, action: str
) -> None:
    seed(api, board)
    board.link("leaf", "blocks", "root", "bot")
    board_state.save(board, api.path)
    api_endpoint.STORE = api_endpoint.BoardStore(api.path)
    before = api.path.read_bytes()
    code, response = order(api, action, revision=0)
    assert code == 409 and "cycle" in response["error"]
    assert api.path.read_bytes() == before


def test_apply_requires_revision_and_preview_validates_optional_revision(
    api: RunningApi, board: board_state.Board
) -> None:
    seed(api, board)
    before = api.path.read_bytes()
    assert order(api, "apply")[0] == 428
    assert order(api, "preview", revision=99)[0] == 412
    request = urllib.request.Request(
        api.base + API + "/board/ticket-order/preview",
        data=json.dumps({"lane": "backlog", "cards": None}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {api_endpoint.CLI_TOKEN}", "If-Match": "garbage"},
    )
    assert_http_error(request, 428)
    assert api.path.read_bytes() == before


def test_concurrent_apply_has_one_winner(api: RunningApi, board: board_state.Board) -> None:
    seed(api, board)
    barrier = threading.Barrier(2)

    def apply(_index: int) -> tuple[int, dict[str, Any]]:
        barrier.wait(timeout=5)
        return order(api, "apply", revision=0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, range(2)))
    assert sorted(code for code, _ in results) == [200, 412]
    assert board_state.load(api.path).revision == 1
    assert len(board_state.load(api.path).ticket_history) == 1


def test_stale_preview_cannot_overwrite_a_later_graph_edit(
    api: RunningApi, board: board_state.Board
) -> None:
    seed(api, board)
    assert order(api, "preview")[0] == 200
    code, _ = request_json(
        api.base + API + "/cards/root/link",
        token=api_endpoint.CLI_TOKEN,
        revision=0,
        body={"kind": "blocks", "other": "bridge", "remove": True},
    )
    assert code == 200
    before = api.path.read_bytes()
    assert order(api, "apply", revision=0)[0] == 412
    assert api.path.read_bytes() == before
    code, response = order(api, "apply", revision=1)
    assert code == 200 and response["applied"] is False
    assert api.path.read_bytes() == before


@pytest.mark.parametrize("failure", ["audit", "save"])
def test_failed_candidate_never_changes_accepted_memory_or_disk(
    api: RunningApi,
    board: board_state.Board,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    seed(api, board)
    store = api_endpoint.STORE
    assert store is not None
    before = api.path.read_bytes()
    accepted = copy.deepcopy(store.snapshot().to_json())
    if failure == "audit":
        original = board_state.Board.reorder_tickets

        def fail_audit(
            candidate: board_state.Board, lane: str, refs: list[str] | None, by: str
        ) -> board_state.JsonObject:
            original(candidate, lane, refs, by)
            raise board_state.BoardError("injected audit failure")

        monkeypatch.setattr(board_state.Board, "reorder_tickets", fail_audit)
        error = board_state.BoardError
    else:

        def fail_save(_board: board_state.Board, _path: pathlib.Path, **_kwargs: object) -> None:
            raise OSError("injected save failure")

        monkeypatch.setattr(board_state, "save", fail_save)
        error = OSError
    with pytest.raises(error, match="injected"):
        store.ticket_order("backlog", None, "bot", apply=True, expected_revision=0)
    assert api.path.read_bytes() == before
    monkeypatch.undo()
    assert store.snapshot().to_json() == accepted


def test_configuration_is_cli_only_and_revision_checked(api: RunningApi) -> None:
    route = api.base + API + "/board/ticket-order/config"
    before = api.path.read_bytes()
    body: dict[str, object] = {"lane": "backlog", "enabled": True}
    for token, status in ((None, 401), ("invalid", 401), (api_endpoint.BROWSER_TOKEN, 403)):
        assert request_json(route, token=token, revision=0, body=body)[0] == status
    assert request_json(route, token=api_endpoint.CLI_TOKEN, body=body)[0] == 428
    assert api.path.read_bytes() == before
    code, response = request_json(route, token=api_endpoint.CLI_TOKEN, revision=0, body=body)
    assert code == 200
    assert set(response) == {"result", "revision", "enabled"}
    assert response["revision"] == 1
    assert response["enabled"] is True
    assert "backlog" in board_state.load(api.path).policy.reorder_lanes
    before = api.path.read_bytes()
    assert request_json(route, token=api_endpoint.CLI_TOKEN, revision=0, body=body)[0] == 412
    assert api.path.read_bytes() == before
    code, _ = request_json(
        route,
        token=api_endpoint.CLI_TOKEN,
        revision=1,
        body={"lane": "backlog", "enabled": False},
    )
    assert code == 200
    assert "backlog" not in board_state.load(api.path).policy.reorder_lanes


@pytest.mark.parametrize(
    "body",
    [
        {"lane": "backlog", "enabled": "true"},
        {"lane": "backlog", "enabled": 1},
        {"lane": "backlog", "enabled": None},
        {"lane": "backlog", "enabled": True, "actor": "bot"},
        {"lane": "missing", "enabled": True},
    ],
)
def test_invalid_configuration_preserves_snapshot(api: RunningApi, body: dict[str, object]) -> None:
    before = api.path.read_bytes()
    code, _ = request_json(
        api.base + API + "/board/ticket-order/config",
        token=api_endpoint.CLI_TOKEN,
        revision=0,
        body=body,
    )
    assert code == 409
    assert api.path.read_bytes() == before
