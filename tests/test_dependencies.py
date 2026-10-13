"""Critical-path ordering and audited, transitive blocker priority inheritance."""

import json
import socket
from typing import TYPE_CHECKING

import pytest

from localswim import api_endpoint, board_state

if TYPE_CHECKING:
    import pathlib


def test_critical_depth_precedes_priority_and_ticket(board: board_state.Board) -> None:
    for name, priority in [("isolated", "P0"), ("leaf", "P2"), ("middle", "P5"), ("root", "P5")]:
        board.create(name, name, "backlog", "bot", priority=priority)
    board.link("middle", "blocks", "leaf", "bot")
    board.link("root", "blocks", "middle", "bot")
    assert board_state.blocking_order(board).depths == {
        "isolated": 0,
        "leaf": 0,
        "middle": 1,
        "root": 2,
    }
    assert [item.id for item in board.lanes()[0].items] == ["root", "middle", "isolated", "leaf"]
    assert board.find("root").priority == "P2"
    assert board.find("middle").priority == "P2"
    assert board.verify() == []
    assert board_state.total_order_problems(board) == []


def test_branch_depth_uses_longest_path_not_number_of_children(board: board_state.Board) -> None:
    for name in ["wide", "w1", "w2", "w3", "long", "middle", "leaf"]:
        board.create(name, name, "backlog", "bot", priority="P3")
    for child in ["w1", "w2", "w3"]:
        board.link("wide", "blocks", child, "bot")
    board.link("long", "blocks", "middle", "bot")
    board.link("middle", "blocks", "leaf", "bot")
    assert board_state.blocking_order(board).depths["wide"] == 1
    assert board_state.blocking_order(board).depths["long"] == 2
    assert board.lanes()[0].items[0].id == "long"


def test_paths_cross_lanes_and_include_archived_cards(board: board_state.Board) -> None:
    board.create("root", "Root", "backlog", "bot", priority="P5")
    board.create("middle", "Middle work", "ready_for_work", "bot", priority="P5")
    board.create("leaf", "Leaf", "backlog", "bot", priority="P1")
    board.link("root", "blocks", "middle", "bot")
    board.link("middle", "blocks", "leaf", "bot")
    board.set_archived("leaf", archived=True, by="bot")
    assert board_state.blocking_order(board).depths["root"] == 2
    assert board.find("root").priority == "P1"
    assert [
        it.item.item_id
        for it in board_state.inspect_next_items(
            board,
            ("backlog", "ready_for_work"),
            5,
        )
    ] == ["root", "middle"]
    assert [it.item.item_id for it in board_state.inspect_search_items(board, "r")] == [
        "root",
        "middle",
    ]


def test_priority_changes_promote_all_ancestors_with_actor_and_single_event(
    board: board_state.Board,
) -> None:
    for name in ["root", "middle", "leaf"]:
        board.create(name, name, "backlog", "bot", priority="P5")
    board.link("root", "blocks", "middle", "bot")
    board.link("middle", "blocks", "leaf", "bot")
    board.set_priority("leaf", "P0", "terry")
    assert [item.priority for item in board.items] == ["P0", "P0", "P0"]
    for item in board.items:
        assert len(item.history) == 2
        assert item.history[-1].by == "terry"
        assert item.history[-1].priority_frm == "P5"
        assert item.history[-1].priority_to == "P0"
    before = board.to_json()
    result = board.set_priority("root", "P5", "bot")
    assert "using P0" in result
    assert board.to_json() == before
    board.set_priority("leaf", "P3", "bot")
    assert board.find("root").priority == "P0"
    assert board.find("leaf").priority == "P3"
    assert board.verify() == []


def test_blocked_by_and_relationship_replacement_propagate(board: board_state.Board) -> None:
    board.create("blocker", "Blocker", "backlog", "bot", priority="P5")
    board.create("urgent", "Urgent", "backlog", "bot", priority="P0")
    board.link("urgent", "references", "blocker", "bot")
    assert board.find("blocker").priority == "P5"
    board.replace_link("urgent", "references", "blocked_by", "blocker", "terry")
    assert board.find("blocker").priority == "P0"
    assert board.find("blocker").history[-1].by == "terry"
    assert board_state.blocking_order(board).depths["blocker"] == 1
    board.unlink("urgent", "blocked_by", "blocker", "bot")
    assert board_state.blocking_order(board).depths["blocker"] == 0
    assert board.find("blocker").priority == "P0"


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize(
    ("current_priority", "blocker_priority", "expected"),
    [("P5", "P1", "P1"), ("P0", "P5", "P0"), ("P2", "P2", "P2")],
)
def test_blocked_by_accepts_and_promotes_current_card_or_blocker(
    board: board_state.Board,
    *,
    replace: bool,
    current_priority: str,
    blocker_priority: str,
    expected: str,
) -> None:
    board.create("current", "Current", "backlog", "bot", priority=current_priority)
    board.create("blocker", "Blocker", "backlog", "bot", priority=blocker_priority)
    board.create("ancestor", "Ancestor", "backlog", "bot", priority="P5")
    board.link("ancestor", "blocks", "current", "bot")
    before = {item.id: (item.priority, len(item.history)) for item in board.items}
    if replace:
        board.link("current", "references", "blocker", "bot")
        board.replace_link("current", "references", "blocked_by", "blocker", "terry")
    else:
        board.link("current", "blocked_by", "blocker", "terry")
    assert board.find_link("blocker", "blocks", "current") is not None
    assert board.find_link("current", "references", "blocker") is None
    assert all(item.priority == expected for item in board.items)
    for item in board.items:
        old_priority, events = before[item.id]
        changed = old_priority != expected
        assert len(item.history) == events + int(changed)
        if changed:
            assert item.history[-1].by == "terry"
            assert item.history[-1].priority_frm == old_priority
            assert item.history[-1].priority_to == expected
    before_repeat = board.to_json()
    board.link("current", "blocked_by", "blocker", "terry")
    assert board.to_json() == before_repeat
    board.unlink("current", "blocked_by", "blocker", "terry")
    assert all(item.priority == expected for item in board.items)
    assert board.verify() == []


def test_cycles_share_depth_and_priority_without_hanging(board: board_state.Board) -> None:
    for name, priority in [("a", "P5"), ("b", "P2"), ("leaf", "P0"), ("root", "P4")]:
        board.create(name, name, "backlog", "bot", priority=priority)
    board.link("a", "blocks", "b", "bot")
    board.link("b", "blocks", "a", "bot")
    board.link("b", "blocks", "leaf", "bot")
    board.link("root", "blocks", "a", "bot")
    assert board_state.blocking_order(board).depths == {"a": 1, "b": 1, "leaf": 0, "root": 2}
    assert [item.id for item in board.lanes()[0].items] == ["root", "a", "b", "leaf"]
    assert all(item.priority == "P0" for item in board.items)
    assert len(board.links) == 4
    assert board.verify() == []


def test_hierarchy_and_nonblocking_relationships_do_not_affect_rank(
    board: board_state.Board,
) -> None:
    board.create("parent", "Parent", "backlog", "bot", priority="P5")
    board.create("child", "Child", "backlog", "bot", priority="P0")
    board.set_parent("child", "parent", "bot")
    board.link("parent", "relates_to", "child", "bot")
    assert board_state.blocking_order(board).depths == {"parent": 0, "child": 0}
    assert board.find("parent").priority == "P5"
    assert [item.id for item in board.lanes()[0].items] == ["child", "parent"]


def test_numeric_priority_order_is_independent_of_policy_row_order(
    board: board_state.Board,
) -> None:
    raw = board.to_json()
    policy = raw["policy"]
    assert isinstance(policy, dict)
    priorities = policy["priorities"]
    assert isinstance(priorities, list)
    policy["priorities"] = list(reversed(priorities))
    board = board_state.Board.from_json(raw, "reverse priority rows")
    board.create("low", "Low", "backlog", "bot", priority="P5")
    board.create("high", "High", "backlog", "bot", priority="P0")
    assert [item.id for item in board.lanes()[0].items] == ["high", "low"]
    board.link("low", "blocks", "high", "bot")
    assert board.find("low").priority == "P0"


def test_long_chain_avoids_recursion_limits(board: board_state.Board) -> None:
    count = 1500
    for index in range(count):
        board.create(
            f"item-{index}",
            "Chain",
            "backlog",
            "bot",
            priority="P0" if index == count - 1 else "P5",
        )
    board.links = [
        board_state.Link(f"item-{index}", "blocks", f"item-{index + 1}")
        for index in range(count - 1)
    ]
    order = board_state.blocking_order(board)
    assert order.depths["item-0"] == count - 1
    assert order.depths[f"item-{count - 1}"] == 0
    board.enforce_dependency_priorities("bot")
    assert all(item.priority == "P0" for item in board.items)
    assert board.verify() == []


def test_existing_board_is_reconciled_atomically_once(
    board: board_state.Board,
    tmp_path: pathlib.Path,
) -> None:
    board.create("blocker", "Blocker", "backlog", "bot", priority="P5")
    board.create("urgent", "Urgent", "backlog", "bot", priority="P0")
    board.links.append(board_state.Link("blocker", "blocks", "urgent"))
    board.revision = 7
    path = tmp_path / "old-board.json"
    board_state.save(board, path)
    assert board.verify()
    store = api_endpoint.BoardStore(path)
    normalized = store.snapshot()
    assert normalized.revision == 8
    assert normalized.find("blocker").priority == "P0"
    assert normalized.find("blocker").history[-1].by == "bot"
    assert normalized.links == board.links
    assert normalized.verify() == []
    assert store.snapshot().revision == 8
    assert api_endpoint.BoardStore(path).snapshot().revision == 8


def test_dependency_promotion_rolls_back_when_transaction_fails(
    board: board_state.Board,
    tmp_path: pathlib.Path,
) -> None:
    board.create("blocker", "Blocker", "backlog", "bot", priority="P5")
    board.create("urgent", "Urgent", "backlog", "bot", priority="P0")
    path = tmp_path / "board.json"
    board_state.save(board, path)
    store = api_endpoint.BoardStore(path)
    before = path.read_bytes()

    def refuse(candidate: board_state.Board) -> str:
        candidate.link("blocker", "blocks", "urgent", "bot")
        raise board_state.BoardError("injected refusal")

    with pytest.raises(board_state.BoardError, match="injected refusal"):
        store.execute(0, refuse)
    assert path.read_bytes() == before
    assert store.snapshot().find("blocker").priority == "P5"


def test_completed_lane_uses_same_order(board: board_state.Board) -> None:
    for name, priority in [("leaf", "P1"), ("blocker", "P5"), ("isolated", "P0")]:
        board.create(name, name, "ready_for_work", "bot", priority=priority)
        board.move(name, "in_progress", "bot")
        board.move(name, "ready_for_review", "bot")
        board.move(name, "completed", "terry")
    board.link("blocker", "blocks", "leaf", "bot")
    completed = next(lane for lane in board.lanes() if lane.state == "completed")
    assert [item.id for item in completed.items] == ["blocker", "isolated", "leaf"]


def test_replaced_snapshot_is_reconciled_once(
    board: board_state.Board, tmp_path: pathlib.Path
) -> None:
    board.create("root", "Root", "backlog", "bot", priority="P3")
    board.create("leaf", "Leaf", "backlog", "bot", priority="P3")
    board.link("root", "blocks", "leaf", "bot")
    path = tmp_path / "replace.json"
    board_state.save(board, path)
    store = api_endpoint.BoardStore(path)
    replacement = store.snapshot()
    leaf = replacement.find("leaf")
    leaf.priority = "P0"
    leaf.history.append(
        board_state.Change(board_state.now(), "", "terry", priority_frm="P3", priority_to="P0")
    )
    replacement.revision += 1
    board_state.save(replacement, path)
    repaired = store.snapshot()
    assert repaired.revision == 2
    assert repaired.find("root").priority == "P0"
    assert repaired.find("root").history[-1].by == "bot"
    assert repaired.verify() == []
    before = path.read_bytes()
    assert store.snapshot().revision == 2
    assert path.read_bytes() == before


def test_named_priority_scales_keep_policy_order(board: board_state.Board) -> None:
    raw = board.to_json()
    policy = raw["policy"]
    assert isinstance(policy, dict)
    policy["priorities"] = [
        {"id": "urgent", "label": "Urgent"},
        {"id": "routine", "label": "Routine"},
    ]
    policy["defaultPriority"] = "routine"
    board = board_state.Board.from_json(raw, "named priority scale")
    board.create("root", "Root", "backlog", "bot", priority="routine")
    board.create("leaf", "Leaf", "backlog", "bot", priority="urgent")
    board.link("root", "blocks", "leaf", "bot")
    assert board.find("root").priority == "urgent"
    assert board_state.priority_ranks(board.policy) == {"urgent": 0, "routine": 1}
    board.create("current", "Current", "backlog", "bot", priority="routine")
    board.link("current", "blocked_by", "root", "terry")
    assert board.find("current").priority == "urgent"
    assert board.find("current").history[-1].by == "terry"
    assert board.verify() == []


def test_inherited_priority_is_checked_by_verification(board: board_state.Board) -> None:
    board.create("root", "Root", "backlog", "bot", priority="P5")
    board.create("leaf", "Leaf", "backlog", "bot", priority="P0")
    board.links.append(board_state.Link("root", "blocks", "leaf"))
    assert any("blocker priority is below required P0" in problem for problem in board.verify())
    board.enforce_dependency_priorities("bot")
    assert board.verify() == []


def test_offline_policy_migration_reconciles_old_blocker_priorities(
    board: board_state.Board,
    tmp_path: pathlib.Path,
) -> None:
    board.create("root", "Root", "backlog", "bot", priority="P5")
    board.create("leaf", "Leaf", "backlog", "bot", priority="P0")
    board.links.append(board_state.Link("root", "blocks", "leaf"))
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        board.port = candidate.getsockname()[1]
    policy = tmp_path / "rules.json"
    policy.write_text(json.dumps(board.policy.to_json()), encoding="utf-8")
    raw = board.to_json()
    raw["schema"] = board_state.EMBEDDED_POLICY_PREVIOUS_SCHEMA
    del raw["policy"]
    path = tmp_path / "schema-three.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    board_state.embed_policy(path, policy)
    migrated = board_state.load(path)
    assert migrated.find("root").priority == "P0"
    assert migrated.find("root").history[-1].by == "bot"
    assert migrated.links == board.links
    assert migrated.revision == 1
    assert migrated.verify() == []
