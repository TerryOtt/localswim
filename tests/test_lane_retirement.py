"""Policy and offline storage contracts for lane retirement."""

import json
import socket
from typing import TYPE_CHECKING, cast

import pytest

from localswim import board_state

if TYPE_CHECKING:
    import pathlib

    from localswim.board_state import Board, JsonObject, JsonValue


@pytest.fixture
def board_path(tmp_path: pathlib.Path, board: Board) -> pathlib.Path:
    """Persist a synthetic board with one card archived after entering Ready for Work."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        board.port = sock.getsockname()[1]
    board.create("direct", "Created in lane", "ready_for_work", "bot")
    board.create("arrived", "Moved into lane", "backlog", "bot")
    board.move("arrived", "ready_for_work", "bot")
    for item in board.items:
        board.set_archived(item.id, archived=True, by="bot")
    path = tmp_path / "board.json"
    board_state.save(board, path)
    return path


def read_document(path: pathlib.Path) -> JsonObject:
    return cast("JsonObject", json.loads(path.read_text(encoding="utf-8")))


def write_document(path: pathlib.Path, raw: JsonObject) -> None:
    path.write_text(json.dumps(raw), encoding="utf-8")


@pytest.mark.parametrize(
    ("schema", "marker"),
    [
        (6, None),
        (6, ["ready_for_work"]),
        (7, None),
        (7, []),
        (7, "ready_for_work"),
        (7, [False]),
        (7, ["missing"]),
        (7, ["ready_for_work", "ready_for_work"]),
        (7, ["completed"]),
    ],
)
def test_retired_marker_is_strict(board: Board, schema: int, marker: JsonValue) -> None:
    policy = board.policy.to_json()
    policy.update(schema=schema, retiredLanes=marker)
    with pytest.raises(board_state.BoardError, match="retire"):
        board_state.TransitionPolicy.from_json(policy, "test")


def test_schema_seven_requires_marker(board: Board) -> None:
    policy = board.policy.to_json()
    policy["schema"] = 7
    with pytest.raises(board_state.BoardError, match="retiredLanes"):
        board_state.TransitionPolicy.from_json(policy, "test")


def test_schema_seven_requires_integer_version(board: Board) -> None:
    policy = board.policy.to_json()
    policy.update(schema=7.0, retiredLanes=["ready_for_work"])
    with pytest.raises(board_state.BoardError, match="rules schema"):
        board_state.TransitionPolicy.from_json(policy, "test")


def test_retirement_refuses_last_active_lane(board_path: pathlib.Path) -> None:
    raw = read_document(board_path)
    raw["items"] = []
    raw.pop("archiveHistory")
    policy = cast("JsonObject", raw["policy"])
    policy["lanes"] = [{"id": "backlog", "label": "Backlog", "create": ["bot"]}]
    policy["edges"] = {"bot": [], "terry": []}
    write_document(board_path, raw)
    before = board_path.read_bytes()
    with pytest.raises(board_state.BoardError, match="at least one active lane"):
        board_state.retire_lane(board_path, "backlog")
    assert board_path.read_bytes() == before


def test_legacy_policy_override_cannot_bypass_retirement(board_path: pathlib.Path) -> None:
    original_policy = board_state.load(board_path).policy
    board_state.retire_lane(board_path, "ready_for_work")
    with pytest.raises(board_state.BoardError, match="overrides cannot be used"):
        board_state.load(board_path, original_policy)


def test_retirement_preserves_snapshot_and_repeats_as_noop(board_path: pathlib.Path) -> None:
    original = read_document(board_path)
    original["extension"] = {"unmodeled": "preserve"}
    write_document(board_path, original)
    assert board_state.retire_lane(board_path, "ready_for_work") == (
        "retired lane ready_for_work; revision 1"
    )
    policy = cast("JsonObject", original["policy"])
    policy.update(schema=7, retiredLanes=["ready_for_work"])
    original["revision"] = 1
    assert read_document(board_path) == original
    before_retry = board_path.read_bytes()
    assert "already retired" in board_state.retire_lane(board_path, "ready_for_work")
    assert board_path.read_bytes() == before_retry
    assert board_state.retire_lane(board_path, "blocked") == "retired lane blocked; revision 2"
    assert board_state.load(board_path).policy.retired_lanes == {"ready_for_work", "blocked"}


def test_retirement_keeps_audit_and_allows_authorized_archive_recovery(
    board_path: pathlib.Path,
) -> None:
    board_state.retire_lane(board_path, "ready_for_work")
    board = board_state.load(board_path)
    assert board.verify() == []
    assert board.policy.retired_lanes == {"ready_for_work"}
    assert "ready_for_work" in board.policy.states
    assert "ready_for_work" not in board.policy.active_states
    assert all("ready_for_work" not in edge for edge in board.policy.edges_for("bot"))
    assert all(lane.state != "ready_for_work" for lane in board.lanes())
    retired = next(
        lane for lane in board.lanes(include_archived=True) if lane.state == "ready_for_work"
    )
    assert [item.id for item in retired.items] == ["direct", "arrived"]
    with pytest.raises(board_state.BoardError, match="retired"):
        board.set_archived("direct", archived=False, by="bot")
    with pytest.raises(board_state.BoardError, match="retired"):
        board.create("new", "New card", "ready_for_work", "bot")
    board.create("new", "New card", "backlog", "bot")
    with pytest.raises(board_state.BoardError, match="retired"):
        board.move("new", "ready_for_work", "bot")
    with pytest.raises(board_state.BoardError, match="retired"):
        board.move("direct", "ready_for_work", "bot")
    with pytest.raises(board_state.BoardError):
        board.move("direct", "completed", "bot")
    board.move("direct", "in_progress", "bot")
    assert board.find("direct").archived
    board.set_archived("direct", archived=False, by="bot")
    assert board.verify() == []


def test_lane_summaries_and_refusals_omit_retired_endpoints(board: Board) -> None:
    document = board.policy.to_json()
    document.update(
        schema=7,
        retiredLanes=["ready_for_work"],
        edges={
            "bot": [
                {"from": "backlog", "to": "ready_for_work"},
                {"from": "ready_for_work", "to": "backlog"},
                {"from": "in_progress", "to": "ready_for_work"},
                {"from": "in_progress", "to": "blocked"},
            ],
            "terry": [{"from": "in_progress", "to": "ready_for_work"}],
        },
    )
    policy = board_state.TransitionPolicy.from_json(document, "test")
    assert board_state.lane_owner_label("backlog", policy) == "IN: nobody  ·  OUT: nobody"
    assert board_state.lane_owner_label("in_progress", policy) == "IN: nobody  ·  OUT: Bot"
    assert policy.has_move_grant("bot", "backlog", "ready_for_work")
    assert policy.may_move("bot", "ready_for_work", "backlog")
    assert policy.explain_refusal("bot", "backlog", "blocked") == "Nothing moves out of backlog"
    advice = policy.explain_refusal("bot", "in_progress", "completed")
    assert advice.endswith("bot may go to: blocked")
    assert policy.explain_refusal("terry", "in_progress", "completed").endswith(
        "That belongs to: bot"
    )


@pytest.mark.parametrize(
    ("create", "moves"),
    [
        (["ready_for_work"], []),
        ([], [("completed", "ready_for_work")]),
        ([], [("ready_for_work", "completed")]),
    ],
)
def test_grants_cannot_involve_retired_lanes(
    board_path: pathlib.Path, create: list[str], moves: list[tuple[str, str]]
) -> None:
    board_state.retire_lane(board_path, "ready_for_work")
    board = board_state.load(board_path)
    before = board.to_json()
    with pytest.raises(board_state.BoardError, match="retired"):
        board.grant_permissions(["bot"], create, moves, "bot")
    assert board.to_json() == before


def test_retirement_refuses_active_card_and_invalid_audit(board_path: pathlib.Path) -> None:
    board = board_state.load(board_path)
    board.set_archived("direct", archived=False, by="bot")
    board_state.save(board, board_path)
    before = board_path.read_bytes()
    with pytest.raises(board_state.BoardError, match=r"active cards.*#0001"):
        board_state.retire_lane(board_path, "ready_for_work")
    assert board_path.read_bytes() == before

    board.find("direct").state = "backlog"
    board_state.save(board, board_path)
    before = board_path.read_bytes()
    with pytest.raises(board_state.BoardError, match="board does not verify"):
        board_state.retire_lane(board_path, "blocked")
    assert board_path.read_bytes() == before


def test_verify_flags_active_card_without_lane_history(board_path: pathlib.Path) -> None:
    board_state.retire_lane(board_path, "ready_for_work")
    board = board_state.load(board_path)
    item = board.find("direct")
    item.history.clear()
    item.archived = False
    assert "direct: active card in retired lane 'ready_for_work'" in board.verify()


def test_structural_maintenance_retains_retirement_without_new_grants(
    board_path: pathlib.Path,
) -> None:
    board_state.retire_lane(board_path, "ready_for_work")
    board_state.add_lane(board_path, "Incoming", create_for=("bot",), connect_for=("bot",))
    board_state.rename_lane_label(board_path, "ready_for_work", "Old Ready")
    board_state.migrate_lane_id(board_path, "ready_for_work", "old_ready")
    board = board_state.load(board_path)
    assert board.verify() == []
    assert board.policy.retired_lanes == {"old_ready"}
    assert board.policy.lane_label["old_ready"] == "Old Ready"
    assert board.find("arrived").state == "old_ready"
    assert not board.policy.has_move_grant("bot", "incoming", "old_ready")
    assert not board.policy.has_move_grant("bot", "old_ready", "incoming")
    assert board.policy.may_move("bot", "incoming", "backlog")
