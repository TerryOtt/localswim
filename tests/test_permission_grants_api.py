"""CLI-only, atomic HTTP permission grant contracts."""

# ruff: noqa: SLF001 -- rollback assertions inspect unpublished store state

import urllib.error
import urllib.request
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

import pytest

from localswim import api_endpoint, board_state
from tests.test_api import API, RunningApi, request_json
from tests.test_api import api as api  # noqa: PLC0414 -- pytest fixture re-export
from tests.test_workspace import RunningWorkspace, board_route
from tests.test_workspace import gateway as gateway  # noqa: PLC0414 -- pytest fixture re-export

if TYPE_CHECKING:
    import pathlib


GRANT: dict[str, object] = {
    "actors": ["bot", "terry"],
    "create": ["in_progress"],
    "moves": [{"from": "backlog", "to": "completed"}],
}


def grant(
    api: RunningApi, body: dict[str, object] | None = None, *, revision: int = 0
) -> tuple[int, dict[str, Any]]:
    return request_json(
        api.base + API + "/board/permissions",
        token=api_endpoint.CLI_TOKEN,
        revision=revision,
        body=GRANT if body is None else body,
    )


@pytest.mark.parametrize("credential", [None, "invalid", "browser"])
def test_permission_grants_require_cli_credential(api: RunningApi, credential: str | None) -> None:
    before = api.path.read_bytes()
    code, _ = request_json(
        api.base + API + "/board/permissions",
        token=api_endpoint.BROWSER_TOKEN if credential == "browser" else credential,
        revision=0,
        body=GRANT,
    )
    assert code == (403 if credential == "browser" else 401)
    assert api.path.read_bytes() == before


def test_permission_grants_deny_gateway_browser(gateway: RunningWorkspace) -> None:
    before = gateway.first.read_bytes()
    _, session = request_json(gateway.base + API + "/browser-session")
    code, _ = request_json(
        board_route(gateway.base, "alpha/board", "board/permissions"),
        token=str(session["token"]),
        revision=0,
        body=GRANT,
    )
    assert code == 403
    assert gateway.first.read_bytes() == before


def test_permission_grants_require_current_revision(api: RunningApi) -> None:
    before = api.path.read_bytes()
    code, _ = request_json(
        api.base + API + "/board/permissions", token=api_endpoint.CLI_TOKEN, body=GRANT
    )
    assert code == 428
    assert grant(api, revision=99)[0] == 412
    assert api.path.read_bytes() == before
    assert api_endpoint.STORE is not None
    assert not api_endpoint.STORE.policy.may_create("bot", "in_progress")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {**GRANT, "by": "terry"},
        {"actors": ["bot"], "create": []},
        {**GRANT, "actors": "bot"},
        {**GRANT, "actors": [True]},
        {**GRANT, "actors": [None]},
        {**GRANT, "actors": []},
        {**GRANT, "actors": ["unknown"]},
        {**GRANT, "actors": ["Bot"]},
        {**GRANT, "create": "in_progress"},
        {**GRANT, "create": [1]},
        {**GRANT, "create": ["In progress"]},
        {**GRANT, "create": ["in_progress", "missing"]},
        {**GRANT, "moves": {}},
        {**GRANT, "moves": [["backlog", "completed"]]},
        {**GRANT, "moves": [{"from": "backlog"}]},
        {**GRANT, "moves": [{"from": "backlog", "to": "completed", "by": "bot"}]},
        {**GRANT, "moves": [{"from": 1, "to": "completed"}]},
        {**GRANT, "moves": [{"from": "backlog", "to": False}]},
        {**GRANT, "moves": [{"from": "backlog", "to": "missing"}]},
        {**GRANT, "moves": [{"from": "backlog", "to": "backlog"}]},
        {"actors": ["bot"], "create": [], "moves": []},
    ],
)
def test_invalid_permission_grants_roll_back(api: RunningApi, body: dict[str, object]) -> None:
    before = api.path.read_bytes()
    store = api_endpoint.STORE
    assert store is not None
    snapshot = store._board.to_json()
    policy = store.policy
    assert grant(api, body)[0] == 409
    assert api.path.read_bytes() == before
    assert store._board.to_json() == snapshot
    assert store.policy is policy


@pytest.mark.parametrize(
    "raw",
    [
        b'{"actors":["bot"],"actors":["terry"],"create":["in_progress"],"moves":[]}',
        b'{"actors":["bot"],"create":[],"moves":[{"from":"backlog","from":"ready_for_work","to":"completed"}]}',
        b"[]",
        b"null",
        b"{",
    ],
)
def test_permission_grants_reject_ambiguous_json(api: RunningApi, raw: bytes) -> None:
    before = api.path.read_bytes()
    request = urllib.request.Request(
        api.base + API + "/board/permissions",
        data=raw,
        headers={"Authorization": "Bearer " + api_endpoint.CLI_TOKEN, "If-Match": '"revision-0"'},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=5)
    with caught.value:
        assert caught.value.code == 400
    assert api.path.read_bytes() == before


def test_permission_grants_persist_and_publish_without_card_changes(api: RunningApi) -> None:
    code, _ = request_json(
        api.base + API + "/cards",
        token=api_endpoint.CLI_TOKEN,
        revision=0,
        body={"id": "existing", "subject": "Existing", "state": "backlog", "detail": "Keep"},
    )
    assert code == 200
    before = deepcopy(board_state.load(api.path).to_json())
    expected_policy = deepcopy(cast("board_state.JsonObject", before["policy"]))
    lanes = cast("list[board_state.JsonObject]", expected_policy["lanes"])
    target = next(lane for lane in lanes if lane["id"] == "in_progress")
    assert target["create"] == []
    target["create"] = ["bot", "terry"]
    edges = cast("dict[str, list[board_state.JsonValue]]", expected_policy["edges"])
    edges["bot"].append({"from": "backlog", "to": "completed"})
    edges["terry"].append({"from": "backlog", "to": "completed"})
    code, response = grant(api, revision=1)
    assert code == 200, response
    assert response["revision"] == 2
    store = api_endpoint.STORE
    assert store is not None
    assert store.policy.to_json() == expected_policy
    assert store.policy.check_edges() == []
    persisted = board_state.load(api.path)
    after = persisted.to_json()
    assert {key: value for key, value in after.items() if key not in {"policy", "revision"}} == {
        key: value for key, value in before.items() if key not in {"policy", "revision"}
    }
    assert persisted.policy.to_json() == expected_policy
    assert persisted.verify() == []
    # Immediate subsequent commands use the committed grants for both credential roles.
    code, response = request_json(
        api.base + API + "/cards/existing/move",
        token=api_endpoint.CLI_TOKEN,
        revision=2,
        body={"to": "completed"},
    )
    assert code == 200, response
    code, response = request_json(
        api.base + API + "/cards",
        token=api_endpoint.BROWSER_TOKEN,
        revision=3,
        body={"id": "new", "subject": "New", "state": "in_progress"},
    )
    assert code == 200, response
    assert board_state.load(api.path).find("new").history[0].by == "terry"


def test_permission_grants_save_failure_never_publishes(
    api: RunningApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = api.path.read_bytes()
    store = api_endpoint.STORE
    assert store is not None
    snapshot = store._board.to_json()
    policy = store.policy

    def fail_save(_board: board_state.Board, _path: pathlib.Path) -> None:
        raise OSError("injected save failure")

    monkeypatch.setattr(board_state, "save", fail_save)
    code, response = grant(api)
    assert code == 409
    assert "injected save failure" in response["error"]
    assert api.path.read_bytes() == before
    assert store._board.to_json() == snapshot
    assert store.policy is policy


def test_permission_grants_refuse_external_policy_store(api: RunningApi) -> None:
    before = api.path.read_bytes()
    store = api_endpoint.BoardStore(api.path, policy=board_state.TransitionPolicy.load())
    api_endpoint.STORE = store
    code, response = grant(api)
    assert code == 409
    assert "embedded" in response["error"]
    assert api.path.read_bytes() == before
    assert not store.policy.may_create("bot", "in_progress")


def test_permission_grants_obey_shutdown_gate(api: RunningApi) -> None:
    before = api.path.read_bytes()
    api_endpoint._shutdown_requested.set()
    assert grant(api)[0] == 503
    assert api.path.read_bytes() == before
