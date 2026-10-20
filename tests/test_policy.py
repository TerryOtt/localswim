"""TransitionPolicy ownership, isolation, and live reload behavior."""

import copy
import json
import os
from typing import TYPE_CHECKING, Any, cast

import pytest

from localswim import api_endpoint, board_state
from tests.support import USERS

if TYPE_CHECKING:
    import pathlib


def policy_file(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    """Copy the production policy so a test may edit it independently."""
    path = tmp_path / name
    path.write_text(board_state.RULES_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def policy_doc(path: pathlib.Path) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


def write_policy(path: pathlib.Path, doc: dict[str, Any], *, advance: bool = False) -> None:
    old_stamp = path.stat().st_mtime_ns
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    if advance:
        os.utime(path, ns=(old_stamp + 1_000_000_000, old_stamp + 1_000_000_000))


def remove_claude_backlog_promotion(path: pathlib.Path) -> None:
    doc = policy_doc(path)
    doc["edges"]["bot"] = [
        edge
        for edge in doc["edges"]["bot"]
        if not (edge["from"] == "backlog" and edge["to"] == "ready_for_work")
    ]
    write_policy(path, doc, advance=True)


def make_store(
    tmp_path: pathlib.Path,
    name: str,
    policy: board_state.TransitionPolicy,
) -> api_endpoint.BoardStore:
    path = tmp_path / f"{name}.json"
    board = board_state.Board(
        project=name, users=USERS, browser_user="terry", cli_user="bot", default_owner="bot"
    )
    board_state.save(board, path)
    return api_endpoint.BoardStore(path, policy)


def create_alpha(board: board_state.Board) -> str:
    return board.create("alpha", "Alpha", "backlog", "bot")


def test_transition_policy_cannot_be_mutated(tmp_path: pathlib.Path) -> None:
    policy = board_state.TransitionPolicy.load(policy_file(tmp_path, "rules.json"))

    with pytest.raises(TypeError):
        cast("Any", policy.table["backlog"].outbound)["completed"] = frozenset({"bot"})


def policy_grants(policy: board_state.TransitionPolicy) -> dict[tuple[str, ...], bool]:
    """Enumerate allowed and denied creation/move cases, including unknown IDs."""
    actors = (*sorted(policy.rules.edge_actors), "unknown_actor")
    states = (*policy.states, "unknown_lane")
    creates = {
        ("create", actor, state): policy.may_create(actor, state)
        for actor in actors
        for state in states
    }
    moves = {
        ("move", actor, source, destination): policy.may_move(actor, source, destination)
        for actor in actors
        for source in states
        for destination in states
    }
    return creates | moves


@pytest.mark.parametrize("exposure", ["input", "document", "to_json"])
def test_nested_policy_documents_are_detached(tmp_path: pathlib.Path, exposure: str) -> None:
    document = policy_doc(policy_file(tmp_path, "rules.json"))
    document["lanes"][0]["note"] = "Lane metadata must survive"
    document["edges"]["terry"][0]["description"] = "Edge metadata must survive"
    document["metadata"] = {"nested": [{"names": ["original"]}]}
    original = copy.deepcopy(document)
    policy = board_state.TransitionPolicy.from_json(document, "embedded test")
    grants = policy_grants(policy)
    if exposure == "document":
        exposed = cast("dict[str, Any]", policy.rules.document)
    elif exposure == "to_json":
        exposed = cast("dict[str, Any]", policy.to_json())
    else:
        exposed = document

    exposed["lanes"][0]["create"].clear()
    exposed["lanes"][0]["note"] = "Changed lane metadata"
    exposed["edges"]["terry"][0]["description"] = "Changed edge metadata"
    exposed["edges"]["bot"].append({"from": "backlog", "to": "completed"})
    exposed["metadata"]["nested"][0]["names"].append("changed")
    exposed["priorities"][0]["label"] = "Changed priority label"

    assert policy.to_json() == original
    assert policy.rules.document == original
    assert policy_grants(policy) == grants
    reconstructed = board_state.TransitionPolicy.from_json(policy.to_json(), "round trip")
    assert reconstructed.to_json() == original
    assert policy_grants(reconstructed) == grants
    assert reconstructed.lanes == policy.lanes
    assert reconstructed.priorities == policy.priorities
    assert reconstructed.priority_label == policy.priority_label
    assert reconstructed.default_priority == policy.default_priority
    # A real grant mutation must change the oracle when explicitly reconstructed.
    changed = board_state.TransitionPolicy.from_json(exposed, "changed detached document")
    assert policy_grants(changed) != grants
    assert changed.may_move("bot", "backlog", "completed")
    assert not policy.may_move("bot", "backlog", "completed")


def test_deepcopy_policy_sharing_keeps_documents_isolated(tmp_path: pathlib.Path) -> None:
    policy = board_state.TransitionPolicy.load(policy_file(tmp_path, "rules.json"))
    original = policy.to_json()

    shared = copy.deepcopy(policy)
    assert shared is policy
    exposed = cast("dict[str, Any]", shared.rules.document)
    exposed["edges"]["bot"].clear()
    exposed["lanes"].clear()

    assert shared.to_json() == original
    assert policy.to_json() == original
    reconstructed = board_state.TransitionPolicy.from_json(shared.to_json(), "shared policy")
    assert policy_grants(reconstructed) == policy_grants(policy)


def test_edge_description_is_optional(tmp_path: pathlib.Path) -> None:
    path = policy_file(tmp_path, "rules.json")
    doc = policy_doc(path)
    doc["edges"]["terry"][0].pop("description", None)
    write_policy(path, doc)

    policy = board_state.TransitionPolicy.load(path)

    assert policy.may_move("terry", "backlog", "ready_for_work")


def test_edges_require_exactly_two_actors(tmp_path: pathlib.Path) -> None:
    path = policy_file(tmp_path, "rules.json")
    doc = policy_doc(path)
    del doc["edges"]["bot"]
    write_policy(path, doc)

    with pytest.raises(board_state.BoardError, match="exactly 2"):
        board_state.TransitionPolicy.load(path)


def test_duplicate_actor_keys_are_rejected_before_json_can_collapse_them(
    tmp_path: pathlib.Path,
) -> None:
    path = policy_file(tmp_path, "rules.json")
    text = path.read_text(encoding="utf-8")
    text = text.replace('"edges": {', '"edges": {\n    "terry": [],', 1)
    path.write_text(text, encoding="utf-8")

    with pytest.raises(board_state.BoardError, match="duplicate JSON object key 'terry'"):
        board_state.TransitionPolicy.load(path)


def test_three_differently_cased_actor_keys_are_rejected(tmp_path: pathlib.Path) -> None:
    path = policy_file(tmp_path, "rules.json")
    doc = policy_doc(path)
    doc["edges"] = {"terry": [], "Bot": [], "Terry": []}
    write_policy(path, doc)

    with pytest.raises(board_state.BoardError, match="exactly 2 distinct actors"):
        board_state.TransitionPolicy.load(path)


def test_actor_ids_match_board_config_case_sensitively(tmp_path: pathlib.Path) -> None:
    path = policy_file(tmp_path, "rules.json")
    doc = policy_doc(path)
    doc["edges"] = {
        "Terry": doc["edges"]["terry"],
        "Bot": doc["edges"]["bot"],
    }
    write_policy(path, doc)
    policy = board_state.TransitionPolicy.load(path)

    with pytest.raises(board_state.BoardError, match=r"actor.*Bot, Terry"):
        make_store(tmp_path, "case-mismatch", policy)


def test_each_actor_value_must_be_an_edge_list(tmp_path: pathlib.Path) -> None:
    path = policy_file(tmp_path, "rules.json")
    doc = policy_doc(path)
    doc["edges"]["terry"] = {}
    write_policy(path, doc)

    with pytest.raises(board_state.BoardError, match=r"edges\.terry is not a list"):
        board_state.TransitionPolicy.load(path)


@pytest.mark.parametrize("actor", ["", " terry", "terry "])
def test_edge_actor_ids_must_be_nonempty_and_unpadded(
    tmp_path: pathlib.Path,
    actor: str,
) -> None:
    path = policy_file(tmp_path, "rules.json")
    doc = policy_doc(path)
    doc["edges"][actor] = doc["edges"].pop("terry")
    write_policy(path, doc)

    with pytest.raises(
        board_state.BoardError,
        match="actor ids must be nonempty and have no outer whitespace",
    ):
        board_state.TransitionPolicy.load(path)


def test_edge_actor_must_be_a_configured_board_user(tmp_path: pathlib.Path) -> None:
    path = policy_file(tmp_path, "rules.json")
    doc = policy_doc(path)
    doc["edges"]["unknown"] = doc["edges"].pop("bot")
    write_policy(path, doc)
    policy = board_state.TransitionPolicy.load(path)

    with pytest.raises(board_state.BoardError, match=r"actor.*unknown"):
        make_store(tmp_path, "invalid-actor", policy)


def test_edge_actors_must_be_the_configured_browser_and_cli_users(
    tmp_path: pathlib.Path,
) -> None:
    path = policy_file(tmp_path, "rules.json")
    doc = policy_doc(path)
    doc["edges"]["scott"] = doc["edges"].pop("bot")
    write_policy(path, doc)
    policy = board_state.TransitionPolicy.load(path)
    users = (*USERS, board_state.User("scott", "Scott", board_state.BOT, "#884422"))
    board = board_state.Board(
        project="configured-actors",
        users=users,
        browser_user="terry",
        cli_user="bot",
        default_owner="bot",
    )
    board_state.save(board, tmp_path / "configured-actors.json")

    with pytest.raises(board_state.BoardError, match=r"exactly match.*bot, terry"):
        api_endpoint.BoardStore(tmp_path / "configured-actors.json", policy)


def test_configured_browser_and_cli_users_must_be_different() -> None:
    with pytest.raises(board_state.BoardError, match="MUST be two different actor ids"):
        board_state.install_users(USERS, "terry", "terry", "terry", "same configured actor")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("from", "not_a_lane"),
        ("to", "not_a_lane"),
    ],
)
def test_edge_endpoints_must_name_declared_lanes(
    tmp_path: pathlib.Path,
    field: str,
    value: str,
) -> None:
    path = policy_file(tmp_path, "rules.json")
    doc = policy_doc(path)
    doc["edges"]["terry"][0][field] = value
    write_policy(path, doc)

    with pytest.raises(board_state.BoardError, match="names unknown lane 'not_a_lane'"):
        board_state.TransitionPolicy.load(path)


def test_self_loop_edge_is_rejected(tmp_path: pathlib.Path) -> None:
    path = policy_file(tmp_path, "rules.json")
    doc = policy_doc(path)
    doc["edges"]["terry"][0] = {"from": "backlog", "to": "backlog"}
    write_policy(path, doc)

    with pytest.raises(board_state.BoardError, match="joins 'backlog' to itself"):
        board_state.TransitionPolicy.load(path)


def test_duplicate_actor_edge_is_rejected(tmp_path: pathlib.Path) -> None:
    path = policy_file(tmp_path, "rules.json")
    doc = policy_doc(path)
    doc["edges"]["terry"].append(dict(doc["edges"]["terry"][0]))
    write_policy(path, doc)

    with pytest.raises(board_state.BoardError, match=r"repeats terry on backlog ->"):
        board_state.TransitionPolicy.load(path)


def test_invalid_json_reports_parser_location(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "broken-rules.json"
    path.write_text('{\n  "schema": 6,\n  "edges": nope\n}\n', encoding="utf-8")

    with pytest.raises(
        board_state.BoardError,
        match=r"invalid JSON at line 3, column 12: Expecting value",
    ):
        board_state.TransitionPolicy.load(path)


def test_edge_entry_must_be_an_object(tmp_path: pathlib.Path) -> None:
    path = policy_file(tmp_path, "rules.json")
    doc = policy_doc(path)
    doc["edges"]["terry"][0] = []
    write_policy(path, doc)

    with pytest.raises(board_state.BoardError, match=r"edges.terry\[0\] is not an object"):
        board_state.TransitionPolicy.load(path)


@pytest.mark.parametrize(
    ("edge", "message"),
    [
        ({"to": "ready_for_work"}, "missing from"),
        ({"from": "backlog"}, "missing to"),
        ({"from": "backlog", "to": "ready_for_work", "note": "legacy"}, "unknown field.*note"),
        ({"from": "", "to": "ready_for_work"}, "from is not a nonempty string"),
        ({"from": 1, "to": "ready_for_work"}, "from is not a nonempty string"),
        ({"from": "backlog", "to": 1}, "to is not a nonempty string"),
        (
            {"from": "backlog", "to": "ready_for_work", "description": []},
            "description is not a string",
        ),
    ],
)
def test_edge_fields_are_strictly_validated(
    tmp_path: pathlib.Path,
    edge: dict[str, Any],
    message: str,
) -> None:
    path = policy_file(tmp_path, "rules.json")
    doc = policy_doc(path)
    doc["edges"]["terry"][0] = edge
    write_policy(path, doc)

    with pytest.raises(board_state.BoardError, match=message):
        board_state.TransitionPolicy.load(path)


def test_stores_enforce_their_own_transition_policies(
    tmp_path: pathlib.Path,
) -> None:
    allowed_path = policy_file(tmp_path, "allowed-rules.json")
    denied_path = policy_file(tmp_path, "denied-rules.json")
    remove_claude_backlog_promotion(denied_path)

    allowed = make_store(tmp_path, "allowed", board_state.TransitionPolicy.load(allowed_path))
    denied = make_store(tmp_path, "denied", board_state.TransitionPolicy.load(denied_path))
    allowed.execute(0, create_alpha)
    denied.execute(0, create_alpha)

    allowed.execute(1, lambda board: board.move("alpha", "ready_for_work", "bot"))
    with pytest.raises(board_state.BoardError, match="not to ready_for_work"):
        denied.execute(1, lambda board: board.move("alpha", "ready_for_work", "bot"))


def test_policy_reload_is_isolated_to_its_store(tmp_path: pathlib.Path) -> None:
    first_path = policy_file(tmp_path, "first-rules.json")
    second_path = policy_file(tmp_path, "second-rules.json")
    first = make_store(tmp_path, "first", board_state.TransitionPolicy.load(first_path))
    second = make_store(tmp_path, "second", board_state.TransitionPolicy.load(second_path))
    remove_claude_backlog_promotion(first_path)

    message = first.reload_policy_if_changed()

    assert message == "rules.json reloaded: 7 lanes"
    assert not first.policy.may_move("bot", "backlog", "ready_for_work")
    assert second.policy.may_move("bot", "backlog", "ready_for_work")
    assert first.snapshot().policy == first.policy


def test_bad_policy_reload_keeps_last_valid_policy_and_is_reported_once(
    tmp_path: pathlib.Path,
) -> None:
    rules_path = policy_file(tmp_path, "rules.json")
    store = make_store(tmp_path, "board", board_state.TransitionPolicy.load(rules_path))
    accepted = store.policy.rules
    old_stamp = rules_path.stat().st_mtime_ns
    rules_path.write_text('{"schema": -1}\n', encoding="utf-8")
    os.utime(rules_path, ns=(old_stamp + 1_000_000_000, old_stamp + 1_000_000_000))

    message = store.reload_policy_if_changed()

    assert message is not None
    assert "changed and was REFUSED" in message
    assert store.policy.rules == accepted
    assert store.reload_policy_if_changed() is None


def test_invalid_json_reload_reports_location_and_keeps_valid_policy(
    tmp_path: pathlib.Path,
) -> None:
    rules_path = policy_file(tmp_path, "rules.json")
    store = make_store(tmp_path, "board", board_state.TransitionPolicy.load(rules_path))
    accepted = store.policy.rules
    old_stamp = rules_path.stat().st_mtime_ns
    rules_path.write_text('{\n  "schema": 6,\n  "edges": nope\n}\n', encoding="utf-8")
    os.utime(rules_path, ns=(old_stamp + 1_000_000_000, old_stamp + 1_000_000_000))

    message = store.reload_policy_if_changed()

    assert message is not None
    assert "invalid JSON at line 3, column 12: Expecting value" in message
    assert store.policy.rules == accepted
